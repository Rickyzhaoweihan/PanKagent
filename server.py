#!/usr/bin/env python3
"""
PlannerAgent API Server
FastAPI server for serving PlannerAgent queries via HTTP.

Endpoints
---------
  GET    /              — API info
  GET    /health        — health check

  Multi-turn dialogue (session-based):
  POST   /chat/start    — start a chat session (returns plan for review by default; opt-in auto_confirm)
  POST   /chat/message  — follow-up; smart-routed to pipeline or context-only
  GET    /chat/history  — retrieve conversation history
  DELETE /chat/end      — end session and free memory

  Interactive plan review (session-based):
  POST   /plan/start    — start interactive plan session (returns plan for review)
  POST   /plan/revise   — revise plan with a user prompt
  POST   /plan/confirm  — confirm plan and run final format/reasoning pipeline
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
from dataclasses import dataclass, field
import asyncio
import io
import json
import os
import sys
import time
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

# Load .env BEFORE importing modules that read ANTHROPIC_API_KEY at import time
# (claude.py, qp_query_planner.py, PankBaseAgent/claude.py all read os.environ).
# .env lives next to server.py.
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(_env_path, override=False)
except ImportError:
    pass  # python-dotenv not installed — fall back to whatever env the process inherited

# Alias CLAUDE_API_KEY -> ANTHROPIC_API_KEY for backward-compat with older .env files.
# All code downstream reads ANTHROPIC_API_KEY.
if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("CLAUDE_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = os.environ["CLAUDE_API_KEY"]

# Import the main chat function and the rigor-mode toggle
import main as _main_module
from main import (
    chat_one_round, extract_markdown, clean_response_json,
    format_plan_as_markdown, clean_user_question, run_plan_start,
    run_plan_revise, run_plan_confirm,
)
from utils import reset_cypher_queries
from stream_events import emit

# Default to rigor mode for the server
_main_module.RIGOR_MODE = True

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — pre-initialise expensive singletons ONCE at startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize agents on startup, cleanup on shutdown"""
    logger.info("=" * 60)
    logger.info("Starting PlannerAgent API Server...")
    logger.info("=" * 60)

    try:
        start_time = time.time()

        # 1. Pre-load PankBaseAgent text2cypher agent + session
        logger.info("Pre-loading PankBaseAgent Text2Cypher agent...")
        from PankBaseAgent.utils import _get_text2cypher_agent, _get_pankbase_session
        _get_text2cypher_agent()
        _get_pankbase_session()

        # 2. Pre-load query-planner skill text2cypher agent (separate singleton)
        logger.info("Pre-loading query-planner Text2Cypher agent...")
        import os
        _qp_scripts = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "skills", "query-planner", "scripts"
        )
        if _qp_scripts not in sys.path:
            sys.path.insert(0, _qp_scripts)
        from qp_query_planner import _get_text2cypher_agent as _qp_get_agent
        _qp_get_agent()

        # 3. Pre-load HIRN literature scripts (avoids import-lock in threads)
        logger.info("Pre-loading HIRN literature scripts...")
        _hirn_skill_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "hirn_publication_retrieval", "skills", "hirn-literature-retrieve",
        )
        if _hirn_skill_dir not in sys.path:
            sys.path.insert(0, _hirn_skill_dir)
        import scripts.scrape_hirn, scripts.resolve_ids       # noqa: F401
        import scripts.fetch_fulltext, scripts.chunk_text      # noqa: F401
        import scripts.search_chunks, scripts.query_expander   # noqa: F401

        # 3b. HPAP MySQL skill DISABLED — donor metadata now lives in Neo4j KG (donor nodes)
        # The MySQL database skill is no longer loaded or used.

        # 3c. Pre-load genomic coordinates skill
        logger.info("Pre-loading genomic coordinates skill...")
        _genomic_skill_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "PankBaseAgent", "text_to_sql",
        )
        if _genomic_skill_dir not in sys.path:
            sys.path.insert(0, _genomic_skill_dir)
        from src.text2sql_agent import Text2SQLAgent as _Text2SQLAgent  # noqa: F401

        # 3d. Pre-load ssGSEA client
        logger.info("Pre-loading ssGSEA client...")
        _ssgsea_skill_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "skills", "ssgsea",
        )
        if _ssgsea_skill_dir not in sys.path:
            sys.path.insert(0, _ssgsea_skill_dir)
        import ssgsea_client as _ssgsea_client  # noqa: F401

        # 3e. Pre-load functional data client
        logger.info("Pre-loading functional data client...")
        _functional_skill_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "skills", "functional_data",
        )
        if _functional_skill_dir not in sys.path:
            sys.path.insert(0, _functional_skill_dir)
        import functional_data_client as _functional_data_client  # noqa: F401
        _functional_data_client._load_specs()  # warm the JSON spec cache at startup

        # 4. Load experience buffer (optional — graceful failure)
        try:
            logger.info("Loading experience buffer...")
            from PankBaseAgent.experience_buffer import get_experience_buffer
            buffer = get_experience_buffer()
            loaded = buffer.load_best_examples(max_examples=50)
            stats = buffer.get_stats()
            logger.info(
                f"✓ Experience buffer: {len(loaded)} examples "
                f"from {stats['total_curated']} curated"
            )
        except Exception as e:
            logger.warning(f"Experience buffer not available: {e}")

        # 5. Initialize SQLite session store and restore non-expired sessions
        try:
            import session_store
            session_store.init_db()
            now = time.time()
            restored_plan = 0
            for s in session_store.load_all_plan_sessions():
                if now - s.created_at > SESSION_TTL_SECONDS:
                    continue  # expired — row stays on disk per keep-forever policy
                _plan_sessions[s.session_id] = s
                restored_plan += 1
            restored_chat = 0
            for s in session_store.load_all_chat_sessions():
                if now - s.last_active > CHAT_SESSION_TTL_SECONDS:
                    continue
                _chat_sessions[s.session_id] = s
                restored_chat += 1
            logger.info(
                f"✓ Session store: restored {restored_plan} plan, "
                f"{restored_chat} chat sessions from SQLite"
            )
        except Exception as e:
            logger.error(f"✗ Session store init / restore failed: {e}", exc_info=True)
            raise

        # 6. Background TTL cleanup — runs every 5 minutes regardless of request rate
        #    so expired sessions don't accumulate in memory between requests.
        _stop_cleanup = threading.Event()

        def _cleanup_loop():
            while not _stop_cleanup.wait(timeout=300):
                try:
                    _cleanup_expired_sessions()
                    _cleanup_expired_chat_sessions()
                except Exception:
                    pass

        _cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True, name="session-cleanup")
        _cleanup_thread.start()

        elapsed = time.time() - start_time
        logger.info(f"✓ All agents initialized in {elapsed:.2f}s")
        logger.info("=" * 60)
        logger.info("Server is ready to accept requests!")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"✗ Failed to initialize: {e}", exc_info=True)
        raise

    yield  # Server runs here

    _stop_cleanup.set()

    logger.info("Shutting down server...")
    try:
        import session_store
        session_store.close_db()
    except Exception as e:
        logger.warning(f"session_store close on shutdown failed: {e}")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PlannerAgent API",
    description="API for querying biomedical knowledge graph using natural language",
    version="2.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def strip_trailing_slash(request: Request, call_next):
    """Normalize a trailing ``/`` on the request path so e.g. ``POST /chat/start/``
    is routed exactly like ``POST /chat/start``.

    Why: FastAPI's default ``redirect_slashes=True`` answers any trailing-slash
    request with a ``307 Temporary Redirect`` whose ``Location`` is built from
    the request's host header. When we sit behind an nginx reverse-proxy that
    strips a path prefix (here: ``/pankgraph-agent``), nginx rewrites only the
    host in the redirected ``Location`` — NOT the stripped prefix — so the
    client follows the redirect to a path that nginx does not route, which
    returns **404**.

    Rewriting ``path`` / ``raw_path`` on the ASGI scope makes the downstream
    router see the no-slash form directly, so no redirect is ever issued. This
    is applied before every route handler, including mounted sub-apps.

    The root path (``/``) is left alone — rstrip on ``/`` would produce the
    empty string, which ASGI treats as a missing path and 404s.
    """
    path = request.scope.get("path", "/")
    if path != "/" and path.endswith("/"):
        new_path = path.rstrip("/") or "/"
        request.scope["path"] = new_path
        request.scope["raw_path"] = new_path.encode("latin-1")
    return await call_next(request)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    message: str
    uptime_seconds: Optional[float] = None


