"""SQLite-backed persistence for server.py session state.

Writes are synchronous on the request thread — each HTTP handler upserts the
mutated ``PlanSession`` / ``ChatSession`` to disk *before* returning the
response, so a crash or restart cannot lose state that was already ack'd to
the client.

Sessions that the in-memory TTL has already evicted stay on disk forever
(retention policy: keep-forever). Only non-expired rows are restored into the
in-memory dicts at server startup.

Lock acquisition order (invariant — document and preserve):
    server.py::_sessions_lock       ─┐
    server.py::_chat_sessions_lock  ─┼── acquired FIRST
                                      ↓
    session_store::_conn_lock       ─── acquired INSIDE the above

Never the reverse. This prevents deadlock between a request thread that holds
_sessions_lock and the startup-restore thread that holds _conn_lock.

Payload size note: our worst-case ``PlanSession`` is ~250 KB (measured
2026-04-22). SQLite's default row limit is ~1 GB, so no special handling is
needed. We store list/dict fields as JSON ``TEXT`` (not gzip ``BLOB``) so the
``sqlite3`` CLI can inspect a stuck session without a decoder.

Durability: ``journal_mode=WAL`` + ``synchronous=NORMAL``. This is durable
across process crashes; it can only lose uncommitted work on an OS-level
crash or power loss. Acceptable for a session store.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid circular import at runtime — server.py imports us
    from server import PlanSession, ChatSession

logger = logging.getLogger(__name__)


_DB_PATH = os.environ.get(
    "PANK_SESSIONS_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "sessions.sqlite"),
)

_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()


# ---------------------------------------------------------------------------
# DDL (kept inline so a bare `python -c 'from session_store import init_db; init_db()'`
# works on any fresh machine)
# ---------------------------------------------------------------------------

_DDL = [
    """CREATE TABLE IF NOT EXISTS plan_sessions (
        session_id          TEXT PRIMARY KEY,
        original_question   TEXT NOT NULL,
        rigor               INTEGER NOT NULL,
        complexity          TEXT NOT NULL DEFAULT 'simple',
        use_literature      INTEGER NOT NULL DEFAULT 1,
        literature_result   TEXT NOT NULL DEFAULT '',
        current_plan        TEXT NOT NULL DEFAULT '{}',
        neo4j_results       TEXT NOT NULL DEFAULT '[]',
        cypher_queries      TEXT NOT NULL DEFAULT '[]',
        chat_history        TEXT NOT NULL DEFAULT '[]',
        created_at          REAL NOT NULL,
        updated_at          REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_plan_sessions_created_at ON plan_sessions(created_at)",

    """CREATE TABLE IF NOT EXISTS chat_sessions (
        session_id                TEXT PRIMARY KEY,
        rigor                     INTEGER NOT NULL,
        use_literature            INTEGER NOT NULL DEFAULT 1,
        history                   TEXT NOT NULL DEFAULT '[]',
        last_question             TEXT NOT NULL DEFAULT '',
        last_plan                 TEXT NOT NULL DEFAULT '{}',
        last_neo4j_results        TEXT NOT NULL DEFAULT '[]',
        last_cypher_queries       TEXT NOT NULL DEFAULT '[]',
        last_complexity           TEXT NOT NULL DEFAULT 'simple',
        last_literature_result    TEXT NOT NULL DEFAULT '',
        pending_question          TEXT NOT NULL DEFAULT '',
        pending_plan_session_id   TEXT NOT NULL DEFAULT '',
        created_at                REAL NOT NULL,
        last_active               REAL NOT NULL,
        updated_at                REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions_last_active ON chat_sessions(last_active)",

    """CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT NOT NULL,
        event       TEXT NOT NULL,
        ts          REAL NOT NULL,
        data        TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts         ON events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_event      ON events(event)",

    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",

    # Content-addressed cache of generated rigor-agent answers. Keyed by a
    # fingerprint of the executed query artifacts (cypher/SQL/functional URL) +
    # rigor/complexity/use_literature + CACHE_VERSION — NOT the NL phrasing. Lets
    # a repeat confirm skip the slow Sonnet call and replay the stored response.
    """CREATE TABLE IF NOT EXISTS answer_cache (
        fingerprint     TEXT PRIMARY KEY,
        response        TEXT NOT NULL,
        complexity      TEXT,
        rigor           INTEGER,
        use_literature  INTEGER,
        created_at      REAL NOT NULL,
        last_hit_at     REAL,
        hit_count       INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_answer_cache_created_at ON answer_cache(created_at)",
]


SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Connection bootstrap / teardown
# ---------------------------------------------------------------------------

def init_db(db_path: str | None = None) -> None:
    """Open (or create) the SQLite file and ensure the schema exists.

    Safe to call multiple times from the same process — it will close a prior
    connection and reopen. That is useful for tests and for the
    ``_restart_survives_close`` guarantee in test_session_store.py.
    """
    global _conn
    path = db_path or _DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    with _conn_lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None

        # isolation_level=None → autocommit; we use explicit BEGIN where needed.
        # This avoids sqlite3's legacy implicit-transaction behavior that clashes
        # with WAL mode PRAGMAs.
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row

        # PRAGMAs — order matters: journal_mode must come before any data write.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")

        for stmt in _DDL:
            conn.execute(stmt)

        # Seed / verify schema_version exactly once
        cur = conn.execute("SELECT version FROM schema_version")
        rows = cur.fetchall()
        if not rows:
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        elif rows[0]["version"] != SCHEMA_VERSION:
            logger.warning(
                "schema_version on disk (%s) differs from code (%s). "
                "No migration applied — verify manually.",
                rows[0]["version"], SCHEMA_VERSION,
            )

        _conn = conn

    logger.info("session_store: initialized at %s (schema v%d)", path, SCHEMA_VERSION)


def close_db() -> None:
    """Close the connection. Safe to call at shutdown or in tests."""
    global _conn
    with _conn_lock:
        if _conn is not None:
            try:
                _conn.close()
            finally:
                _conn = None


def _get_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError(
            "session_store not initialized — call session_store.init_db() "
            "before any upsert/load/delete/record_event."
        )
    return _conn


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _loads(text: str | None, default: Any) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise


def _plan_to_row(s: "PlanSession") -> tuple:
    now = time.time()
    return (
        s.session_id,
        s.original_question,
        1 if s.rigor else 0,
        s.complexity,
        1 if s.use_literature else 0,
        s.literature_result or "",
        _dumps(s.current_plan or {}),
        _dumps(s.neo4j_results or []),
        _dumps(s.cypher_queries or []),
        _dumps(s.chat_history or []),
        float(s.created_at),
        now,
    )


def _row_to_plan(row: sqlite3.Row) -> "PlanSession":
    # Local import to avoid circular import at module load
    from server import PlanSession
    return PlanSession(
        session_id=row["session_id"],
        original_question=row["original_question"],
        rigor=bool(row["rigor"]),
        chat_history=_loads(row["chat_history"], []),
        current_plan=_loads(row["current_plan"], {}),
        neo4j_results=_loads(row["neo4j_results"], []),
        cypher_queries=_loads(row["cypher_queries"], []),
        complexity=row["complexity"],
        use_literature=bool(row["use_literature"]),
        literature_result=row["literature_result"] or "",
        created_at=float(row["created_at"]),
    )


def _chat_to_row(s: "ChatSession") -> tuple:
    now = time.time()
    return (
        s.session_id,
        1 if s.rigor else 0,
        1 if s.use_literature else 0,
        _dumps(s.history or []),
        s.last_question or "",
        _dumps(s.last_plan or {}),
        _dumps(s.last_neo4j_results or []),
        _dumps(s.last_cypher_queries or []),
        s.last_complexity or "simple",
        s.last_literature_result or "",
        s.pending_question or "",
        s.pending_plan_session_id or "",
        float(s.created_at),
        float(s.last_active),
        now,
    )


def _row_to_chat(row: sqlite3.Row) -> "ChatSession":
    from server import ChatSession
    return ChatSession(
        session_id=row["session_id"],
        history=_loads(row["history"], []),
        rigor=bool(row["rigor"]),
        use_literature=bool(row["use_literature"]),
        last_question=row["last_question"] or "",
        last_plan=_loads(row["last_plan"], {}),
        last_neo4j_results=_loads(row["last_neo4j_results"], []),
        last_cypher_queries=_loads(row["last_cypher_queries"], []),
        last_complexity=row["last_complexity"] or "simple",
        last_literature_result=row["last_literature_result"] or "",
        pending_question=row["pending_question"] or "",
        pending_plan_session_id=row["pending_plan_session_id"] or "",
        created_at=float(row["created_at"]),
        last_active=float(row["last_active"]),
    )


# ---------------------------------------------------------------------------
# Public write API (synchronous, raise-on-failure for sessions)
# ---------------------------------------------------------------------------

_PLAN_COLS = (
    "session_id, original_question, rigor, complexity, use_literature, "
    "literature_result, current_plan, neo4j_results, cypher_queries, "
    "chat_history, created_at, updated_at"
)
_PLAN_PLACEHOLDERS = ", ".join(["?"] * 12)
_PLAN_UPSERT_SQL = (
    f"INSERT INTO plan_sessions ({_PLAN_COLS}) VALUES ({_PLAN_PLACEHOLDERS}) "
    f"ON CONFLICT(session_id) DO UPDATE SET "
    f"original_question=excluded.original_question, "
    f"rigor=excluded.rigor, "
    f"complexity=excluded.complexity, "
    f"use_literature=excluded.use_literature, "
    f"literature_result=excluded.literature_result, "
    f"current_plan=excluded.current_plan, "
    f"neo4j_results=excluded.neo4j_results, "
    f"cypher_queries=excluded.cypher_queries, "
    f"chat_history=excluded.chat_history, "
    f"updated_at=excluded.updated_at"
)

_CHAT_COLS = (
    "session_id, rigor, use_literature, history, last_question, last_plan, "
    "last_neo4j_results, last_cypher_queries, last_complexity, "
    "last_literature_result, pending_question, pending_plan_session_id, "
    "created_at, last_active, updated_at"
)
_CHAT_PLACEHOLDERS = ", ".join(["?"] * 15)
_CHAT_UPSERT_SQL = (
    f"INSERT INTO chat_sessions ({_CHAT_COLS}) VALUES ({_CHAT_PLACEHOLDERS}) "
    f"ON CONFLICT(session_id) DO UPDATE SET "
    f"rigor=excluded.rigor, "
    f"use_literature=excluded.use_literature, "
    f"history=excluded.history, "
    f"last_question=excluded.last_question, "
    f"last_plan=excluded.last_plan, "
    f"last_neo4j_results=excluded.last_neo4j_results, "
    f"last_cypher_queries=excluded.last_cypher_queries, "
    f"last_complexity=excluded.last_complexity, "
    f"last_literature_result=excluded.last_literature_result, "
    f"pending_question=excluded.pending_question, "
    f"pending_plan_session_id=excluded.pending_plan_session_id, "
    f"last_active=excluded.last_active, "
    f"updated_at=excluded.updated_at"
)


def upsert_plan_session(s: "PlanSession") -> None:
    row = _plan_to_row(s)
    with _conn_lock:
        _get_conn().execute(_PLAN_UPSERT_SQL, row)


def upsert_chat_session(s: "ChatSession") -> None:
    row = _chat_to_row(s)
    with _conn_lock:
        _get_conn().execute(_CHAT_UPSERT_SQL, row)


def delete_plan_session(session_id: str) -> None:
    """Remove a plan session row — used when the client confirms/completes a plan.

    Note: retention policy is keep-forever for TTL evictions, so the server's
    periodic cleanup does NOT call this. Only explicit user-confirmed
    completion (/plan/confirm, /chat/plan/confirm) calls delete_plan_session.
    """
    with _conn_lock:
        _get_conn().execute("DELETE FROM plan_sessions WHERE session_id = ?", (session_id,))


def delete_chat_session(session_id: str) -> None:
    """Remove a chat session row — used only when the client explicitly ends
    a chat via /chat/end. TTL evictions are dict-only and leave the row."""
    with _conn_lock:
        _get_conn().execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))


def record_event(session_id: str, event: str, data: dict) -> None:
    """Append a JSONL-style audit record. Mirrors logs/plan_sessions.jsonl.

    The caller (server._log_plan_event) wraps this in try/except so a failure
    here is log-and-continue — the JSONL log remains the second source of
    truth.
    """
    with _conn_lock:
        _get_conn().execute(
            "INSERT INTO events (session_id, event, ts, data) VALUES (?, ?, ?, ?)",
            (session_id, event, time.time(), _dumps(data)),
        )


# ---------------------------------------------------------------------------
# Answer cache (content-addressed; skips the slow rigor agent on repeat plans)
# ---------------------------------------------------------------------------

def get_cached_answer(fingerprint: str) -> str | None:
    """Return the cached rigor-agent response string for this fingerprint, or
    None. Bumps hit_count / last_hit_at on a hit."""
    with _conn_lock:
        conn = _get_conn()
        cur = conn.execute(
            "SELECT response FROM answer_cache WHERE fingerprint = ?", (fingerprint,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE answer_cache SET hit_count = hit_count + 1, last_hit_at = ? "
            "WHERE fingerprint = ?",
            (time.time(), fingerprint),
        )
        return row["response"]


def put_cached_answer(fingerprint: str, response: str, complexity: str | None,
                      rigor: bool, use_literature: bool) -> None:
    """Store (or refresh) a generated answer keyed by its query fingerprint."""
    with _conn_lock:
        _get_conn().execute(
            "INSERT INTO answer_cache "
            "(fingerprint, response, complexity, rigor, use_literature, created_at, hit_count) "
            "VALUES (?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(fingerprint) DO UPDATE SET "
            "response=excluded.response, complexity=excluded.complexity, "
            "rigor=excluded.rigor, use_literature=excluded.use_literature, "
            "created_at=excluded.created_at",
            (fingerprint, response, complexity, 1 if rigor else 0,
             1 if use_literature else 0, time.time()),
        )


def clear_answer_cache() -> int:
    """Delete every cached answer. Returns the number of rows removed.
    Call after a Neo4j reload (same queries, changed data)."""
    with _conn_lock:
        conn = _get_conn()
        n = conn.execute("SELECT COUNT(*) AS c FROM answer_cache").fetchone()["c"]
        conn.execute("DELETE FROM answer_cache")
        return int(n)


# ---------------------------------------------------------------------------
# Startup restore
# ---------------------------------------------------------------------------

def load_all_plan_sessions() -> list["PlanSession"]:
    """Return every PlanSession row. Caller filters by TTL.

    Rows with malformed JSON in any list/dict column are logged and skipped
    (the server must not crash on a single corrupt row).
    """
    out: list = []
    with _conn_lock:
        cur = _get_conn().execute(f"SELECT {_PLAN_COLS} FROM plan_sessions")
        rows = cur.fetchall()
    for row in rows:
        try:
            out.append(_row_to_plan(row))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "session_store: skipping malformed plan_session row '%s': %s",
                row["session_id"], exc,
            )
    return out


def load_all_chat_sessions() -> list["ChatSession"]:
    out: list = []
    with _conn_lock:
        cur = _get_conn().execute(f"SELECT {_CHAT_COLS} FROM chat_sessions")
        rows = cur.fetchall()
    for row in rows:
        try:
            out.append(_row_to_chat(row))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "session_store: skipping malformed chat_session row '%s': %s",
                row["session_id"], exc,
            )
    return out


# ---------------------------------------------------------------------------
# Introspection helper (not part of the public API — used by tests)
# ---------------------------------------------------------------------------

def _count(table: str) -> int:
    with _conn_lock:
        cur = _get_conn().execute(f"SELECT COUNT(*) AS n FROM {table}")
        return int(cur.fetchone()["n"])
