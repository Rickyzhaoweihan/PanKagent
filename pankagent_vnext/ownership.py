"""Single-active-owner leases and transaction fencing in the service database.

This is a prerequisite for safe recovery, not an active-active job scheduler.
No acquired lease authorizes replaying an uncertain model request.
"""
import time
from uuid import uuid4


class OwnershipError(RuntimeError):
    category = "service_ownership"


class OwnerAlive(OwnershipError):
    pass


class OwnerLost(OwnershipError):
    pass


class OwnerLease:
    def __init__(self, service, ttl=60.0, clock=time.time):
        self.service, self.ttl, self.clock = service, ttl, clock
        self.owner_id = uuid4().hex
        self.epoch = None
        self.expires_at = 0.0
        self.state = "unclaimed"

    @staticmethod
    def initialize(db):
        db.execute("CREATE TABLE IF NOT EXISTS service_ownership (service TEXT PRIMARY KEY, owner_id TEXT NOT NULL, epoch INTEGER NOT NULL, expires_at REAL NOT NULL)")

    def acquire(self, db):
        """Caller holds BEGIN IMMEDIATE; refuses a still-valid prior lease."""
        now = self.clock()
        row = db.execute("SELECT owner_id,epoch,expires_at FROM service_ownership WHERE service=?", (self.service,)).fetchone()
        if row and row[2] > now:
            raise OwnerAlive("service_has_active_owner")
        epoch = row[1] + 1 if row else 1
        db.execute("INSERT OR REPLACE INTO service_ownership VALUES (?,?,?,?)", (self.service, self.owner_id, epoch, now + self.ttl))
        self.epoch, self.expires_at, self.state = epoch, now + self.ttl, "active"
        return epoch

    def assert_owned(self, db):
        row = db.execute("SELECT owner_id,epoch,expires_at FROM service_ownership WHERE service=?", (self.service,)).fetchone()
        if (not row or row[0] != self.owner_id or row[1] != self.epoch
                or row[2] <= self.clock() or self.state != "active"):
            self.state = "lost"
            raise OwnerLost("service_owner_lease_lost")

    def renew(self, db):
        self.assert_owned(db)
        expires = self.clock() + self.ttl
        db.execute("UPDATE service_ownership SET expires_at=? WHERE service=? AND owner_id=? AND epoch=?", (expires, self.service, self.owner_id, self.epoch))
        self.expires_at = expires

    def release(self, db):
        # A stale owner must never release its successor's lease.
        db.execute("UPDATE service_ownership SET expires_at=0 WHERE service=? AND owner_id=? AND epoch=?", (self.service, self.owner_id, self.epoch))
        self.state = "released"

    def snapshot(self):
        state = self.state
        if state == "active" and self.expires_at <= self.clock():
            state = "lost"
        return {"state": state, "epoch": self.epoch, "expires_at": self.expires_at or None, "mode": "single_active_owner"}