# ---------------------------------------------------------------------------
# Plan-confirmation session state
# ---------------------------------------------------------------------------

SESSION_TTL_SECONDS = 1800  # 30 minutes

@dataclass
class PlanSession:
    session_id: str
    original_question: str
    rigor: bool
    chat_history: list = field(default_factory=list)
    current_plan: dict = field(default_factory=dict)
    neo4j_results: list = field(default_factory=list)
    cypher_queries: list = field(default_factory=list)
    complexity: str = "simple"
    use_literature: bool = True
    literature_result: str = ""
    created_at: float = field(default_factory=time.time)

_plan_sessions: dict[str, PlanSession] = {}
_sessions_lock = threading.Lock()


def _persist_plan_session(s: "PlanSession") -> None:
    """Synchronous write to the SQLite session store.

    Called by every handler that mutates a PlanSession, inside the
    ``_sessions_lock`` block, before returning the HTTP response. Raises on
    failure — losing a session's disk record silently is worse than a 500.

    Lock order: caller holds ``_sessions_lock``; this acquires
    ``session_store._conn_lock``. Never the reverse.
    """
    import session_store
    session_store.upsert_plan_session(s)


def _delete_plan_session_row(session_id: str) -> None:
    import session_store
    session_store.delete_plan_session(session_id)


def _get_session(session_id: str) -> PlanSession:
    with _sessions_lock:
        session = _plan_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found or expired")
    if time.time() - session.created_at > SESSION_TTL_SECONDS:
        with _sessions_lock:
            _plan_sessions.pop(session_id, None)
        raise HTTPException(status_code=410, detail=f"Session '{session_id}' has expired")
    return session


def _cleanup_expired_sessions():
    now = time.time()
    with _sessions_lock:
        expired = [sid for sid, s in _plan_sessions.items()
                   if now - s.created_at > SESSION_TTL_SECONDS]
        for sid in expired:
            del _plan_sessions[sid]


# ---------------------------------------------------------------------------
# Plan session JSONL logger
# ---------------------------------------------------------------------------

_PLAN_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_PLAN_LOG_PATH = os.path.join(_PLAN_LOG_DIR, "plan_sessions.jsonl")
_plan_log_lock = threading.Lock()


_LOG_MAX_LINE_BYTES = 500_000  # 500 KB hard cap per JSONL line


def _log_plan_event(session_id: str, event: str, data: dict) -> None:
    """Append a structured JSON line to logs/plan_sessions.jsonl.

    Guarantees:
    - Thread-safe via ``_plan_log_lock``
    - File is opened in append mode — never overwrites existing data
    - Failures are logged but **never** propagated to callers
    - Oversized entries are truncated, not dropped
    - Non-serialisable values fall back to ``str()``
    """
    try:
        safe_data = data if isinstance(data, dict) else {"raw": str(data)[:2000]}

        entry = {
            "session_id": session_id,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": safe_data,
        }

        try:
            line = json.dumps(entry, ensure_ascii=False, default=str)
        except (TypeError, ValueError, OverflowError):
            entry["data"] = {"_serialization_error": True, "event": event}
            line = json.dumps(entry, ensure_ascii=False, default=str)

        if len(line.encode("utf-8")) > _LOG_MAX_LINE_BYTES:
            entry["data"] = {
                "_truncated": True,
                "question": str(safe_data.get("question", ""))[:500],
                "event": event,
                "processing_time_ms": safe_data.get("processing_time_ms"),
            }
            line = json.dumps(entry, ensure_ascii=False, default=str)

        line += "\n"

        os.makedirs(_PLAN_LOG_DIR, exist_ok=True)
        with _plan_log_lock:
            with open(_PLAN_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as exc:
        try:
            logger.warning("[plan-log] Failed to write %s for %s: %s", event, session_id, exc)
        except Exception:
            pass

    # Mirror to SQLite events table (log-and-continue — JSONL is the safety net)
    try:
        import session_store
        payload = data if isinstance(data, dict) else {"raw": str(data)[:2000]}
        session_store.record_event(session_id, event, payload)
    except Exception as exc:
        try:
            logger.warning("[sqlite-events] %s for %s failed: %s", event, session_id, exc)
        except Exception:
            pass


class PlanStartRequest(BaseModel):
    question: str = Field(..., description="Natural language question")
    rigor: bool = Field(True, description="Use rigorous mode")
    use_literature: bool = Field(True, description="Run HIRN literature search in parallel")

class PlanReviseRequest(BaseModel):
    session_id: str = Field(..., description="Session ID from /plan/start")
    prompt: str = Field(..., description="Revision instruction from the user")

class PlanConfirmRequest(BaseModel):
    session_id: str = Field(..., description="Session ID from /plan/start")

class PlanResponse(BaseModel):
    session_id: str
    plan_markdown: str
    plan_json: dict
    use_literature: bool = False
    error: Optional[str] = None

class PlanConfirmResponse(BaseModel):
    answer: str = Field(..., description="Full pipeline JSON response")
    answer_markdown: str = Field("", description="Clean Markdown output identical to the CLI printout")
    processing_time_ms: float


class FunctionalDataRequest(BaseModel):
    question: str = Field(..., description="Natural language question about islet functional assay data")
    donor_ids: Optional[list[str]] = Field(None, description="Optional list of donor IDs to filter by")


class FunctionalDataParamsResponse(BaseModel):
    endpoint: str = Field(..., description="API endpoint path used")
    url: str = Field(..., description="Full URL including query string")
    params: dict = Field(..., description="Query parameters sent to the functional data API")


# ---------------------------------------------------------------------------
# Multi-turn chat session state
# ---------------------------------------------------------------------------

CHAT_SESSION_TTL_SECONDS = 3600  # 1 hour

@dataclass
class ChatSession:
    session_id: str
    history: list = field(default_factory=list)  # [{"role": "user"|"assistant", "content": str}, ...]
    rigor: bool = True
    use_literature: bool = True
    # Last new_query round's plan state (used when the user asks to revise the most recent plan)
    last_question: str = ""
    last_plan: dict = field(default_factory=dict)
    last_neo4j_results: list = field(default_factory=list)
    last_cypher_queries: list = field(default_factory=list)
    last_complexity: str = "simple"
    last_literature_result: str = ""
    # Pending plan review state: set when /chat/message routes a new_query that
    # needs user confirmation via /chat/plan/confirm. Cleared on confirm.
    pending_question: str = ""
    pending_plan_session_id: str = ""
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

_chat_sessions: dict[str, ChatSession] = {}
_chat_sessions_lock = threading.Lock()


def _persist_chat_session(s: "ChatSession") -> None:
    """Sibling of ``_persist_plan_session`` for ChatSession — see that docstring."""
    import session_store
    session_store.upsert_chat_session(s)


def _delete_chat_session_row(session_id: str) -> None:
    import session_store
    session_store.delete_chat_session(session_id)

# Token budget: ~50K tokens ≈ ~160K chars for history
_MAX_HISTORY_CHARS = 150_000


def _get_chat_session(session_id: str) -> ChatSession:
    with _chat_sessions_lock:
        session = _chat_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Chat session '{session_id}' not found or expired")
    if time.time() - session.last_active > CHAT_SESSION_TTL_SECONDS:
        with _chat_sessions_lock:
            _chat_sessions.pop(session_id, None)
        raise HTTPException(status_code=410, detail=f"Chat session '{session_id}' has expired")
    session.last_active = time.time()
    return session


def _cleanup_expired_chat_sessions():
    now = time.time()
    with _chat_sessions_lock:
        expired = [sid for sid, s in _chat_sessions.items()
                   if now - s.last_active > CHAT_SESSION_TTL_SECONDS]
        for sid in expired:
            del _chat_sessions[sid]


def _trim_history(history: list[dict]) -> list[dict]:
    """Keep history within token budget. Drop oldest rounds but always keep the first round."""
    total = sum(len(m["content"]) for m in history)
    if total <= _MAX_HISTORY_CHARS:
        return history
    # Always keep the first Q+A pair (rounds 0-1) for context anchoring
    first_pair = history[:2] if len(history) >= 2 else history[:]
    rest = history[2:]
    # Drop oldest pairs from `rest` until we fit
    while rest and sum(len(m["content"]) for m in first_pair + rest) > _MAX_HISTORY_CHARS:
        rest = rest[2:]  # drop one Q+A pair at a time
    return first_pair + rest


def _classify_followup(history: list[dict], new_question: str) -> str:
    """Binary classifier for multi-turn chat: is the new question a follow-up
    to the previously retrieved context, or a genuinely new query?

    Returns ``'follow_up'`` or ``'new_query'``.

    - ``follow_up``: same entities, same retrieved data applies. The rigor-format
      or rigor-reasoning agent re-answers using the session's stored
      ``last_neo4j_results`` — no new DB queries.
    - ``new_query``: new entities or new data dimensions needed. Triggers full
      plan mode (with dialogue history as context) and returns the plan for
      user review.
    """
    import anthropic

    # Build a compact summary of what's been discussed. 800 chars/turn is
    # wide enough to preserve entity tails (gene lists, donor IDs, GO terms)
    # on long round-1 answers, while keeping the classifier prompt cheap.
    turns = []
    for m in history[-6:]:
        role = "User" if m["role"] == "user" else "Assistant"
        turns.append(f"{role}: {m['content'][:800]}")
    conversation_summary = "\n".join(turns)

    system_prompt = (
        "You classify follow-up questions in a biomedical Q&A system that "
        "orchestrates Neo4j, PostgreSQL, ssGSEA, and literature retrieval.\n\n"
        "Respond with EXACTLY one word: 'follow_up' or 'new_query'.\n\n"
        "HARD RULE — follow_up NEVER invokes any tool (no Neo4j, no SQL, no "
        "ssGSEA, no literature). It can ONLY rephrase, explain, summarise, "
        "rank, filter, or reason over text already present in prior assistant "
        "answers.\n\n"
        "DECISION PROCEDURE — apply in order:\n"
        "1. Reference detection. Does the new question reference the prior "
        "turn? Signals include pronouns ('those', 'that one', 'of them', "
        "'the first one'), demonstratives ('this gene', 'these donors'), or "
        "elided subjects ('and the PIP?', 'what about CFTR?'). When present, "
        "the user is continuing the prior thread — STRONG bias toward "
        "follow_up, but not decisive on its own.\n"
        "2. Coverage check. Can the question be answered using ONLY facts "
        "literally present in prior assistant messages — entities, IDs, "
        "numeric values, terms, descriptions? If yes → follow_up.\n"
        "3. New-dimension test. Would answering require fetching a data "
        "dimension not literally present in prior turns (expression levels, "
        "OCR peaks, genomic coordinates, ssGSEA scores, literature/PubMed, "
        "pathway annotations, donor metadata, drug targets)? If yes → "
        "new_query, even if the referenced entity was already named.\n"
        "4. New-entity test. Does the question introduce a gene, SNP, "
        "disease, cell type, donor, or other entity NOT present in prior "
        "turns? If yes → new_query.\n"
        "5. Ambiguity → new_query.\n\n"
        "FOLLOW_UP examples:\n"
        "- 'What do those GO terms mean?' — explains shown data.\n"
        "- 'Of those genes, which has the highest PIP?' — ranks listed values.\n"
        "- 'Summarise this for a clinician.' — rephrasing.\n"
        "- 'Which donor you found is youngest?' — filters shown rows.\n"
        "- 'Why is that significant?' — reasoning over shown facts.\n\n"
        "NEW_QUERY examples (note: several contain anaphora):\n"
        "- 'What is the expression of CFTR in beta cells?' — new dimension.\n"
        "- 'Tell me about RFX6.' — new entity.\n"
        "- 'Which of those genes are highly expressed in alpha cells?' "
        "— anaphora present, but needs expression data not yet shown.\n"
        "- 'Any PubMed articles on these?' — needs literature retrieval.\n"
        "- 'Are any of those druggable?' — needs drug data not shown.\n\n"
        "Reply with exactly one word."
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=15,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": f"Conversation so far:\n{conversation_summary}\n\nNew question: {new_question}",
            }],
        )
        answer = response.content[0].text.strip().lower()
        if "follow" in answer:
            return "follow_up"
        return "new_query"
    except Exception as exc:
        logger.warning(f"Follow-up classification failed: {exc}")
        return "new_query"


