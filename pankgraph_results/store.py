"""Durable immutable input snapshots and idempotent result jobs."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class ResultStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "results.sqlite3"
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS results (id TEXT PRIMARY KEY, cache_key TEXT UNIQUE, source TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)")
        self.path.chmod(0o600)

    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def create(self, source, identity):
        key = digest(identity)
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM results WHERE cache_key=?", (key,)).fetchone()
            if row:
                return json.loads(row["payload"]), False
            rid = str(uuid.uuid4())
            payload = {"version": 1, "result_id": rid, "status": "preparing", "component_status": {"graph": "pending", "layout": "pending", "resources": "pending", "answer": "pending"}, "created_at": now, "updated_at": now}
            db.execute("INSERT INTO results VALUES (?,?,?,?,?,?)", (rid, key, json.dumps(source), json.dumps(payload), now, now))
            return payload, True

    def get(self, rid):
        with self.db() as db:
            row = db.execute("SELECT payload FROM results WHERE id=?", (rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def by_identity(self, identity):
        with self.db() as db:
            row = db.execute("SELECT payload FROM results WHERE cache_key=?", (digest(identity),)).fetchone()
        return json.loads(row[0]) if row else None

    def source(self, rid):
        with self.db() as db:
            row = db.execute("SELECT source FROM results WHERE id=?", (rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def previous_presentation(self, run_id, exclude):
        with self.db() as db:
            row = db.execute("SELECT payload FROM results WHERE json_extract(source,'$.run_id')=? AND id<>? AND json_type(payload,'$.xy_json')='object' ORDER BY updated DESC LIMIT 1", (run_id, exclude)).fetchone()
        return json.loads(row[0]) if row else None

    def update(self, rid, **changes):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM results WHERE id=?", (rid,)).fetchone()
            if row is None:
                raise KeyError(rid)
            payload = json.loads(row[0])
            if "component_status" in changes:
                changes["component_status"] = {**payload.get("component_status", {}), **changes["component_status"]}
            payload.update(changes, updated_at=time.time())
            db.execute("UPDATE results SET payload=?,updated=? WHERE id=?", (json.dumps(payload, allow_nan=False), payload["updated_at"], rid))
        return payload

    def interrupt(self):
        with self.db() as db:
            rows = list(db.execute("SELECT id,payload FROM results"))
        for row in rows:
            value = json.loads(row["payload"])
            pending = {k: "interrupted" for k, v in value.get("component_status", {}).items() if v == "pending"}
            if pending:
                self.update(row["id"], status="interrupted" if value["status"] == "preparing" else "ready", component_status=pending, error="service_restarted")

    def probe(self):
        with self.db() as db:
            db.execute("SELECT 1").fetchone()
        return {"state": "healthy"}
