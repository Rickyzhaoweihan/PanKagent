"""Query Planner — end-to-end pipeline for planning, translating, combining,
and executing Cypher queries against the PanKgraph Neo4j API.

Public API
----------
- ``plan_query(question)``          → plan dict (from Claude)
- ``translate_plan(plan)``          → plan dict with ``cypher`` populated on each step
- ``execute_plan(plan)``            → list[dict] of Neo4j results
- ``run_query_planner_pipeline(q)`` → list[dict] end-to-end
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from copy import deepcopy
from queue import Queue
from _thread import start_new_thread
from typing import Callable, List, Optional

import anthropic
import requests

# ---------------------------------------------------------------------------
# Local imports (relative to skills/query-planner/scripts/)
# ---------------------------------------------------------------------------
from qp_prompts import QUERY_PLANNER_PROMPT, QUERY_PLANNER_REVISION_PROMPT
from qp_cypher_combiner import build_executable_queries

# ---------------------------------------------------------------------------
# Structured streaming events
# ---------------------------------------------------------------------------
_repo_root_for_events = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _repo_root_for_events not in sys.path:
    sys.path.insert(0, _repo_root_for_events)
from stream_events import emit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NEO4J_BOLT_URI = os.environ.get("NEO4J_BOLT_URI", "bolt://localhost:8687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password")
NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE", "pankgraph")

CLAUDE_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Neo4j driver singleton — one driver shared across all threads, never
# recreated per query.  Creating a new driver per query causes connection-pool
# thread accumulation that leads to OOM after extended uptime.
# ---------------------------------------------------------------------------
_neo4j_driver = None
_neo4j_driver_lock = threading.Lock()


def _get_neo4j_driver():
    global _neo4j_driver
    if _neo4j_driver is None:
        with _neo4j_driver_lock:
            if _neo4j_driver is None:
                from neo4j import GraphDatabase
                _neo4j_driver = GraphDatabase.driver(
                    NEO4J_BOLT_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
                )
    return _neo4j_driver

# ---------------------------------------------------------------------------
# Anthropic client (lazy singleton)
# ---------------------------------------------------------------------------

_anthropic_client: Optional[anthropic.Anthropic] = None


def _get_anthropic_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        try:
            # Fall back to config.py at the repo root
            repo_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..")
            )
            if repo_root not in sys.path:
                sys.path.insert(0, repo_root)
            import config
            api_key = getattr(config, "ANTHROPIC_API_KEY", None)
        except (ImportError, AttributeError):
            pass

    if not api_key or api_key == "YOUR_ANTHROPIC_API_KEY_HERE":
        raise EnvironmentError(
            "ANTHROPIC_API_KEY not set. Export it or set it in config.py"
        )

    _anthropic_client = anthropic.Anthropic(api_key=api_key.strip())
    return _anthropic_client


# ---------------------------------------------------------------------------
# Text2Cypher agent (lazy singleton)
# ---------------------------------------------------------------------------

_text2cypher_agent = None
_text2cypher_lock = threading.Lock()


def _get_text2cypher_agent():
    """Lazily initialise the vLLM-backed text2cypher agent (thread-safe)."""
    global _text2cypher_agent
    if _text2cypher_agent is not None:
        return _text2cypher_agent

    with _text2cypher_lock:
        # Double-check after acquiring lock
        if _text2cypher_agent is not None:
            return _text2cypher_agent

        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..")
        )

        # The agent expects this env var for schema resolution
        # Use the same path as PankBaseAgent/utils.py
        os.environ['NEO4J_SCHEMA_PATH'] = os.path.join(
            repo_root, "PankBaseAgent", "text_to_cypher", "data", "input", "neo4j_schema_ada.json"
        )

        # Ensure repo root is on sys.path so PankBaseAgent is importable as a package
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)

        # Import via the PankBaseAgent package (same as PankBaseAgent/utils.py)
        # This respects the relative imports inside text2cypher_agent.py
        from PankBaseAgent.text_to_cypher.src.text2cypher_agent import Text2CypherAgent

        _text2cypher_agent = Text2CypherAgent()
        return _text2cypher_agent


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> str:
    """Pull the first JSON object out of Claude's response."""
    text = text.strip()
    if text.startswith("{"):
        return text

    # Markdown code block
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Bare braces
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return text


# ---------------------------------------------------------------------------
# 0. interpret_question  —  Claude cleans up typos/grammar BEFORE planning
# ---------------------------------------------------------------------------