def _answer_is_tool_output(text: str) -> bool:
    """Return True if the follow_up answer looks like raw tool output (Cypher,
    SQL, etc.) rather than a natural-language response — which means the
    format/reasoning agent tried to query the DB instead of answering from
    context. The caller should re-route to new_query in that case."""
    import re
    t = text.strip()
    patterns = [
        r"(?i)```\s*(cypher|sql|sparql)",   # fenced code block labelled cypher/sql
        r"(?i)\bMATCH\s*\(",                # bare Cypher MATCH clause
        r"(?i)^SELECT\s+",                  # bare SQL SELECT
        r"(?i)^(WITH|UNWIND)\s+",           # Cypher WITH/UNWIND at top level
        r"\bRETURN\s+\w",                   # Cypher RETURN keyword (uppercase only — avoids English prose)
    ]
    return any(re.search(p, t) for p in patterns)


# ---------------------------------------------------------------------------
# History context builder for follow-up mode
# ---------------------------------------------------------------------------

_HISTORY_CONTEXT_LIMIT = 25_000  # chars; above this, compress older turns via Haiku


def _build_history_context(history: list[dict]) -> tuple[str, bool]:
    """Build the full-dialogue context string passed to the format/reasoning
    agent for ``follow_up`` rounds.

    Returns ``(context_str, was_compressed)``. If the concatenated history is
    under ``_HISTORY_CONTEXT_LIMIT`` chars, every turn is included verbatim.
    Otherwise, all but the most recent Q+A pair are summarised via Claude
    Haiku and ``was_compressed=True`` — the frontend should display a notice.
    """
    if not history:
        return "", False

    def _fmt(turns: list[dict]) -> str:
        return "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
            for m in turns
        )

    full_text = _fmt(history)
    if len(full_text) <= _HISTORY_CONTEXT_LIMIT:
        return f"[Prior conversation]\n{full_text}", False

    # Compress: keep the last Q+A verbatim, summarise everything before
    recent = history[-2:] if len(history) >= 2 else history[:]
    older = history[:-2] if len(history) >= 2 else []

    summary = ""
    if older:
        try:
            import anthropic
            client = anthropic.Anthropic()
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarise this biomedical research conversation into a compact "
                        "paragraph (2-5 sentences). Preserve ALL gene names, SNP IDs "
                        "(rsIDs), disease terms, key numeric values, and conclusions "
                        "reached. Write in past tense, as if reporting what was "
                        "discussed:\n\n"
                        + _fmt(older)
                    ),
                }],
            )
            summary = resp.content[0].text.strip()
        except Exception as exc:
            logger.warning(f"History compression failed: {exc}")
            # Fallback: hard-truncate older turns
            summary = _fmt(older)[-4000:]

    context = (
        "[Prior conversation — older turns summarised]\n"
        f"Summary: {summary}\n\n"
        "[Most recent exchange]\n"
        f"{_fmt(recent)}"
    )
    return context, True


