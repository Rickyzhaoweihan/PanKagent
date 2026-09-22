"""Bounded dashboard history, isolated from application databases."""
import json
import sqlite3
import time
from pathlib import Path


class History:
    def __init__(self, directory, retention_days=7):
        self.path = Path(directory) / 'health.sqlite3'
        self.retention = retention_days * 86400
        self.pending = {}
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS samples (time REAL PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, component TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT, started REAL NOT NULL, ended REAL, resolved_state TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS active_incident ON incidents(component) WHERE ended IS NULL;''')
            if 'resolved_state' not in [row[1] for row in db.execute('PRAGMA table_info(incidents)')]:
                db.execute('ALTER TABLE incidents ADD COLUMN resolved_state TEXT')
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=2)

    def save(self, snapshot, now=None):
        now = now or time.time()
        # Compact history deliberately excludes raw metrics, metadata and payloads.
        compact = {'time': now, 'components': {c['id']: c['state'] for c in snapshot['components']},
                   'budget': snapshot.get('budget', {}), 'queues': snapshot.get('queues', {})}
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO samples VALUES (?,?)', (now, json.dumps(compact)))
            for c in snapshot['components']:
                if c.get('kind') == 'operation':
                    continue  # Idle operations have no periodic success evidence.
                state = c['state']
                reason = c.get('error_category') or state
                previous, count = self.pending.get(c['id'], (None, 0))
                signature = (state, reason)
                count = count + 1 if previous == signature else 1
                self.pending[c['id']] = (signature, count)
                if count < 2:
                    continue
                active = db.execute('SELECT id,state,reason FROM incidents WHERE component=? AND ended IS NULL', (c['id'],)).fetchone()
                if active and (state == 'healthy' or (active[1], active[2]) != signature):
                    db.execute('UPDATE incidents SET ended=?,resolved_state=? WHERE id=?', (now, state, active[0]))
                    active = None
                if state != 'healthy' and not active:
                    db.execute('INSERT INTO incidents(component,state,reason,started) VALUES (?,?,?,?)', (c['id'], state, reason, now))
            db.execute('DELETE FROM samples WHERE time < ?', (now - self.retention,))
            db.execute('DELETE FROM samples WHERE time NOT IN (SELECT time FROM samples ORDER BY time DESC LIMIT 20160)')
            db.execute('DELETE FROM incidents WHERE ended IS NOT NULL AND ended < ?', (now - self.retention,))
            db.execute('DELETE FROM incidents WHERE ended IS NOT NULL AND id NOT IN (SELECT id FROM incidents ORDER BY id DESC LIMIT 5000)')

    def history(self, hours=24, now=None):
        now = now or time.time()
        with self.connect() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT body FROM samples WHERE time >= ? ORDER BY time', (now - hours*3600,))]
        # Buckets preserve the worst observed state, rather than hiding brief failures.
        ranks = {'healthy': 0, 'unknown': 1, 'degraded': 2, 'unavailable': 3}
        keys = {key for row in rows for key in row['components']}
        start = now - hours*3600
        width = hours*3600/180
        buckets = [[] for _ in range(180)]
        for row in rows:
            index = min(179, max(0, int((row['time']-start)/width)))
            buckets[index].append(row)
        points = []
        for index,bucket in enumerate(buckets):
            states = {key:'unknown' for key in keys} if not bucket else {}
            for row in bucket:
                for key, state in row['components'].items():
                    if ranks.get(state, 1) >= ranks.get(states.get(key, 'healthy'), 0):
                        states[key] = state
            points.append({'time':start+index*width,'end':start+(index+1)*width,'components':states,'samples':len(bucket)})
        return {'hours': hours, 'samples': len(rows), 'from': rows[0]['time'] if rows else None,
                'to': rows[-1]['time'] if rows else None, 'points': points,
                'note': 'Worst observed state per bucket. Unobserved time is not uptime.'}

    def incidents(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM incidents ORDER BY ended IS NULL DESC, started DESC LIMIT 100')]