def interpret_question(question: str) -> str:
    """Send the raw user question to Claude for typo/grammar correction only.

    Returns the cleaned question string.  This is a lightweight call with a
    narrow prompt so it's fast and cheap — it runs before any planning or
    text2cypher work begins.
    """
    client = _get_anthropic_client()

    system = (
        "You are a question-cleaning assistant for a biomedical knowledge graph. "
        "Your ONLY job: fix typos, spelling errors, and grammar in the user's "
        "question. Output ONLY the corrected question — nothing else. "
        "Do NOT add detail, do NOT expand abbreviations (keep gene names as-is), "
        "do NOT answer the question. If the question is already correct, repeat it unchanged."
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        temperature=0.0,
        system=system,
        messages=[{"role": "user", "content": question}],
    )

    _in_tok = response.usage.input_tokens
    _out_tok = response.usage.output_tokens
    _cost_input = _in_tok * 5.0 / 1_000_000
    _cost_output = _out_tok * 25.0 / 1_000_000
    _cost_total = _cost_input + _cost_output
    emit("question_interpreted", {
        "raw_question": question[:300],
        "clean_question": response.content[0].text.strip()[:300],
        "input_tokens": _in_tok,
        "output_tokens": _out_tok,
        "cost_total_usd": round(_cost_total, 6),
    })

    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# 1. plan_query  —  Claude generates the plan JSON
# ---------------------------------------------------------------------------

def plan_query(question: str, chat_history_context: str = "") -> dict:
    """Call Claude with the QueryPlanner prompt and return a plan dict.

    The plan has the shape::

        {
            "plan_type": "chain" | "parallel",
            "reasoning": "...",
            "steps": [
                {"id": 1, "natural_language": "...", "join_var": "o", "depends_on": null},
                ...
            ]
        }

    If ``chat_history_context`` is provided, it is prepended as a priming
    exchange so the planner can resolve pronouns and entity references
    ("it", "those genes") against prior conversation turns.
    """
    client = _get_anthropic_client()

    system = (
        QUERY_PLANNER_PROMPT
        + "\n\nIMPORTANT: Your response MUST be a valid JSON object. "
        "Output ONLY the JSON, no additional text, no markdown code blocks."
    )

    messages = []
    if chat_history_context:
        messages.append({
            "role": "user",
            "content": (
                "[Prior conversation — use to resolve entity references in the new question]\n"
                f"{chat_history_context}\n\nNow plan queries for:"
            ),
        })
        messages.append({
            "role": "assistant",
            "content": "Understood. Planning queries for the new question.",
        })
    messages.append({"role": "user", "content": question})

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        temperature=0.2,
        system=system,
        messages=messages,
    )

    _in_tok = response.usage.input_tokens
    _out_tok = response.usage.output_tokens
    _cost_input = _in_tok * 5.0 / 1_000_000   # $5 / MTok
    _cost_output = _out_tok * 25.0 / 1_000_000  # $25 / MTok
    _cost_total = _cost_input + _cost_output
    emit("planner_claude_done", {
        "input_tokens": _in_tok,
        "output_tokens": _out_tok,
        "cost_input_usd": round(_cost_input, 6),
        "cost_output_usd": round(_cost_output, 6),
        "cost_total_usd": round(_cost_total, 6),
    })

    raw = response.content[0].text
    json_str = _extract_json(raw)

    plan = json.loads(json_str, strict=False)

    # Basic validation
    assert isinstance(plan, dict), "Plan must be a JSON object"
    assert "steps" in plan, "Plan must have 'steps'"
    assert len(plan["steps"]) > 0, "Plan must have at least one step"

    emit("plan_generated", {
        "question": question[:200],
        "plan_type": plan.get("plan_type", "unknown"),
        "reasoning": plan.get("reasoning", ""),
        "num_steps": len(plan["steps"]),
        "steps": [
            {
                "id": s["id"],
                "natural_language": s["natural_language"],
                "depends_on": s.get("depends_on"),
                "join_var": s.get("join_var"),
            }
            for s in plan["steps"]
        ],
    })

    return plan