def _answer_from_context(history: list[dict], new_question: str, rigor: bool = True) -> str:
    """Answer a follow-up question using only conversation history (no database queries)."""
    import anthropic

    messages = []
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": new_question})

    system_prompt = (
        "You are a biomedical research assistant continuing a conversation. "
        "The user's previous questions and your previous answers are in the conversation history. "
        "Answer the follow-up question using ONLY information from the prior conversation. "
        "Do NOT make up data, PubMed IDs, or statistics that weren't in your previous answers. "
        "If you cannot answer from the available context, say so clearly and suggest what new query would help."
    )
    if rigor:
        system_prompt += (
            "\nIMPORTANT: You are in rigorous evidence-only mode. "
            "Do NOT speculate or add information beyond what was explicitly retrieved."
        )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            messages=messages,
        )
        return response.content[0].text.strip()
    except Exception as exc:
        logger.error(f"Context-only response failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to generate response: {exc}")


class ChatStartRequest(BaseModel):
    question: str = Field(..., description="First question to start the conversation")
    rigor: bool = Field(True, description="Use rigorous evidence-only mode")
    use_literature: bool = Field(True, description="Include HIRN literature in the plan")
    auto_confirm: bool = Field(
        False,
        description="If True, run plan and auto-confirm in one shot (route='new_query'). "
                    "If False (default), return the plan as 'new_query_pending' and wait "
                    "for /chat/plan/confirm.",
    )

class ChatMessageRequest(BaseModel):
    session_id: str = Field(..., description="Chat session ID from /chat/start")
    question: str = Field(..., description="Follow-up question")

class ChatReviseRequest(BaseModel):
    session_id: str = Field(..., description="Chat session ID")
    prompt: str = Field(..., description="Revision instruction for the last plan")

class ChatResponse(BaseModel):
    session_id: str
    answer: str = Field("", description="Full pipeline JSON")
    answer_markdown: str = Field(..., description="Markdown answer")
    route: str = Field(..., description="'follow_up' | 'new_query' | 'new_query_pending'")
    round: int = Field(..., description="Conversation round number (1-indexed)")
    plan_markdown: str = Field("", description="Plan summary for this round (new_query only)")
    plan_json: Optional[dict] = Field(None, description="Plan JSON for this round (new_query only)")
    pending_plan_session_id: Optional[str] = Field(
        None,
        description="Set when route='new_query_pending'. Client should display the "
                    "plan_markdown to the user and call /chat/plan/confirm with this ID to complete.",
    )
    history_compressed: bool = Field(
        False,
        description="True when prior conversation history was summarised to fit "
                    "context limits. Frontend should display a notice to the user.",
    )
    processing_time_ms: float

class ChatPlanConfirmRequest(BaseModel):
    chat_session_id: str = Field(..., description="Chat session ID")
    plan_session_id: str = Field(..., description="Pending plan session ID from the last /chat/message")
    revision_prompt: Optional[str] = Field(
        None,
        description="Optional revision to apply before confirming (e.g., 'also add eQTLs')",
    )

class ChatHistoryResponse(BaseModel):
    session_id: str
    rounds: int
    history: list[dict]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SERVER_START_TIME = time.time()

# Lock to serialise requests that use module-level global state
_request_lock = threading.Lock()


def _run_query(question: str, rigor: bool = True) -> str:
    """Run the full pipeline under the request lock and return the answer string.

    Deprecated: kept for any remaining callers, but the chat endpoints now use
    ``_run_plan_pipeline`` + ``_run_confirm`` instead (plan-mode foundation).
    """
    with _request_lock:
        _main_module.RIGOR_MODE = rigor
        reset_cypher_queries()
        messages = []
        _, response = chat_one_round(messages, question)
        return response


def _run_plan_pipeline(question: str, use_literature: bool = True,
                       chat_history: list[dict] | None = None) -> dict:
    """Wrap run_plan_start under the request lock. Returns the full plan dict
    (plan, neo4j_results, cypher_queries, complexity, use_literature, literature_result).

    If ``chat_history`` is provided, it is forwarded to ``run_plan_start`` so
    the planner can resolve entity references against prior conversation.
    """
    clean_q = clean_user_question(question)
    with _request_lock:
        return {
            "interpreted_question": clean_q,
            **run_plan_start(clean_q, use_literature=use_literature, chat_history=chat_history),
        }


def _run_plan_revise(
    original_question: str,
    current_plan: dict,
    neo4j_results: list,
    user_prompt: str,
    use_literature: bool,
    literature_result: str,
) -> dict:
    """Wrap run_plan_revise under the request lock."""
    with _request_lock:
        return run_plan_revise(
            original_question=original_question,
            current_plan=current_plan,
            neo4j_results=neo4j_results,
            user_prompt=user_prompt,
            use_literature=use_literature,
            literature_result=literature_result,
        )


