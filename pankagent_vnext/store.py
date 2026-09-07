"""Private, durable sessions and replayable events for the isolated vNext service."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted", "superseded"}
ACTIVE = {"planning", "queued", "running"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Small transactions protect confirmation and event sequence allocation."""

    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = state_dir / "sessions.sqlite3"
        self.lock = threading.RLock()
        self.audit_dropped = 0
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, plan_id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                question TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                created_epoch REAL NOT NULL, plan TEXT, graph_answer TEXT,
                evidence TEXT, literature TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                sequence INTEGER NOT NULL, envelope TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS run_audit (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id), metadata TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                run_id TEXT NOT NULL REFERENCES runs(run_id), event_id TEXT NOT NULL,
                kind TEXT NOT NULL, received_at TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (run_id, event_id)
            );
            CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id, created_epoch);
        """)
        # Additive migration preserves sessions created before plan previews.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
        for name in ("preview", "preview_cache", "replacement_run_id"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {name} TEXT")
        if "include_context" not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN include_context INTEGER NOT NULL DEFAULT 1")
        self.db.commit()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        value["include_context"] = bool(value["include_context"])
        for key in ("plan", "evidence", "literature", "error", "preview", "preview_cache"):
            value[key] = json.loads(value[key]) if value[key] is not None else None
        return value

    def _create_locked(self, question: str, session_id: str | None = None, *, include_context: bool = True, audit: dict | None = None) -> dict:
        now = utc_now()
        if session_id is not None:
            if not self.db.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone():
                raise KeyError("session")
        else:
            session_id = str(uuid4())
            self.db.execute("INSERT INTO sessions VALUES (?, ?)", (session_id, now))
        run_id, plan_id = str(uuid4()), str(uuid4())
        self.db.execute(
            "INSERT INTO runs (run_id,plan_id,session_id,question,status,stage,created_at,updated_at,created_epoch,include_context) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, plan_id, session_id, question, "planning", "queued", now, now, time.time(), int(include_context)),
        )
        metadata = {"original_question": question, "parent_run_id": None, "parent_plan_id": None,
                    "revision_instruction": None, "revision_mode": "new_question", "revision_index": 0,
                    "source": "user", "versions": {}, **(audit or {})}
        self.db.execute("INSERT INTO run_audit VALUES (?, ?)", (run_id, json.dumps(metadata, ensure_ascii=False)))
        return self.get(run_id)

    def create(self, question: str, session_id: str | None = None, *, include_context: bool = True, audit: dict | None = None) -> dict:
        with self.lock, self.db:
            return self._create_locked(question, session_id, include_context=include_context, audit=audit)

    def revise(self, plan_id: str, question: str, *, include_context: bool = True, audit: dict | None = None) -> tuple[dict, dict]:
        """Atomically invalidate an unconfirmed plan and create its replacement."""
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.by_plan(plan_id)
            if old is None:
                raise KeyError("plan")
            if old["status"] not in {"planning", "awaiting_confirmation"}:
                raise ValueError("plan_already_confirmed_or_ended")
            prior = self.audit_metadata(old["run_id"]) or {}
            metadata = {**(audit or {}), "original_question": prior.get("original_question", old["question"]),
                        "parent_run_id": old["run_id"], "parent_plan_id": plan_id,
                        "revision_index": prior.get("revision_index", 0) + 1,
                        "source": (audit or {}).get("source") or prior.get("source", "unknown"),
                        "revision_mode": (audit or {}).get("revision_mode", "legacy_replacement"),
                        "revision_instruction": (audit or {}).get("revision_instruction"),
                        "prior_plan_sha256": self.content_hash(old.get("plan")),
                        "prior_options": {"include_context": old["include_context"]},
                        "requested_options": {"include_context": include_context}}
            new = self._create_locked(question, old["session_id"], include_context=include_context, audit=metadata)
            self.db.execute(
                "UPDATE runs SET status='superseded',stage='superseded',replacement_run_id=?,updated_at=? WHERE run_id=?",
                (new["run_id"], utc_now(), old["run_id"]),
            )
            return self.get(old["run_id"]), new

    def get(self, run_id: str) -> dict | None:
        with self.lock:
            return self._decode(self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

    def snapshot(self, run_id: str) -> dict | None:
        """Pair the current state with its replay high-water mark."""
        with self.lock:
            run = self.get(run_id)
            if run is not None:
                run["event_sequence"] = self.db.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM events WHERE run_id=?", (run_id,),
                ).fetchone()[0]
            return run

    def by_plan(self, plan_id: str) -> dict | None:
        with self.lock:
            return self._decode(self.db.execute("SELECT * FROM runs WHERE plan_id=?", (plan_id,)).fetchone())

    def update(self, run_id: str, **fields: Any) -> dict:
        allowed = {"status", "stage", "plan", "graph_answer", "evidence", "literature", "error", "preview", "preview_cache"}
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Unsupported run update")
        values = []
        for key, value in fields.items():
            values.append(json.dumps(value, ensure_ascii=False) if key in {"plan", "evidence", "literature", "error", "preview", "preview_cache"} and value is not None else value)
        sql = "UPDATE runs SET " + ",".join(f"{key}=?" for key in fields) + ",updated_at=? WHERE run_id=?"
        with self.lock, self.db:
            self.db.execute(sql, [*values, utc_now(), run_id])
        return self.get(run_id)

    def confirm(self, run_id: str) -> bool:
        with self.lock, self.db:
            cursor = self.db.execute(
                "UPDATE runs SET status='queued',stage='queued',updated_at=? "
                "WHERE run_id=? AND status='awaiting_confirmation'",
                (utc_now(), run_id),
            )
            if cursor.rowcount == 1:
                metadata = self.audit_metadata(run_id)
                if metadata is not None:
                    metadata.update(confirmed_at=utc_now(), confirmed_plan_sha256=self.content_hash(self.get(run_id)["plan"]))
                    self.db.execute("UPDATE run_audit SET metadata=? WHERE run_id=?", (json.dumps(metadata), run_id))
            return cursor.rowcount == 1

    @staticmethod
    def content_hash(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def audit_metadata(self, run_id):
        with self.lock:
            row = self.db.execute("SELECT metadata FROM run_audit WHERE run_id=?", (run_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def audit_event(self, run_id, kind, payload, event_id=None):
        """Best-effort telemetry; failures are counted, never allowed to fail an answer."""
        try:
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            if len(raw.encode()) > 32000:
                raise ValueError("audit_payload_limit")
            with self.lock, self.db:
                if event_id and self.db.execute("SELECT 1 FROM audit_events WHERE run_id=? AND event_id=?", (run_id, event_id)).fetchone():
                    return "duplicate"
                if self.db.execute("SELECT COUNT(*) FROM audit_events WHERE run_id=?", (run_id,)).fetchone()[0] >= 1000:
                    raise ValueError("audit_event_limit")
                self.db.execute("INSERT INTO audit_events VALUES (?,?,?,?,?)", (run_id, event_id or str(uuid4()), kind, utc_now(), raw))
            return "recorded"
        except (sqlite3.Error, ValueError, TypeError):
            self.audit_dropped += 1
            return "unavailable"

    def audit_snapshot(self, run_id):
        with self.lock:
            rows = self.db.execute("SELECT event_id,kind,received_at,payload FROM audit_events WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
            return {"version": 1, "metadata": self.audit_metadata(run_id), "events": [dict(row) | {"payload": json.loads(row["payload"])} for row in rows], "dropped_since_start": self.audit_dropped}

    def event(self, run_id: str, event_type: str, payload: dict | None = None) -> dict:
        with self.lock, self.db:
            run = self.get(run_id)
            if run is None:
                raise KeyError("run")
            seq = self.db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
            envelope = {
                "version": 2, "run_id": run_id, "session_id": run["session_id"],
                "sequence": seq, "timestamp": utc_now(), "type": event_type,
                "stage": run["stage"], "status": run["status"],
                "elapsed_ms": max(0, round((time.time() - run["created_epoch"]) * 1000)),
                "payload": payload or {},
            }
            self.db.execute("INSERT INTO events VALUES (?,?,?)", (run_id, seq, json.dumps(envelope, ensure_ascii=False)))
        return envelope

    def events_after(self, run_id: str, sequence: int, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT envelope FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (run_id, sequence, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def history(self, session_id: str, limit: int = 3) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT question,graph_answer FROM runs WHERE session_id=? AND graph_answer IS NOT NULL "
                "AND status IN ('completed','partial') ORDER BY created_epoch DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        result = []
        for row in reversed(rows):
            result.extend([{"role": "user", "content": row[0]}, {"role": "assistant", "content": row[1][:12000]}])
        return result

    def interrupt_active(self) -> list[str]:
        with self.lock:
            rows = self.db.execute("SELECT run_id FROM runs WHERE status IN ('planning','queued','running')").fetchall()
            ids = [row[0] for row in rows]
            for run_id in ids:
                self.update(run_id, status="interrupted", stage="interrupted", error={"category": "service_restarted", "message": "Service restarted; submit a new plan to continue."})
                self.event(run_id, "terminal", {"status": "interrupted"})
        return ids

    def probe(self) -> dict:
        with self.lock:
            self.db.execute("SELECT 1").fetchone()
        return {"state": "healthy", "storage": "sqlite", "durable": True, "audit_dropped": self.audit_dropped}

    def close(self) -> None:
        with self.lock:
            self.db.close()
