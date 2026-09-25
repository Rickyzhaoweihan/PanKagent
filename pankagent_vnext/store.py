"""Private, durable sessions and replayable events for the isolated vNext service."""

from __future__ import annotations

import json
from contextlib import contextmanager
from .ownership import OwnerLease
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
        self._event_contexts = {}
        self.owner = None
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
        for name, kind in (("owner_id", "TEXT"), ("owner_epoch", "INTEGER")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")
        if "followup_ready" not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN followup_ready INTEGER NOT NULL DEFAULT 0")
        OwnerLease.initialize(self.db)
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.owner is not None:
                    self.owner.assert_owned(self.db)
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def acquire_owner(self, ttl=60.0, clock=time.time):
        lease = OwnerLease("pankagent-vnext", ttl, clock)
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            lease.acquire(self.db)
        self.owner = lease
        return lease.snapshot()

    def renew_owner(self):
        with self.transaction():
            self.owner.renew(self.db)
        return self.owner.snapshot()

    def release_owner(self):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.owner.release(self.db)

    def audit_drop(self):
        self.audit_dropped += 1

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        value.pop("owner_id", None)
        value.pop("owner_epoch", None)
        value["include_context"] = bool(value["include_context"])
        value["followup_ready"] = bool(value.get("followup_ready"))
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
        if self.owner is not None:
            self.db.execute("UPDATE runs SET owner_id=?,owner_epoch=? WHERE run_id=?", (self.owner.owner_id, self.owner.epoch, run_id))
        metadata = {"original_question": question, "parent_run_id": None, "parent_plan_id": None,
                    "revision_instruction": None, "revision_mode": "new_question", "revision_index": 0,
                    "source": "user", "versions": {}, **(audit or {})}
        # Legacy correction metadata may replace original_question with a
        # canonical question. Preserve raw wording independently, from audit
        # evidence only; never reconstruct a historical original from run text.
        if metadata.get('parent_run_id'):
            prior = self.audit_metadata(metadata['parent_run_id']) or {}
            metadata['original_raw_question'] = prior.get('original_raw_question',
                prior.get('raw_original_question', prior.get('original_question')))
        else:
            metadata['original_raw_question'] = metadata.get('original_question')
        self.db.execute("INSERT INTO run_audit VALUES (?, ?)", (run_id, json.dumps(metadata, ensure_ascii=False)))
        return self.get(run_id)

    def create(self, question: str, session_id: str | None = None, *, include_context: bool = True, audit: dict | None = None, legacy_retry_submission: str | None = None, parent_run_id: str | None = None) -> dict:
        with self.transaction():
            if parent_run_id:
                parent = self.get(parent_run_id)
                if not parent or parent['session_id'] != session_id:
                    raise KeyError('parent_run')
                if not parent.get('followup_ready') and parent['status'] not in TERMINAL:
                    raise ValueError('Parent answer is not ready for follow-up.')
                # Freeze within the same transaction as run creation. No future turns.
                audit = {**(audit or {}), 'followup_parent_run_id': parent_run_id,
                         'followup_parent_snapshot': parent,
                         'history_snapshot': self.history(session_id, through_run_id=parent_run_id) if include_context else []}
                legacy_retry_submission = None
            if legacy_retry_submission is not None and session_id:
                from .terminal_retry import accepted_term_correction, terminal_clarification_revision
                accepted = accepted_term_correction(self.latest_run(session_id), legacy_retry_submission)
                if accepted:
                    question = accepted['question']
                    audit = {**(audit or {}), **accepted['audit']}
                    legacy_retry_submission = None
                else:
                    binding = terminal_clarification_revision(self.latest_run(session_id), legacy_retry_submission)
                    if binding:
                        audit = {**(audit or {}), **binding}
                        legacy_retry_submission = None
            if legacy_retry_submission is not None and session_id and include_context:
                from .terminal_retry import terminal_revision_retry
                latest = self.latest_run(session_id)
                previous = self.audit_metadata(latest['run_id']) if latest else None
                parent = self.get((previous or {}).get('parent_run_id')) if (previous or {}).get('parent_run_id') else None
                if parent and parent['session_id'] == session_id:
                    binding = terminal_revision_retry(latest, previous, legacy_retry_submission, include_context=include_context)
                    if binding:
                        audit = {**(audit or {}), **binding, 'prior_plan_sha256': self.content_hash(latest.get('plan'))}
            return self._create_locked(question, session_id, include_context=include_context, audit=audit)

    def latest_run(self, session_id: str) -> dict | None:
        """Return the newest run, including active/cancelled ones; never skip back."""
        with self.lock:
            return self._decode(self.db.execute(
                "SELECT * FROM runs WHERE session_id=? ORDER BY created_epoch DESC, rowid DESC LIMIT 1", (session_id,),
            ).fetchone())

    def revise(self, plan_id: str, question: str, *, include_context: bool = True, audit: dict | None = None) -> tuple[dict, dict]:
        """Atomically invalidate an unconfirmed plan and create its replacement."""
        with self.transaction():
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

    def run_status(self, run_id: str) -> str | None:
        """Read the cancellation fence without decoding the evidence payload."""
        with self.lock:
            row = self.db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return row[0] if row else None

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

    def set_effective_question(self, run_id, question):
        """Persist interpreted scope while retaining the submitted text in audit."""
        if not isinstance(question, str) or not question.strip():
            raise ValueError("empty_effective_question")
        with self.transaction():
            run = self.get(run_id)
            metadata = self.audit_metadata(run_id) or {}
            metadata.setdefault('submitted_question', run['question'])
            metadata['effective_question'] = question.strip()
            self.db.execute("UPDATE run_audit SET metadata=? WHERE run_id=?",
                            (json.dumps(metadata, ensure_ascii=False), run_id))
            self.db.execute("UPDATE runs SET question=?,updated_at=? WHERE run_id=?",
                            (question.strip(), utc_now(), run_id))
            return self.get(run_id)

    def update(self, run_id: str, **fields: Any) -> dict:
        allowed = {"followup_ready", "status", "stage", "plan", "graph_answer", "evidence", "literature", "error", "preview", "preview_cache"}
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Unsupported run update")
        values = []
        for key, value in fields.items():
            values.append(json.dumps(value, ensure_ascii=False) if key in {"plan", "evidence", "literature", "error", "preview", "preview_cache"} and value is not None else value)
        sql = "UPDATE runs SET " + ",".join(f"{key}=?" for key in fields) + ",updated_at=? WHERE run_id=?"
        with self.transaction():
            self.db.execute(sql, [*values, utc_now(), run_id])
            if set(fields) & {"plan", "evidence", "preview", "preview_cache"}:
                self._event_contexts.pop(run_id, None)
        return self.get(run_id)

    def update_if_active(self, run_id, **fields):
        with self.lock:
            value = self.get(run_id)
            if value is None or value["status"] in TERMINAL:
                return value
            return self.update(run_id, **fields)

    def event_if_active(self, run_id, event_type, payload=None):
        with self.lock:
            status = self.run_status(run_id)
            if status is None or (status in TERMINAL and event_type != "terminal"):
                return None
            return self.event(run_id, event_type, payload)

    def persist_answer(self, run_id, graph_answer):
        """Persist a streaming delta without decoding unchanged graph evidence."""
        with self.transaction():
            self.db.execute("UPDATE runs SET graph_answer=?,updated_at=? WHERE run_id=? "
                "AND status NOT IN ('completed','partial','failed','cancelled','interrupted','superseded')",
                (graph_answer, utc_now(), run_id))

    def persist_answer_event(self, run_id, graph_answer, payload):
        """Commit the answer prefix and its replayable delta together.

        Cancellation may abandon the caller while SQLite finishes. Neither half
        may survive alone, and a cancelled/superseded run cannot accept late text.
        """
        with self.transaction():
            status = self.run_status(run_id)
            if status is None or status in TERMINAL:
                return None
            self.db.execute("UPDATE runs SET graph_answer=?,updated_at=? WHERE run_id=?",
                            (graph_answer, utc_now(), run_id))
            return self._event_in_transaction(run_id, 'graph_answer', payload)

    def event_context(self, run_id):
        """Metadata and plan context for public event diagnostics."""
        with self.lock:
            row = self.db.execute("SELECT run_id,session_id,question,status,stage,created_epoch FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            if run_id not in self._event_contexts:
                run = self.get(run_id)
                if len(self._event_contexts) >= 16:
                    self._event_contexts.pop(next(iter(self._event_contexts)))
                self._event_contexts[run_id] = {'plan':run.get('plan')}
            return {**dict(row), **self._event_contexts[run_id]}

    def confirm(self, run_id: str) -> bool:
        with self.transaction():
            cursor = self.db.execute(
                "UPDATE runs SET status='queued',stage='queued',updated_at=? "
                "WHERE run_id=? AND status='awaiting_confirmation'",
                (utc_now(), run_id),
            )
            if cursor.rowcount == 1:
                if self.owner is not None:
                    self.db.execute("UPDATE runs SET owner_id=?,owner_epoch=? WHERE run_id=?", (self.owner.owner_id, self.owner.epoch, run_id))
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
            with self.transaction():
                if event_id and self.db.execute("SELECT 1 FROM audit_events WHERE run_id=? AND event_id=?", (run_id, event_id)).fetchone():
                    return "duplicate"
                if self.db.execute("SELECT COUNT(*) FROM audit_events WHERE run_id=?", (run_id,)).fetchone()[0] >= 1000:
                    raise ValueError("audit_event_limit")
                self.db.execute("INSERT INTO audit_events VALUES (?,?,?,?,?)", (run_id, event_id or str(uuid4()), kind, utc_now(), raw))
            return "recorded"
        except (sqlite3.Error, ValueError, TypeError):
            self.audit_dropped += 1
            return "unavailable"

    def claim_execution_repair(self, run_id, step_id, limits):
        """Persist the attempt before inference; crashes/retries cannot reset it."""
        with self.transaction():
            run = self.get(run_id)
            if not run or run['status'] in TERMINAL:
                return False
            plan = run.get('plan') or {}
            planned = (plan.get('planning_route') or {}).get('claude_calls', 0)
            used = self.db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE run_id=? AND kind='execution_repair_claim'",
                (run_id,)).fetchone()[0]
            if used >= limits['execution_repairs'] or planned + used >= limits['total_claude_calls']:
                return False
            self.db.execute("INSERT INTO audit_events VALUES (?,?,?,?,?)", (run_id, str(uuid4()),
                'execution_repair_claim', utc_now(), json.dumps({'step_id': step_id, 'attempt': used + 1})))
            return True

    def audit_snapshot(self, run_id):
        with self.lock:
            rows = self.db.execute("SELECT event_id,kind,received_at,payload FROM audit_events WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
            return {"version": 1, "metadata": self.audit_metadata(run_id), "events": [dict(row) | {"payload": json.loads(row["payload"])} for row in rows], "dropped_since_start": self.audit_dropped}

    def event(self, run_id: str, event_type: str, payload: dict | None = None) -> dict:
        with self.transaction():
            return self._event_in_transaction(run_id, event_type, payload)

    def _event_in_transaction(self, run_id, event_type, payload):
        row = self.db.execute("SELECT session_id,stage,status,created_epoch FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("run")
        run = dict(row)
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

    def latest_answered_run(self, session_id):
        with self.lock:
            row = self.db.execute("SELECT run_id FROM runs WHERE session_id=? AND (status IN ('completed','partial') OR followup_ready=1) ORDER BY created_epoch DESC, rowid DESC LIMIT 1", (session_id,)).fetchone()
        return self.get(row[0]) if row else None

    def history(self, session_id: str, limit: int = 3, through_run_id: str | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT question,graph_answer,literature FROM runs WHERE session_id=? AND (graph_answer IS NOT NULL OR literature IS NOT NULL) "
                "AND (status IN ('completed','partial') OR followup_ready=1) "
                "AND created_epoch <= COALESCE((SELECT created_epoch FROM runs WHERE run_id=?), 1e30) "
                "ORDER BY created_epoch DESC LIMIT ?",
                (session_id, through_run_id, limit),
            ).fetchall()
        result = []
        for row in reversed(rows):
            answer = row[1] or ''
            references = []
            literature = json.loads(row[2]) if row[2] else {}
            for perspective in literature.get('perspectives', []):
                if isinstance(perspective, dict) and isinstance(perspective.get('answer'), str):
                    answer += '\n\nLiterature (' + str(perspective.get('status', 'unknown')) + '): ' + perspective['answer']
                    refs = perspective.get('references') or []
                    references.extend(refs)
                    answer += '\nReferences: ' + json.dumps(refs, ensure_ascii=False)
            if answer.strip():
                result.extend([{"role": "user", "content": row[0]}, {"role": "assistant", "content": answer[:30000], **({"references":references[:30]} if references else {})}])
        return result

    def interrupt_active(self, started_graph_checks=None, *, recovery=False) -> list[str]:
        with self.lock:
            query = "SELECT run_id FROM runs WHERE status IN ('planning','queued','running')"
            params = ()
            if self.owner is not None:
                self.owner.assert_owned(self.db)
                if recovery:
                    query += " AND (owner_epoch IS NULL OR owner_epoch < ?)"
                    params = (self.owner.epoch,)
                else:
                    query += " AND owner_id=? AND owner_epoch=?"
                    params = (self.owner.owner_id, self.owner.epoch)
            rows = self.db.execute(query, params).fetchall()
            ids = [row[0] for row in rows]
            for run_id in ids:
                run = self.get(run_id)
                fields = {}
                if run["status"] in {"queued", "running"} and (run.get("plan") or {}).get("steps"):
                    from .interrupted_evidence import complete_interrupted_evidence
                    started = None if started_graph_checks is None else started_graph_checks.get(run_id, set())
                    fields["evidence"] = complete_interrupted_evidence(run["plan"], run["evidence"],
                        {"category": "service_restarted"}, started)
                if (run.get('literature') or {}).get('sources'):
                    literature = run['literature']
                    for source in literature['sources'].values():
                        if source.get('status') in {'pending', 'running', 'queued'}:
                            source['status'] = 'interrupted'
                    literature['status'] = 'partial'
                    fields['literature'] = literature
                self.update(run_id, status="interrupted", stage="interrupted", error={"category": "service_restarted", "message": "Service restarted; submit a new plan to continue."}, **fields)
                self.event(run_id, "terminal", {"status": "interrupted"})
        return ids

    def probe(self) -> dict:
        with self.lock:
            self.db.execute("SELECT 1").fetchone()
        return {"state": "healthy", "storage": "sqlite", "durable": True, "audit_dropped": self.audit_dropped}

    def close(self) -> None:
        with self.lock:
            self.db.close()