def _run_confirm(
    question: str,
    neo4j_results: list,
    cypher_queries: list,
    complexity: str,
    use_literature: bool,
    literature_result: str,
    rigor: bool,
    pre_final_answer: str = "",
    chat_history: list[dict] | None = None,
) -> str:
    """Wrap run_plan_confirm under the request lock. Returns the formatted answer JSON string.

    ``pre_final_answer`` is forwarded to the format/reasoning agent where it
    is appended to the user input (prior conversation context for follow-ups).
    ``chat_history`` is an alternative — if provided and ``pre_final_answer``
    is empty, the last 4 turns are built into a context string downstream.
    """
    with _request_lock:
        _main_module.RIGOR_MODE = rigor
        return run_plan_confirm(
            question=question,
            neo4j_results=neo4j_results,
            cypher_queries=cypher_queries,
            complexity=complexity,
            use_literature=use_literature,
            literature_result=literature_result,
            rigor=rigor,
            pre_final_answer=pre_final_answer,
            chat_history=chat_history,
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_model=dict)
async def root():
    """Root endpoint with API information"""
    return {
        "service": "PlannerAgent API",
        "version": "2.1.0",
        "status": "running",
        "endpoints": {
            "chat_start": "POST /chat/start — start multi-turn dialogue (returns plan for review; auto_confirm=true to run in one shot)",
            "chat_message": "POST /chat/message — follow-up; smart-routed (follow_up reuses stored data, new_query returns pending plan)",
            "chat_plan_confirm": "POST /chat/plan/confirm — confirm (and optionally revise) pending new_query plan from /chat/message",
            "chat_revise": "POST /chat/revise — revise the most recent confirmed plan in the session, auto-confirm",
            "chat_history": "GET /chat/history?session_id=X — get conversation history",
            "chat_end": "DELETE /chat/end?session_id=X — end session",
            "plan_start": "POST /plan/start — start interactive plan session (manual confirm)",
            "plan_revise": "POST /plan/revise — revise plan with user prompt",
            "plan_confirm": "POST /plan/confirm — confirm plan and get final answer",
            "functional_data": "POST /functional-data — resolve NL question to functional data API params (returns endpoint + URL + params only)",
            "health": "GET /health — health check",
            "docs": "GET /docs — interactive API documentation",
        },
    }


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint"""
    uptime = time.time() - SERVER_START_TIME
    return HealthResponse(
        status="healthy",
        message="Server is running and ready to accept requests",
        uptime_seconds=uptime,
    )


# ---------------------------------------------------------------------------
# Plan confirmation endpoints
# ---------------------------------------------------------------------------

@app.post("/plan/start", response_model=PlanResponse)
async def plan_start(request: PlanStartRequest):
    """Start an interactive plan session.

    Runs plan_query + translate_plan + execute_plan, stores the session,
    and returns the plan as Markdown for the user to review.
    """
    start_time = time.time()

    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    raw_question = request.question.strip()
    rigor = request.rigor
    use_lit = request.use_literature
    logger.info(f"[/plan/start] Received (rigor={rigor}, literature={use_lit}): {raw_question[:100]}...")

    _cleanup_expired_sessions()

    question = clean_user_question(raw_question)

    def _do_plan():
        with _request_lock:
            return run_plan_start(question, use_literature=use_lit)

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _do_plan)
    except Exception as e:
        logger.error(f"[/plan/start] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Planning failed: {e}")

    session_id = uuid.uuid4().hex[:12]
    session = PlanSession(
        session_id=session_id,
        original_question=question,
        rigor=rigor,
        current_plan=result["plan"],
        neo4j_results=result["neo4j_results"],
        cypher_queries=result["cypher_queries"],
        complexity=result["complexity"],
        use_literature=result.get("use_literature", False),
        literature_result=result.get("literature_result", ""),
    )

    plan_md = format_plan_as_markdown(
        question, result["plan"], result["neo4j_results"],
        use_literature=session.use_literature,
        literature_result=session.literature_result,
    )
    session.chat_history.append({"role": "user", "content": question})
    session.chat_history.append({"role": "assistant", "content": plan_md})

    with _sessions_lock:
        _plan_sessions[session_id] = session
        _persist_plan_session(session)

    processing_time = (time.time() - start_time) * 1000
    logger.info(f"[/plan/start] Session {session_id} created with {len(result['plan'].get('steps', []))} steps, literature={session.use_literature}")

    _log_plan_event(session_id, "plan_start", {
        "raw_question": raw_question,
        "clean_question": question,
        "plan": result["plan"],
        "plan_markdown": plan_md,
        "neo4j_results_count": len(result.get("neo4j_results", [])),
        "cypher_queries": result.get("cypher_queries", []),
        "complexity": result.get("complexity", "simple"),
        "use_literature": session.use_literature,
        "has_literature": bool(result.get("literature_result")),
        "rigor": rigor,
        "processing_time_ms": round(processing_time, 1),
    })

    return PlanResponse(
        session_id=session_id,
        plan_markdown=plan_md,
        plan_json=result["plan"],
        use_literature=session.use_literature,
    )


@app.post("/plan/revise", response_model=PlanResponse)
async def plan_revise(request: PlanReviseRequest):
    """Revise the current plan based on a user prompt.

    Re-runs plan_query + translate + execute with the revision context
    and returns the updated plan.
    """
    start_time = time.time()

    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Revision prompt cannot be empty")

    session = _get_session(request.session_id)
    prompt = request.prompt.strip()
    logger.info(f"[/plan/revise] Session {session.session_id}: {prompt[:100]}...")

    session.chat_history.append({"role": "user", "content": prompt})

    def _do_revise():
        with _request_lock:
            return run_plan_revise(
                original_question=session.original_question,
                current_plan=session.current_plan,
                neo4j_results=session.neo4j_results,
                user_prompt=prompt,
                use_literature=session.use_literature,
                literature_result=session.literature_result,
            )

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _do_revise)
    except Exception as e:
        logger.error(f"[/plan/revise] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Revision failed: {e}")

    # Always update session state — even soft errors return a valid plan
    session.current_plan = result["plan"]
    session.neo4j_results = result["neo4j_results"]
    session.cypher_queries = result["cypher_queries"]
    session.complexity = result["complexity"]
    session.use_literature = result.get("use_literature", session.use_literature)
    session.literature_result = result.get("literature_result", session.literature_result)

    error_msg = result.get("error")

    plan_md = format_plan_as_markdown(
        session.original_question, result["plan"], result["neo4j_results"],
        use_literature=session.use_literature,
        literature_result=session.literature_result,
    )
    if error_msg:
        plan_md = f"> **Note:** {error_msg}\n\n{plan_md}"

    session.chat_history.append({"role": "assistant", "content": plan_md})

    with _sessions_lock:
        _persist_plan_session(session)

    processing_time = (time.time() - start_time) * 1000
    logger.info(f"[/plan/revise] Session {session.session_id} revised to {len(result['plan'].get('steps', []))} steps, literature={session.use_literature}")

    _log_plan_event(session.session_id, "plan_revise", {
        "question": session.original_question,
        "user_prompt": prompt,
        "plan": result["plan"],
        "plan_markdown": plan_md,
        "neo4j_results_count": len(result.get("neo4j_results", [])),
        "cypher_queries": result.get("cypher_queries", []),
        "complexity": result.get("complexity", "simple"),
        "use_literature": session.use_literature,
        "has_literature": bool(session.literature_result),
        "error": error_msg,
        "processing_time_ms": round(processing_time, 1),
    })

    return PlanResponse(
        session_id=session.session_id,
        plan_markdown=plan_md,
        plan_json=result["plan"],
        use_literature=session.use_literature,
    )


@app.post("/plan/confirm", response_model=PlanConfirmResponse)
async def plan_confirm(request: PlanConfirmRequest):
    """Confirm the current plan and execute the format/reasoning pipeline.

    Uses the already-retrieved neo4j_results from the session — no new
    database queries are executed.
    """
    session = _get_session(request.session_id)
    logger.info(f"[/plan/confirm] Session {session.session_id} confirmed, running final pipeline...")

    start_time = time.time()

    def _do_confirm():
        with _request_lock:
            return run_plan_confirm(
                question=session.original_question,
                neo4j_results=session.neo4j_results,
                cypher_queries=session.cypher_queries,
                complexity=session.complexity,
                use_literature=session.use_literature,
                literature_result=session.literature_result,
                rigor=session.rigor,
            )

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _do_confirm)
    except Exception as e:
        logger.error(f"[/plan/confirm] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Execution failed: {e}")

    processing_time = (time.time() - start_time) * 1000
    cleaned = clean_response_json(response)
    md = extract_markdown(response)

    # Extract cypher queries and follow-up questions from the response JSON
    _confirm_cypher = []
    _confirm_follow_ups = []
    _confirm_reasoning = ""
    try:
        _resp_data = json.loads(cleaned) if isinstance(cleaned, str) else cleaned
        _resp_text = _resp_data.get("text", {})
        if isinstance(_resp_text, str):
            _resp_text = json.loads(_resp_text)
        _confirm_cypher = _resp_text.get("cypher", [])
        _confirm_follow_ups = _resp_text.get("follow_up_questions", [])
        _confirm_reasoning = _resp_text.get("reasoning_trace", "")
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    _log_plan_event(session.session_id, "plan_confirm", {
        "question": session.original_question,
        "plan": session.current_plan,
        "cypher_queries": session.cypher_queries,
        "final_answer_cypher": _confirm_cypher,
        "final_answer_markdown": md[:5000],
        "reasoning_trace": _confirm_reasoning[:3000] if _confirm_reasoning else "",
        "follow_up_questions": _confirm_follow_ups,
        "neo4j_results_count": len(session.neo4j_results),
        "complexity": session.complexity,
        "use_literature": session.use_literature,
        "has_literature": bool(session.literature_result),
        "rigor": session.rigor,
        "chat_history": session.chat_history,
        "processing_time_ms": round(processing_time, 1),
    })

    # Clean up the session after successful execution
    with _sessions_lock:
        _plan_sessions.pop(session.session_id, None)
        _delete_plan_session_row(session.session_id)

    logger.info(f"[/plan/confirm] Done in {processing_time:.0f}ms")

    return PlanConfirmResponse(answer=cleaned, answer_markdown=md, processing_time_ms=processing_time)


# ---------------------------------------------------------------------------
# Multi-turn chat endpoints
# ---------------------------------------------------------------------------

@app.post("/chat/start", response_model=ChatResponse)
async def chat_start(request: ChatStartRequest):
    """Start a new multi-turn chat session.

    Each new_query round runs ``run_plan_start`` + ``run_plan_confirm`` (same
    foundation as ``/plan/*``). The last plan is stashed on the session so the
    user can call ``/chat/revise`` to refine it before asking a new question.
    """
    start_time = time.time()

    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    question = request.question.strip()
    rigor = request.rigor
    use_lit = request.use_literature
    auto_confirm = request.auto_confirm
    logger.info(
        f"[/chat/start] New session (rigor={rigor}, literature={use_lit}, "
        f"auto_confirm={auto_confirm}): {question[:100]}..."
    )

    _cleanup_expired_chat_sessions()

    # 1. Plan
    loop = asyncio.get_running_loop()
    try:
        plan_result = await loop.run_in_executor(None, _run_plan_pipeline, question, use_lit)
    except Exception as e:
        logger.error(f"[/chat/start] plan error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Planning failed: {e}")

    interpreted = plan_result.get("interpreted_question", question)
    plan = plan_result["plan"]
    neo4j_results = plan_result["neo4j_results"]
    cypher_queries = plan_result["cypher_queries"]
    complexity = plan_result.get("complexity", "simple")
    literature_result = plan_result.get("literature_result", "")
    plan_md = format_plan_as_markdown(
        interpreted, plan, neo4j_results,
        use_literature=use_lit, literature_result=literature_result,
    )

    session_id = uuid.uuid4().hex[:12]

    if not auto_confirm:
        # Plan-review mode: stash a PlanSession, return new_query_pending.
        # The user must call /chat/plan/confirm (with the pending id) to run
        # the format/reasoning pipeline. Round-1 turns are persisted immediately below
        # (see session.history.append calls).
        pending_plan_session_id = uuid.uuid4().hex[:12]
        plan_session = PlanSession(
            session_id=pending_plan_session_id,
            original_question=interpreted,
            rigor=rigor,
            current_plan=plan,
            neo4j_results=neo4j_results,
            cypher_queries=cypher_queries,
            complexity=complexity,
            use_literature=use_lit,
            literature_result=literature_result,
        )
        with _sessions_lock:
            _plan_sessions[pending_plan_session_id] = plan_session
            _persist_plan_session(plan_session)

        session = ChatSession(
            session_id=session_id,
            rigor=rigor,
            use_literature=use_lit,
            pending_question=question,
            pending_plan_session_id=pending_plan_session_id,
        )
        # Persist round-1 turns immediately so round 2 sees prior context
        # even if the client never calls /chat/plan/confirm. The placeholder
        # assistant content is the plan markdown (entities + plan summary);
        # /chat/plan/confirm upgrades it to the final answer in place.
        session.history.append({"role": "user", "content": question})
        session.history.append({"role": "assistant", "content": plan_md})
        with _chat_sessions_lock:
            _chat_sessions[session_id] = session
            _persist_chat_session(session)

        processing_time = (time.time() - start_time) * 1000
        logger.info(
            f"[/chat/start] Session {session_id} pending plan "
            f"{pending_plan_session_id} in {processing_time:.0f}ms"
        )

        _log_plan_event(f"chat_{session_id}", "chat_start", {
            "question": question,
            "interpreted_question": interpreted,
            "plan_type": plan.get("plan_type"),
            "plan_markdown": plan_md[:3000],
            "route": "new_query_pending",
            "pending_plan_session_id": pending_plan_session_id,
            "rigor": rigor,
            "use_literature": use_lit,
            "processing_time_ms": round(processing_time, 1),
        })

        return ChatResponse(
            session_id=session_id,
            answer="",
            answer_markdown="",
            route="new_query_pending",
            round=1,
            plan_markdown=plan_md,
            plan_json=plan,
            pending_plan_session_id=pending_plan_session_id,
            processing_time_ms=processing_time,
        )

    # Auto-confirm mode (opt-in): run format/reasoning and return the answer.
    try:
        response = await loop.run_in_executor(
            None, _run_confirm,
            interpreted, neo4j_results, cypher_queries, complexity,
            use_lit, literature_result, rigor,
        )
    except Exception as e:
        logger.error(f"[/chat/start] confirm error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Confirm failed: {e}")

    cleaned = clean_response_json(response)
    md = extract_markdown(response)

    session = ChatSession(
        session_id=session_id,
        rigor=rigor,
        use_literature=use_lit,
        last_question=interpreted,
        last_plan=plan,
        last_neo4j_results=neo4j_results,
        last_cypher_queries=cypher_queries,
        last_complexity=complexity,
        last_literature_result=literature_result,
    )
    session.history.append({"role": "user", "content": question})
    session.history.append({"role": "assistant", "content": md})

    with _chat_sessions_lock:
        _chat_sessions[session_id] = session
        _persist_chat_session(session)

    processing_time = (time.time() - start_time) * 1000
    logger.info(f"[/chat/start] Session {session_id} done in {processing_time:.0f}ms")

    _log_plan_event(f"chat_{session_id}", "chat_start", {
        "question": question,
        "interpreted_question": interpreted,
        "plan_type": plan.get("plan_type"),
        "plan_markdown": plan_md[:3000],
        "answer_markdown": md[:3000],
        "rigor": rigor,
        "use_literature": use_lit,
        "processing_time_ms": round(processing_time, 1),
    })

    return ChatResponse(
        session_id=session_id,
        answer=cleaned,
        answer_markdown=md,
        route="new_query",
        round=1,
        plan_markdown=plan_md,
        plan_json=plan,
        processing_time_ms=processing_time,
    )


@app.post("/chat/message", response_model=ChatResponse)
async def chat_message(request: ChatMessageRequest):
    """Send a follow-up message in an existing chat session.

    Binary smart routing:

    - ``follow_up``: Claude decides this is a continuation of the prior turn.
      Reuses the session's stored ``last_neo4j_results`` + ``last_cypher_queries``
      and re-runs only the rigor-format / rigor-reasoning agent with the new
      question. No new database queries. The full dialogue history is passed
      as prior-conversation context (compressed via Haiku if too long — the
      response sets ``history_compressed=True`` so the frontend can notify).

    - ``new_query``: Claude decides this is a genuinely new question. Runs
      ``run_plan_start`` with dialogue history as planner context (text only,
      no raw retrieved data) and returns the plan for user review. Response
      carries ``route='new_query_pending'`` and a ``pending_plan_session_id``.
      The client must call ``/chat/plan/confirm`` to complete the round.
    """
    start_time = time.time()

    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    session = _get_chat_session(request.session_id)
    question = request.question.strip()
    current_round = len(session.history) // 2 + 1

    logger.info(f"[/chat/message] Session {session.session_id} round {current_round}: {question[:100]}...")

    loop = asyncio.get_running_loop()
    route = await loop.run_in_executor(None, _classify_followup, session.history, question)
    logger.info(f"[/chat/message] Route: {route}")

    plan_md = ""
    plan_json: Optional[dict] = None
    history_compressed = False
    pending_plan_session_id: Optional[str] = None
    cleaned = ""
    md = ""

    if route == "follow_up":
        # Reuse stored session data — no new DB queries. Re-run just the
        # rigor-format or rigor-reasoning agent with the new question plus the
        # full dialogue history as pre_final_answer (compressed if too long).
        if not session.last_neo4j_results and not session.last_cypher_queries:
            # Edge case: first turn was routed as follow_up (shouldn't happen
            # since /chat/start creates stored state). Fall through to new_query.
            logger.info(f"[/chat/message] No stored context for follow_up — promoting to new_query")
            route = "new_query"
        else:
            history_context, history_compressed = _build_history_context(session.history)
            try:
                response = await loop.run_in_executor(
                    None, _run_confirm,
                    question,
                    session.last_neo4j_results,
                    session.last_cypher_queries,
                    session.last_complexity,
                    session.use_literature,
                    session.last_literature_result,
                    session.rigor,
                    history_context,   # pre_final_answer
                    None,              # no chat_history planner context
                )
                cleaned = clean_response_json(response)
                md = extract_markdown(response)
                # NOTE: session.last_* fields are NOT updated — same retrieved
                # data stays active for subsequent follow-ups in this thread.
                _check_text = md or cleaned
                _is_tool = _answer_is_tool_output(_check_text)
                logger.info(
                    f"[/chat/message] follow_up guard: md_len={len(md)} "
                    f"cleaned_len={len(cleaned)} is_tool_output={_is_tool} "
                    f"md_preview={repr(_check_text[:120])}"
                )
                if _is_tool:
                    logger.warning(
                        "[/chat/message] follow_up answer looks like raw tool "
                        "output — promoting to new_query"
                    )
                    route = "new_query"
            except Exception as e:
                logger.error(f"[/chat/message] follow_up error: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Follow-up failed: {e}")

    if route == "new_query":
        # Run plan + create PlanSession. Plan is NOT auto-confirmed — the user
        # reviews it and calls /chat/plan/confirm (optionally with revisions).
        try:
            plan_result = await loop.run_in_executor(
                None, _run_plan_pipeline, question, session.use_literature, session.history,
            )
            interpreted = plan_result.get("interpreted_question", question)
            plan = plan_result["plan"]
            neo4j_results = plan_result["neo4j_results"]
            cypher_queries = plan_result["cypher_queries"]
            complexity = plan_result.get("complexity", "simple")
            use_lit_round = plan_result.get("use_literature", session.use_literature)
            literature_result = plan_result.get("literature_result", "")

            # Reuse existing PlanSession infrastructure so /chat/plan/confirm
            # can look up the pending plan just like /plan/confirm does.
            pending_plan_session_id = uuid.uuid4().hex[:12]
            plan_session = PlanSession(
                session_id=pending_plan_session_id,
                original_question=interpreted,
                rigor=session.rigor,
                current_plan=plan,
                neo4j_results=neo4j_results,
                cypher_queries=cypher_queries,
                complexity=complexity,
                use_literature=use_lit_round,
                literature_result=literature_result,
            )
            with _sessions_lock:
                _plan_sessions[pending_plan_session_id] = plan_session
                _persist_plan_session(plan_session)

            # Stash pending state on the chat session (cleared on confirm)
            session.pending_question = question
            session.pending_plan_session_id = pending_plan_session_id

            plan_md = format_plan_as_markdown(
                interpreted, plan, neo4j_results,
                use_literature=use_lit_round, literature_result=literature_result,
            )
            plan_json = plan
            # Persist round-N placeholder turns immediately so round N+1 sees
            # this question in history even if /chat/plan/confirm is never called.
            # /chat/plan/confirm upgrades the assistant turn to the final answer.
            session.history.append({"role": "user", "content": question})
            session.history.append({"role": "assistant", "content": plan_md})
            session.history = _trim_history(session.history)
            with _chat_sessions_lock:
                _persist_chat_session(session)
            route = "new_query_pending"
        except Exception as e:
            logger.error(f"[/chat/message] plan error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Planning failed: {e}")

    # Only append to history for completed rounds. For new_query_pending the
    # placeholder turns were already persisted above; /chat/plan/confirm
    # upgrades them in place with the final answer.
    if route != "new_query_pending":
        session.history.append({"role": "user", "content": question})
        session.history.append({"role": "assistant", "content": md})
        session.history = _trim_history(session.history)
        with _chat_sessions_lock:
            _persist_chat_session(session)

    processing_time = (time.time() - start_time) * 1000
    logger.info(
        f"[/chat/message] Session {session.session_id} round {current_round} "
        f"done ({route}, compressed={history_compressed}) in {processing_time:.0f}ms"
    )

    _log_plan_event(f"chat_{session.session_id}", "chat_message", {
        "question": question,
        "route": route,
        "round": current_round,
        "plan_markdown": plan_md[:3000],
        "answer_markdown": md[:3000],
        "history_compressed": history_compressed,
        "pending_plan_session_id": pending_plan_session_id,
        "processing_time_ms": round(processing_time, 1),
    })

    return ChatResponse(
        session_id=session.session_id,
        answer=cleaned,
        answer_markdown=md,
        route=route,
        round=current_round,
        plan_markdown=plan_md,
        plan_json=plan_json,
        pending_plan_session_id=pending_plan_session_id,
        history_compressed=history_compressed,
        processing_time_ms=processing_time,
    )


@app.post("/chat/revise", response_model=ChatResponse)
async def chat_revise(request: ChatReviseRequest):
    """Revise the most recent new_query plan in the chat session, then auto-confirm.

    Lets users refine a plan (e.g. "also add QTL SNPs", "drop GO terms") without
    starting a new question. The revised plan is auto-confirmed and the updated
    answer replaces the last assistant turn in history.
    """
    start_time = time.time()

    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Revision prompt cannot be empty")

    session = _get_chat_session(request.session_id)
    if not session.last_plan or not session.last_question:
        raise HTTPException(status_code=409, detail="No prior plan to revise in this session")

    prompt = request.prompt.strip()
    current_round = len(session.history) // 2  # revise updates the last round, not a new one
    logger.info(f"[/chat/revise] Session {session.session_id} revising round {current_round}: {prompt[:100]}...")

    loop = asyncio.get_running_loop()

    try:
        rev = await loop.run_in_executor(
            None, _run_plan_revise,
            session.last_question,
            session.last_plan,
            session.last_neo4j_results,
            prompt,
            session.use_literature,
            session.last_literature_result,
        )
    except Exception as e:
        logger.error(f"[/chat/revise] revise error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Revision failed: {e}")

    # Update session with revised plan state
    session.last_plan = rev["plan"]
    session.last_neo4j_results = rev["neo4j_results"]
    session.last_cypher_queries = rev["cypher_queries"]
    session.last_complexity = rev.get("complexity", session.last_complexity)
    session.use_literature = rev.get("use_literature", session.use_literature)
    session.last_literature_result = rev.get("literature_result", session.last_literature_result)

    plan_md = format_plan_as_markdown(
        session.last_question, session.last_plan, session.last_neo4j_results,
        use_literature=session.use_literature, literature_result=session.last_literature_result,
    )
    err = rev.get("error")
    if err:
        plan_md = f"> **Note:** {err}\n\n{plan_md}"

    # Auto-confirm
    try:
        response = await loop.run_in_executor(
            None, _run_confirm,
            session.last_question, session.last_neo4j_results, session.last_cypher_queries,
            session.last_complexity, session.use_literature, session.last_literature_result,
            session.rigor,
        )
    except Exception as e:
        logger.error(f"[/chat/revise] confirm error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Confirm failed: {e}")

    cleaned = clean_response_json(response)
    md = extract_markdown(response)

    # Replace the last assistant turn with the revised answer (don't add a new round)
    if session.history and session.history[-1].get("role") == "assistant":
        session.history[-1] = {"role": "assistant", "content": md}
    else:
        session.history.append({"role": "user", "content": f"[revision] {prompt}"})
        session.history.append({"role": "assistant", "content": md})
    session.history = _trim_history(session.history)

    with _chat_sessions_lock:
        _persist_chat_session(session)

    processing_time = (time.time() - start_time) * 1000
    logger.info(f"[/chat/revise] Session {session.session_id} done in {processing_time:.0f}ms")

    _log_plan_event(f"chat_{session.session_id}", "chat_revise", {
        "prompt": prompt,
        "plan_markdown": plan_md[:3000],
        "answer_markdown": md[:3000],
        "processing_time_ms": round(processing_time, 1),
    })

    return ChatResponse(
        session_id=session.session_id,
        answer=cleaned,
        answer_markdown=md,
        route="new_query",
        round=current_round,
        plan_markdown=plan_md,
        plan_json=session.last_plan,
        processing_time_ms=processing_time,
    )


@app.post("/chat/plan/confirm", response_model=ChatResponse)
async def chat_plan_confirm(request: ChatPlanConfirmRequest):
    """Confirm (and optionally revise) the pending plan from the last
    ``/chat/message`` ``new_query_pending`` response. Executes the
    format/reasoning pipeline using the stored retrieved data, updates the
    chat session's history and ``last_*`` fields, and returns the final answer.

    If ``revision_prompt`` is provided, the plan is revised via
    ``run_plan_revise`` before confirmation.
    """
    start_time = time.time()

    chat_session = _get_chat_session(request.chat_session_id)
    plan_session = _get_session(request.plan_session_id)

    if chat_session.pending_plan_session_id != request.plan_session_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"plan_session_id '{request.plan_session_id}' is not the pending "
                f"plan for chat session '{chat_session.session_id}'"
            ),
        )

    logger.info(
        f"[/chat/plan/confirm] chat={chat_session.session_id} "
        f"plan={plan_session.session_id} (revise={bool(request.revision_prompt)})"
    )

    loop = asyncio.get_running_loop()

    # Optional revision before confirmation
    if request.revision_prompt and request.revision_prompt.strip():
        prompt = request.revision_prompt.strip()
        try:
            rev = await loop.run_in_executor(
                None, _run_plan_revise,
                plan_session.original_question,
                plan_session.current_plan,
                plan_session.neo4j_results,
                prompt,
                plan_session.use_literature,
                plan_session.literature_result,
            )
        except Exception as e:
            logger.error(f"[/chat/plan/confirm] revise error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Revision failed: {e}")

        plan_session.current_plan = rev["plan"]
        plan_session.neo4j_results = rev["neo4j_results"]
        plan_session.cypher_queries = rev["cypher_queries"]
        plan_session.complexity = rev.get("complexity", plan_session.complexity)
        plan_session.use_literature = rev.get("use_literature", plan_session.use_literature)
        plan_session.literature_result = rev.get("literature_result", plan_session.literature_result)
        with _sessions_lock:
            _persist_plan_session(plan_session)

    # Confirm — run the format/reasoning pipeline. Chat history provides
    # prior-conversation context to the format/reasoning agent.
    try:
        response = await loop.run_in_executor(
            None, _run_confirm,
            plan_session.original_question,
            plan_session.neo4j_results,
            plan_session.cypher_queries,
            plan_session.complexity,
            plan_session.use_literature,
            plan_session.literature_result,
            plan_session.rigor,
            "",                           # pre_final_answer — let chat_history build it
            chat_session.history,         # chat_history
        )
    except Exception as e:
        logger.error(f"[/chat/plan/confirm] confirm error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Confirm failed: {e}")

    cleaned = clean_response_json(response)
    md = extract_markdown(response)

    plan_md = format_plan_as_markdown(
        plan_session.original_question,
        plan_session.current_plan,
        plan_session.neo4j_results,
        use_literature=plan_session.use_literature,
        literature_result=plan_session.literature_result,
    )

    # Upgrade the placeholder round-1 turns appended at /chat/start (or
    # append a fresh pair on the legacy auto_confirm path where no
    # placeholder exists).
    pending_q = chat_session.pending_question or plan_session.original_question
    h = chat_session.history
    if (
        len(h) >= 2
        and h[-2].get("role") == "user"
        and h[-2].get("content") == pending_q
        and h[-1].get("role") == "assistant"
    ):
        # Placeholder exists from /chat/start — replace the assistant turn
        h[-1] = {"role": "assistant", "content": md}
    else:
        h.append({"role": "user", "content": pending_q})
        h.append({"role": "assistant", "content": md})
    chat_session.history = _trim_history(h)

    # Update chat session's stored plan state for future follow_up rounds
    chat_session.last_question = plan_session.original_question
    chat_session.last_plan = plan_session.current_plan
    chat_session.last_neo4j_results = plan_session.neo4j_results
    chat_session.last_cypher_queries = plan_session.cypher_queries
    chat_session.last_complexity = plan_session.complexity
    chat_session.use_literature = plan_session.use_literature
    chat_session.last_literature_result = plan_session.literature_result

    # Clear pending flags and delete the PlanSession
    chat_session.pending_question = ""
    chat_session.pending_plan_session_id = ""
    with _chat_sessions_lock:
        _persist_chat_session(chat_session)
    with _sessions_lock:
        _plan_sessions.pop(plan_session.session_id, None)
        _delete_plan_session_row(plan_session.session_id)

    current_round = len(chat_session.history) // 2
    processing_time = (time.time() - start_time) * 1000
    logger.info(
        f"[/chat/plan/confirm] chat={chat_session.session_id} "
        f"round {current_round} done in {processing_time:.0f}ms"
    )

    _log_plan_event(f"chat_{chat_session.session_id}", "chat_plan_confirm", {
        "plan_session_id": plan_session.session_id,
        "revision_prompt": request.revision_prompt,
        "question": pending_q,
        "plan_markdown": plan_md[:3000],
        "answer_markdown": md[:3000],
        "processing_time_ms": round(processing_time, 1),
    })

    return ChatResponse(
        session_id=chat_session.session_id,
        answer=cleaned,
        answer_markdown=md,
        route="new_query",
        round=current_round,
        plan_markdown=plan_md,
        plan_json=plan_session.current_plan,
        processing_time_ms=processing_time,
    )


@app.get("/chat/history", response_model=ChatHistoryResponse)
async def chat_history(session_id: str):
    """Retrieve conversation history for a chat session."""
    session = _get_chat_session(session_id)
    return ChatHistoryResponse(
        session_id=session.session_id,
        rounds=len(session.history) // 2,
        history=session.history,
    )


@app.delete("/chat/end")
async def chat_end(session_id: str):
    """End a chat session and free memory."""
    with _chat_sessions_lock:
        removed = _chat_sessions.pop(session_id, None)
        if removed is not None:
            _delete_chat_session_row(session_id)
    if removed is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    logger.info(f"[/chat/end] Session {session_id} ended ({len(removed.history) // 2} rounds)")
    return {"status": "ended", "session_id": session_id, "rounds": len(removed.history) // 2}


# ---------------------------------------------------------------------------
# Functional Data API — parameters-only endpoint
# ---------------------------------------------------------------------------

@app.post("/functional-data", response_model=FunctionalDataParamsResponse)
async def functional_data_params(request: FunctionalDataRequest):
    """Resolve a natural language question to a Functional Data API call.

    Returns only the endpoint, full URL, and query parameters that would be
    sent to the functional data API — not the data itself.
    """
    from urllib.parse import urlencode
    import functional_data_client as _fdc

    prior_donor_ids: list[str] | None = request.donor_ids or None

    loop = asyncio.get_event_loop()

    def _resolve():
        sel = _fdc.extract_endpoint_and_params(request.question, prior_donor_ids)
        ok, err = _fdc._validate_selection(sel)
        if not ok:
            raise ValueError(f"Invalid endpoint/params: {err}")
        return sel

    try:
        sel = await loop.run_in_executor(None, _resolve)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    endpoint: str = sel["endpoint"]
    params: dict = sel.get("params") or {}
    qs = urlencode(params)
    full_url = f"{_fdc.FUNCTIONAL_BASE_URL}{endpoint}{'?' + qs if qs else ''}"

    return FunctionalDataParamsResponse(
        endpoint=endpoint,
        url=full_url,
        params=params,
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler for unhandled errors"""
    logger.error(f"Unhandled exception: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Local development entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    import os

    port = 8080

    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"Error: Invalid port '{sys.argv[1]}'. Using default port {port}")
    elif "PORT" in os.environ:
        try:
            port = int(os.getenv("PORT"))
        except ValueError:
            print(f"Error: Invalid PORT env var. Using default port {port}")

    print("\n" + "=" * 60)
    print("Starting PlannerAgent API Server (Development Mode)")
    print("=" * 60)
    print(f"\nServer will be available at:")
    print(f"  - http://localhost:{port}")
    print(f"  - API docs: http://localhost:{port}/docs")
    print(f"  - Health check: http://localhost:{port}/health")
    print(f"\nEndpoints:")
    print(f"  POST   /chat/start         — start dialogue (returns pending plan; auto_confirm=true to run in one shot)")
    print(f"  POST   /chat/message       — follow-up (follow_up reuses data; new_query returns pending plan)")
    print(f"  POST   /chat/plan/confirm  — confirm/revise pending plan from /chat/message")
    print(f"  POST   /chat/revise        — revise last confirmed plan, auto-confirm")
    print(f"  GET    /chat/history       — get conversation history")
    print(f"  DELETE /chat/end           — end chat session")
    print(f"  POST   /plan/start    — interactive plan session (manual confirm)")
    print(f"  POST   /plan/revise   — revise plan with user prompt")
    print(f"  POST   /plan/confirm  — confirm plan and get final answer")
    print("\nPress Ctrl+C to stop the server")
    print("=" * 60 + "\n")

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
