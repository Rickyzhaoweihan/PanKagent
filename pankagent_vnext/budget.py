"""Persistent atomic reservations: interrupted/ambiguous calls retain their bound."""
import sqlite3
import threading
import time
import uuid
from pathlib import Path

PRICES = {'claude-sonnet-5':(2.0,10.0), 'claude-haiku-4-5-20251001':(1.0,5.0)}

class BudgetExceeded(RuntimeError):
    pass

class Budget:
    def __init__(self,path,limit):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.limit=float(limit);self.lock=threading.RLock()
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, model TEXT, purpose TEXT, reserved REAL, actual REAL, created REAL, tokens TEXT)')
        self.path.chmod(0o600)
    def _db(self):
        return sqlite3.connect(self.path,timeout=10)
    def reserve(self,model,purpose,input_bound,max_output):
        ip,op=PRICES[model]
        # Covers uncached input or a 1.25x five-minute cache write, whichever is larger.
        amount=(input_bound*ip*1.25+max_output*op)/1e6
        with self.lock,self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            used=db.execute('SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM usage').fetchone()[0]
            if used+amount > self.limit: raise BudgetExceeded('development_budget_exhausted')
            rid=uuid.uuid4().hex
            db.execute('INSERT INTO usage VALUES (?,?,?,?,NULL,?,NULL)',(rid,model,purpose,amount,time.time()))
        return rid
    def settle(self,rid,usage):
        import json
        with self.lock,self._db() as db:
            row=db.execute('SELECT model FROM usage WHERE id=?',(rid,)).fetchone()
            ip,op=PRICES[row[0]]
            actual=(usage.get('input_tokens',0)*ip+usage.get('output_tokens',0)*op+
                    usage.get('cache_creation_input_tokens',0)*ip*1.25+
                    usage.get('cache_read_input_tokens',0)*ip*.1)/1e6
            db.execute('UPDATE usage SET actual=?,tokens=? WHERE id=?',(actual,json.dumps(usage),rid))
    def snapshot(self):
        with self.lock,self._db() as db:
            actual,reserved,calls,pending=db.execute('SELECT COALESCE(SUM(actual),0),COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0),COUNT(*),SUM(CASE WHEN actual IS NULL THEN 1 ELSE 0 END) FROM usage').fetchone()
        return {'limit_usd':self.limit,'spent_usd':round(actual,6),'reserved_usd':round(reserved,6),
                'remaining_usd':round(max(0,self.limit-actual-reserved),6),'calls':calls,'pending_calls':pending or 0}