def revise_plan_query(
    original_question: str,
    current_plan: dict,
    execution_summary: list[dict],
    user_prompt: str,
    use_literature: bool = False,
) -> dict:
    """Revise an existing plan based on the user's prompt.

    Args:
        original_question: The original NL question.
        current_plan: The current plan dict (with steps, plan_type, reasoning).
        execution_summary: Per-step summary like [{"id": 1, "cypher": "...", "records": 5}, ...].
        user_prompt: The user's revision instruction.
        use_literature: Whether literature search is currently enabled.

    Returns:
        A new plan dict (including ``use_literature`` bool), or a plan with
        ``plan_type == "error"`` if the revision is not feasible.
    """
    client = _get_anthropic_client()

    system = (
        QUERY_PLANNER_REVISION_PROMPT
        + "\n\n"
        + QUERY_PLANNER_PROMPT
        + "\n\nIMPORTANT: Your response MUST be a valid JSON object. "
        "Output ONLY the JSON, no additional text, no markdown code blocks."
    )

    steps_summary = []
    for step in current_plan.get("steps", []):
        sid = step["id"]
        exec_info = next((e for e in execution_summary if e["id"] == sid), {})
        cypher = exec_info.get("cypher", step.get("cypher", "(not translated)"))
        records = exec_info.get("records", "?")
        steps_summary.append(
            f"  Step {sid}: {step['natural_language']}\n"
            f"    Cypher: {cypher}\n"
            f"    Records returned: {records}"
        )

    lit_state = "enabled" if use_literature else "disabled"
    user_message = (
        f"[Original question]\n{original_question}\n\n"
        f"[Current plan — {current_plan.get('plan_type', 'unknown')}]\n"
        f"Reasoning: {current_plan.get('reasoning', '')}\n"
        f"Steps:\n" + "\n".join(steps_summary) + "\n\n"
        f"[Literature search: currently {lit_state}]\n\n"
        f"[User revision request]\n{user_prompt}\n\n"
        "Please output a revised JSON plan (including use_literature). "
        "Keep steps that are unchanged."
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        temperature=0.2,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    emit("plan_revision_claude_done", {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    })

    raw = response.content[0].text
    json_str = _extract_json(raw)
    plan = json.loads(json_str, strict=False)

    # Default use_literature to current state if the LLM didn't include it
    if "use_literature" not in plan:
        plan["use_literature"] = use_literature

    if plan.get("plan_type") == "error":
        emit("plan_revision_error", {"reasoning": plan.get("reasoning", "")})
        return plan

    assert isinstance(plan, dict), "Plan must be a JSON object"
    assert "steps" in plan, "Plan must have 'steps'"
    assert len(plan["steps"]) > 0, "Plan must have at least one step"

    emit("plan_revised", {
        "plan_type": plan.get("plan_type", "unknown"),
        "reasoning": plan.get("reasoning", ""),
        "num_steps": len(plan["steps"]),
        "use_literature": plan.get("use_literature", False),
    })

    return plan


# ---------------------------------------------------------------------------
# 2. translate_plan  —  vLLM text2cypher for every step (parallel)
# ---------------------------------------------------------------------------

def _translate_one_step(nl_query: str, q: Queue, step_id: int) -> None:
    """Worker: translate a single NL query → Cypher via the text2cypher agent."""
    try:
        agent = _get_text2cypher_agent()
        cypher = agent.respond(nl_query)

        # Run auto-fix for safety (LIMIT, etc.)
        try:
            from PankBaseAgent.text_to_cypher.src.cypher_validator import (
                validate_cypher, auto_fix_cypher,
            )
            validation = validate_cypher(cypher)
            fixed, fixes = auto_fix_cypher(cypher, validation)
            if fixes:
                cypher = fixed
        except ImportError:
            pass

        q.put((step_id, True, cypher))
    except Exception:
        q.put((step_id, False, traceback.format_exc()))


def translate_plan(plan: dict) -> dict:
    """Translate every step's ``natural_language`` to Cypher in parallel.

    Steps with ``"source": "hpap"`` or ``"source": "genomic"`` are skipped —
    they are executed via their respective NL-to-SQL pipelines, not text2cypher.

    Mutates *plan* in-place by adding a ``cypher`` key to each step.
    Returns the updated plan.
    """
    plan = deepcopy(plan)
    steps = plan["steps"]
    kg_steps = [s for s in steps if s.get("source") not in ("hpap", "genomic", "functional_data")]
    q: Queue = Queue()

    for step in kg_steps:
        start_new_thread(
            _translate_one_step,
            (step["natural_language"], q, step["id"]),
        )

    # Collect results for KG steps only
    results: dict[int, str] = {}
    deadline = time.time() + 120  # 2 min timeout
    while len(results) < len(kg_steps) and time.time() < deadline:
        time.sleep(0.2)
        while not q.empty():
            step_id, success, payload = q.get_nowait()
            if success:
                results[step_id] = payload
            else:
                emit("text2cypher_done", {
                    "step_id": step_id,
                    "success": False,
                    "error": payload,
                })
                results[step_id] = ""

    # Attach Cypher to each step (non-KG steps get empty cypher)
    for step in steps:
        if step.get("source") in ("hpap", "genomic", "functional_data"):
            step["cypher"] = ""
            continue
        cypher = results.get(step["id"], "")
        step["cypher"] = cypher
        emit("text2cypher_done", {
            "step_id": step["id"],
            "success": bool(cypher),
            "cypher": cypher,
        })

    return plan


# ---------------------------------------------------------------------------
# 3. execute_plan  —  build compound Cypher(s) + call Neo4j API
# ---------------------------------------------------------------------------

def _clean_cypher(cypher: str) -> str:
    """Normalise whitespace and quote characters for the Neo4j API."""
    cleaned = " ".join(cypher.split())
    cleaned = cleaned.replace('"', '"').replace("'", '"')
    return cleaned


def _execute_cypher(cypher: str, timeout: int = 60) -> dict:
    """Execute a Cypher query against the local Neo4j via Bolt and return structured JSON."""
    _MAX_RECORDS = 500  # hard cap — prevents runaway queries from exhausting memory

    try:
        driver = _get_neo4j_driver()
        with driver.session(database=NEO4J_DATABASE) as session:
            result = session.run(cypher)
            keys = list(result.keys())
            records = []
            for record in result:
                if len(records) >= _MAX_RECORDS:
                    break
                row = {}
                for key in keys:
                    val = record[key]
                    if isinstance(val, list):
                        row[key] = []
                        for item in val:
                            if hasattr(item, 'labels'):  # Node
                                row[key].append({
                                    "__type__": "node",
                                    "id": str(item.element_id),
                                    "element_id": item.element_id,
                                    "labels": list(item.labels),
                                    "properties": dict(item),
                                })
                            elif hasattr(item, 'type'):  # Relationship
                                row[key].append({
                                    "__type__": "relationship",
                                    "id": str(item.element_id),
                                    "element_id": item.element_id,
                                    "type": item.type,
                                    "start_node_element_id": item.start_node.element_id if hasattr(item, 'start_node') else "",
                                    "end_node_element_id": item.end_node.element_id if hasattr(item, 'start_node') else "",
                                    "properties": dict(item),
                                })
                            else:
                                row[key].append(item)
                    else:
                        row[key] = val
                records.append(row)
        return {"records": records, "keys": keys}
    except Exception as exc:
        return {"error": str(exc)[:2000], "query": cypher}


_HEAVY_RELS = re.compile(r'\b(OCR_peak_in|gene_activity_score_in)\b', re.IGNORECASE)


def _ensure_limit_before_collect(cypher: str, limit: int = 50) -> str:
    """Inject a LIMIT before collect() when needed.

    Two cases:
      1. OCR-related queries (OCR_peak_in, gene_activity_score_in) — ALWAYS
         get a LIMIT because these tables are massive in Neo4j.
      2. Unconstrained queries (no WHERE) — broad scans need a cap.

    Queries that already have a LIMIT are never modified.
    """
    if not cypher or re.search(r'\bLIMIT\s+\d+', cypher, re.IGNORECASE):
        return cypher

    is_heavy = bool(_HEAVY_RELS.search(cypher))

    if not is_heavy and re.search(r'\bWHERE\b', cypher, re.IGNORECASE):
        return cypher

    collect_match = re.search(r'\bWITH\s+collect\s*\(', cypher, re.IGNORECASE)
    if collect_match:
        node_vars = set(re.findall(r'\((\w+)(?::\w+)?[^)]*\)', cypher[:collect_match.start()]))
        rel_vars = set(re.findall(r'\[(\w+):\w+[^\]]*\]', cypher[:collect_match.start()]))
        all_vars = node_vars | rel_vars
        if all_vars:
            vars_str = ', '.join(sorted(all_vars))
            pos = collect_match.start()
            return cypher[:pos] + f"WITH {vars_str} LIMIT {limit}\n" + cypher[pos:]
    return cypher


def _execute_single_cypher_sync(cypher: str, index: int = 0) -> tuple[str, dict]:
    """Synchronously execute one Cypher query. Returns (cleaned_cypher, result_dict).

    Used by both the parallel executor (via the _execute_one_cypher thread wrapper)
    and the cross-source chain executor which runs steps serially.
    """
    cleaned = _clean_cypher(cypher)
    cleaned = _ensure_limit_before_collect(cleaned, limit=50)
    emit("cypher_executing", {
        "index": index + 1,
        "cypher": cleaned[:500],
        "length": len(cleaned),
    })

    neo4j_start = time.time()
    result = _execute_cypher(cleaned)
    elapsed = time.time() - neo4j_start

    has_error = "error" in result
    emit("cypher_result", {
        "index": index + 1,
        "elapsed_s": round(elapsed, 2),
        "success": not has_error,
        "error": result.get("error") if has_error else None,
    })

    return cleaned, result


def _execute_one_cypher(cypher: str, index: int, q: Queue) -> None:
    """Worker: execute a single Cypher query and put the result on *q*."""
    cleaned, result = _execute_single_cypher_sync(cypher, index)
    q.put((index, cleaned, result))


# ---------------------------------------------------------------------------
# Entity extraction: pulls gene names/IDs, SNP IDs, donor IDs from any step
# result (KG records OR SQL rows). Used for cross-source chaining.
# ---------------------------------------------------------------------------

# Column-name keyword heuristics for row-based results
_ENTITY_COLUMN_MAP = {
    # gene-related columns
    "gene_names": ["gene_name", "gene_symbol", "name"],
    "gene_ids":   ["gene_id", "ensembl_id", "id"],
    "snv_ids":    ["snv_id", "snp_id", "rsid", "variant_id"],
    "donor_ids":  ["donor_id", "donor", "center_donor_id", "hpap_id"],
}

_ENSEMBL_RE = re.compile(r'^ENSG\d+$')
_RSID_RE = re.compile(r'^rs\d+$', re.IGNORECASE)


def _extract_entities_from_result(result_entry: dict) -> dict:
    """Return named entities pulled from a single step's result.

    Reads:
    - ``result["records"][*]["nodes"]`` (KG Bolt format): groups by node label
      -> gene nodes contribute ``gene_names`` (properties.name) and ``gene_ids`` (properties.id);
         snv nodes contribute ``snv_ids``; donor nodes contribute ``donor_ids``.
    - ``result["rows"]`` (SQL row format): inspects column names to infer types.

    Returns a dict with keys gene_names, gene_ids, snv_ids, donor_ids (deduped, order preserved).
    """
    out: dict[str, list[str]] = {
        "gene_names": [],
        "gene_ids": [],
        "snv_ids": [],
        "donor_ids": [],
    }
    seen: dict[str, set[str]] = {k: set() for k in out}

    def _add(bucket: str, value) -> None:
        if value is None:
            return
        v = str(value).strip()
        if not v or v in seen[bucket]:
            return
        seen[bucket].add(v)
        out[bucket].append(v)

    result = result_entry.get("result") if isinstance(result_entry, dict) else None
    if not isinstance(result, dict):
        return out

    # Format 1: KG Bolt records with nodes
    for record in result.get("records", []) or []:
        nodes = record.get("nodes", []) if isinstance(record, dict) else []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            labels = [str(l).lower() for l in node.get("labels", [])]
            props = node.get("properties", {}) or {}
            if "gene" in labels:
                _add("gene_names", props.get("name"))
                _add("gene_ids", props.get("id"))
            elif any(l in labels for l in ("snv", "sequence_variant", "variants", "snp")):
                _add("snv_ids", props.get("id"))
            elif "donor" in labels:
                _add("donor_ids", props.get("id") or props.get("center_donor_id"))

    # Format 2: SQL rows — infer from column names
    rows = result.get("rows", []) or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for bucket, candidates in _ENTITY_COLUMN_MAP.items():
            for col in candidates:
                if col in row and row[col]:
                    _add(bucket, row[col])
                    break
        # also heuristic: any value that looks like an Ensembl ID or rsID
        for v in row.values():
            if isinstance(v, str):
                if _ENSEMBL_RE.match(v):
                    _add("gene_ids", v)
                elif _RSID_RE.match(v):
                    _add("snv_ids", v)

    return out


def _summarize_entities(result_entry: dict) -> dict:
    """Quick entity count summary for streaming events."""
    ents = _extract_entities_from_result(result_entry)
    return {k: len(v) for k, v in ents.items()}


_NON_KG_SOURCES = ("hpap", "genomic", "functional_data")


def _call_handler(step: dict, handler, prior_entities: dict | None) -> dict:
    """Call a non-KG handler with prior_entities (falling back if the handler
    does not accept the kwarg for backward compatibility)."""
    src = step.get("source", "unknown")
    nl = step["natural_language"]
    if handler is None:
        return {"query": nl, "result": {"error": f"No {src} handler configured", "source": src}}
    try:
        try:
            return handler(nl, prior_entities=prior_entities)
        except TypeError:
            # Handler hasn't been updated to accept prior_entities — old signature
            return handler(nl)
    except Exception as exc:
        return {"query": nl, "result": {"error": str(exc)[:2000], "source": src}}


def _resolve_prior_entities(step: dict, results_by_id: dict[int, dict]) -> dict | None:
    """Pick the parent step's entities.

    - If ``depends_on`` is explicitly set: use that parent.
    - Otherwise fall back to ``step["id"] - 1`` (implicit predecessor).
    Returns None if no parent exists or the parent produced nothing useful.
    """
    parent_id = step.get("depends_on")
    if parent_id is None:
        parent_id = step.get("id", 0) - 1
    parent = results_by_id.get(parent_id)
    if parent is None:
        return None
    ents = _extract_entities_from_result(parent)
    if not any(ents.values()):
        return None
    ents["source_step_id"] = parent_id
    return ents


def _execute_pure_kg_chain(plan: dict) -> list[dict]:
    """Existing behaviour: combine all KG steps into one compound Cypher query."""
    cyphers = build_executable_queries(plan)
    if not cyphers:
        return [{"query": "", "result": {"error": "No executable Cypher produced"}}]

    # Execute all combined queries in parallel (usually just one for pure chain)
    q: Queue = Queue()
    for idx, cypher in enumerate(cyphers):
        start_new_thread(_execute_one_cypher, (cypher, idx, q))

    collected: dict[int, tuple[str, dict]] = {}
    deadline = time.time() + 120
    while len(collected) < len(cyphers) and time.time() < deadline:
        time.sleep(0.2)
        while not q.empty():
            idx, cleaned, result = q.get_nowait()
            collected[idx] = (cleaned, result)

    out: list[dict] = []
    for idx in range(len(cyphers)):
        if idx in collected:
            cleaned, result = collected[idx]
            out.append({"query": cleaned, "result": result})
        else:
            out.append({
                "query": _clean_cypher(cyphers[idx]),
                "result": {"error": "Neo4j execution timeout"},
            })
    return out


def _execute_cross_source_chain(
    plan: dict,
    hpap_handler,
    genomic_handler,
    functional_data_handler=None,
) -> list[dict]:
    """Execute steps strictly sequentially in id order.

    Each step's result is added to ``results_by_id`` so downstream steps can
    see it via ``depends_on`` (or implicit predecessor). KG steps execute
    their own Cypher individually (no WITH combining).
    """
    steps = sorted(plan.get("steps", []), key=lambda s: s.get("id", 0))

    # Validate depends_on references only point backwards
    ids = {s.get("id") for s in steps}
    for s in steps:
        dep = s.get("depends_on")
        if dep is not None and dep not in ids:
            emit("chain_validation_warning", {
                "id": s.get("id"),
                "bad_depends_on": dep,
                "message": "depends_on references a non-existent step",
            })
        elif dep is not None and dep >= s.get("id", 0):
            emit("chain_validation_warning", {
                "id": s.get("id"),
                "bad_depends_on": dep,
                "message": "depends_on points forward or to self",
            })

    results_by_id: dict[int, dict] = {}
    final: list[dict] = []
    handlers = {"hpap": hpap_handler, "genomic": genomic_handler,
                "functional_data": functional_data_handler}

    for step in steps:
        sid = step.get("id", 0)
        src = step.get("source")
        prior_entities = _resolve_prior_entities(step, results_by_id)

        emit("chain_step_start", {
            "id": sid,
            "source": src or "kg",
            "prior_entity_counts": {k: len(v) for k, v in (prior_entities or {}).items() if isinstance(v, list)},
        })

        if src is None:
            cypher = step.get("cypher", "")
            if not cypher:
                result_entry = {
                    "query": step.get("natural_language", ""),
                    "result": {"error": "No Cypher was translated for this KG step"},
                }
            else:
                cleaned, result = _execute_single_cypher_sync(cypher, index=sid - 1)
                result_entry = {"query": cleaned, "result": result}
        else:
            result_entry = _call_handler(step, handlers.get(src), prior_entities)

        results_by_id[sid] = result_entry
        final.append(result_entry)
        emit("chain_step_done", {
            "id": sid,
            "source": src or "kg",
            "entity_counts": _summarize_entities(result_entry),
        })

    if not final:
        return [{"query": "", "result": {"error": "No steps executed"}}]
    return final


def _execute_parallel_with_deps(
    plan: dict,
    hpap_handler,
    genomic_handler,
    functional_data_handler=None,
) -> list[dict]:
    """Parallel plan execution, with ``depends_on`` support for non-KG steps.

    KG steps are combined/parallel-executed first (as before). Non-KG steps
    whose ``depends_on`` is a KG step receive that parent's extracted
    entities. Non-KG steps that depend on other non-KG steps run after
    their parent finishes.
    """
    steps = plan.get("steps", [])

    hpap_indices: set[int] = set()
    genomic_indices: set[int] = set()
    functional_data_indices: set[int] = set()
    for i, step in enumerate(steps):
        src = step.get("source")
        if src == "hpap":
            hpap_indices.add(i)
        elif src == "genomic":
            genomic_indices.add(i)
        elif src == "functional_data":
            functional_data_indices.add(i)

    cyphers = build_executable_queries(plan)
    kg_results: list[dict] = []

    if cyphers:
        q: Queue = Queue()
        for idx, cypher in enumerate(cyphers):
            start_new_thread(_execute_one_cypher, (cypher, idx, q))

        collected: dict[int, tuple[str, dict]] = {}
        deadline = time.time() + 120
        while len(collected) < len(cyphers) and time.time() < deadline:
            time.sleep(0.2)
            while not q.empty():
                idx, cleaned, result = q.get_nowait()
                collected[idx] = (cleaned, result)

        for idx in range(len(cyphers)):
            if idx in collected:
                cleaned, result = collected[idx]
                kg_results.append({"query": cleaned, "result": result})
            else:
                kg_results.append({
                    "query": _clean_cypher(cyphers[idx]),
                    "result": {"error": "Neo4j execution timeout"},
                })

    # Build results_by_id for KG steps so non-KG steps can see them via depends_on
    results_by_id: dict[int, dict] = {}
    kg_idx = 0
    for i, step in enumerate(steps):
        if step.get("source") not in _NON_KG_SOURCES and kg_idx < len(kg_results):
            results_by_id[step.get("id", i + 1)] = kg_results[kg_idx]
            kg_idx += 1

    non_kg_results: dict[int, dict] = {}
    handlers = {"hpap": hpap_handler, "genomic": genomic_handler,
                "functional_data": functional_data_handler}

    # Execute non-KG steps in dependency order (topological). Simple iterative:
    # repeatedly pick steps whose parent is satisfied (or has no depends_on).
    remaining = list(hpap_indices | genomic_indices | functional_data_indices)
    # Sort by step id so independent steps run in natural order
    remaining.sort(key=lambda i: steps[i].get("id", 0))

    max_iters = len(remaining) + 2
    while remaining and max_iters > 0:
        max_iters -= 1
        progress = False
        new_remaining = []
        for i in remaining:
            step = steps[i]
            dep = step.get("depends_on")
            if dep is not None and dep not in results_by_id:
                new_remaining.append(i)
                continue
            prior_entities = None
            if dep is not None:
                parent = results_by_id.get(dep)
                if parent is not None:
                    ents = _extract_entities_from_result(parent)
                    if any(v for v in ents.values() if isinstance(v, list)):
                        ents["source_step_id"] = dep
                        prior_entities = ents
            src = step.get("source")
            result_entry = _call_handler(step, handlers.get(src), prior_entities)
            results_by_id[step.get("id", i + 1)] = result_entry
            non_kg_results[i] = result_entry
            progress = True
        remaining = new_remaining
        if not progress:
            # Remaining steps have unsatisfiable dependencies — run them without prior
            for i in remaining:
                step = steps[i]
                src = step.get("source")
                result_entry = _call_handler(step, handlers.get(src), None)
                results_by_id[step.get("id", i + 1)] = result_entry
                non_kg_results[i] = result_entry
            remaining = []

    # Merge results in original step order
    final: list[dict] = []
    kg_idx = 0
    for i, step in enumerate(steps):
        if i in non_kg_results:
            final.append(non_kg_results[i])
        else:
            if kg_idx < len(kg_results):
                final.append(kg_results[kg_idx])
                kg_idx += 1
            else:
                final.append({"query": "", "result": {"error": "No executable Cypher produced"}})

    if not final:
        return [{"query": "", "result": {"error": "No executable queries produced"}}]
    return final


def execute_plan(
    plan: dict,
    hpap_handler: Optional[Callable[..., dict]] = None,
    genomic_handler: Optional[Callable[..., dict]] = None,
    functional_data_handler: Optional[Callable[..., dict]] = None,
) -> list[dict]:
    """Execute a translated plan against the appropriate data sources.

    Three execution modes:

    1. **Pure-KG chain** (``plan_type: "chain"``, no non-KG steps): all steps
       are combined into one compound Cypher query via ``WITH`` clauses
       (``qp_cypher_combiner.combine_chain``) and executed as a single Neo4j
       query. Existing behaviour — unchanged.
    2. **Cross-source chain** (``plan_type: "chain"``, at least one non-KG
       step): steps execute **strictly sequentially** in ``id`` order. Each
       step receives prior entities (gene names/IDs, SNP IDs, donor IDs)
       extracted from its parent step (``depends_on`` or implicit id-1).
    3. **Parallel** (default): KG steps run in parallel as before. Non-KG
       steps respect ``depends_on`` — they wait for their parent and receive
       its extracted entities.

    Returns a list of ``{"query", "result"}`` dicts in original step order.
    """
    plan_type = plan.get("plan_type", "parallel")
    steps = plan.get("steps", [])
    has_non_kg = any(s.get("source") in _NON_KG_SOURCES for s in steps)

    if plan_type == "chain" and not has_non_kg:
        return _execute_pure_kg_chain(plan)

    if plan_type == "chain":
        return _execute_cross_source_chain(
            plan, hpap_handler, genomic_handler, functional_data_handler
        )

    return _execute_parallel_with_deps(
        plan, hpap_handler, genomic_handler, functional_data_handler
    )


# ---------------------------------------------------------------------------
# 4. Test-time scaling helpers
# ---------------------------------------------------------------------------

NUM_CANDIDATES = 1


def _count_nonempty_results(results: list[dict]) -> int:
    """Score a candidate by counting how many queries returned non-empty data."""
    count = 0
    for r in results:
        neo4j_result = r.get("result", {})
        if isinstance(neo4j_result, dict) and "error" in neo4j_result:
            continue
        # Handle non-KG tabular results: {"rows": [...], "row_count": N, "source": "hpap"|"genomic"|...}
        if isinstance(neo4j_result, dict) and neo4j_result.get("source") in ("hpap", "genomic", "functional_data"):
            rows = neo4j_result.get("rows", [])
            if rows:
                count += 1
            continue
        # Handle new API format: {"records": [...], "keys": [...]}
        if isinstance(neo4j_result, dict) and "records" in neo4j_result:
            records = neo4j_result["records"]
            if records:
                count += 1
            continue
        # Handle old API format: {"results": "...string..."}
        results_value = (
            neo4j_result.get("results", "")
            if isinstance(neo4j_result, dict)
            else ""
        )
        if isinstance(results_value, str):
            norm = results_value.strip().lower()
            if norm == "no results" or not norm:
                continue
            if "nodes, edges" in norm and (
                "[], []" in norm or "[][]" in norm.replace(" ", "")
            ):
                continue
        count += 1
    return count


def _run_single_pipeline(
    question: str,
    candidate_id: int,
    q: Queue,
    hpap_handler: Optional[Callable[[str], dict]] = None,
    genomic_handler: Optional[Callable[[str], dict]] = None,
) -> None:
    """Worker: run the full plan-translate-execute pipeline for one candidate."""
    try:
        plan = plan_query(question)
        plan = translate_plan(plan)
        results = execute_plan(plan, hpap_handler=hpap_handler, genomic_handler=genomic_handler)
        score = _count_nonempty_results(results)
        q.put((candidate_id, score, results, plan, None))
    except Exception:
        q.put((candidate_id, 0, [], {}, traceback.format_exc()))


# ---------------------------------------------------------------------------
# 5. run_query_planner_pipeline  —  best-of-N with test-time scaling
# ---------------------------------------------------------------------------

def run_query_planner_pipeline(
    question: str,
    hpap_handler: Optional[Callable[[str], dict]] = None,
    genomic_handler: Optional[Callable[[str], dict]] = None,
) -> tuple[list[dict], dict]:
    """Full pipeline with test-time scaling: run N candidates in parallel,
    pick the one with the most non-empty Neo4j results.

    Returns:
        (results, plan)  where *results* is a list of
        ``{"query": ..., "result": ...}`` dicts and *plan* is the final
        plan dict (with Cypher populated on each step).
    """
    emit("pipeline_start", {
        "question": question,
        "num_candidates": NUM_CANDIDATES,
    })

    t0 = time.time()

    # Launch all candidates in parallel
    q: Queue = Queue()
    for i in range(NUM_CANDIDATES):
        start_new_thread(_run_single_pipeline, (question, i, q, hpap_handler, genomic_handler))

    # Collect results (3 min total timeout — candidates run concurrently)
    collected: dict[int, tuple[int, list, dict, str | None]] = {}
    deadline = time.time() + 180
    while len(collected) < NUM_CANDIDATES and time.time() < deadline:
        time.sleep(0.3)
        while not q.empty():
            cid, score, results, plan, error = q.get_nowait()
            collected[cid] = (score, results, plan, error)

    if not collected:
        emit("test_time_scaling_result", {
            "num_candidates": 0,
            "candidates": [],
            "selected": -1,
            "selected_score": 0,
            "elapsed_s": round(time.time() - t0, 1),
        })
        return [{"query": "", "result": {"error": "All candidates timed out"}}], {}

    # Pick best: highest non-empty count, then most total queries as tiebreaker
    best_id = max(
        collected,
        key=lambda cid: (collected[cid][0], len(collected[cid][1])),
    )
    best_score, best_results, best_plan, _ = collected[best_id]

    elapsed = time.time() - t0
    emit("test_time_scaling_result", {
        "num_candidates": len(collected),
        "candidates": [
            {
                "id": cid,
                "score": s,
                "num_queries": len(r),
                "plan_type": p.get("plan_type", "?") if isinstance(p, dict) else "?",
                "error": e[:200] if e else None,
            }
            for cid, (s, r, p, e) in sorted(collected.items())
        ],
        "selected": best_id,
        "selected_score": best_score,
        "elapsed_s": round(elapsed, 1),
    })

    return best_results, best_plan

