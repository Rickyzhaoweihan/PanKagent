"""Separate persistent test allocations; conservative usage accounting."""
import json,sqlite3,uuid,os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT=Path('/db/pankagent-vnext-private/operations/formatter-budget-20261105')
def db():
 ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
 c=sqlite3.connect(ROOT/'usage.sqlite3',timeout=15)
 c.execute('CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, phase TEXT, reserved REAL, charged REAL, usage TEXT)')
 os.chmod(ROOT/'usage.sqlite3',0o600)
 return c
def reserve(payload):
 phase='tomorrow' if datetime.now(ZoneInfo('America/Detroit')).date().isoformat()>='2026-11-06' else 'repair'
 # UTF-8 byte count bounds text tokens, plus protocol overhead. Rates conservatively
 # cover long-context/cache-write pricing; charged is an upper estimate, not invoice.
 amount=((len(json.dumps(payload,ensure_ascii=False).encode())+4096)*5+payload['max_output_tokens']*15)/1e6
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  used=c.execute('SELECT coalesce(sum(coalesce(charged,reserved)),0) FROM calls WHERE phase=?',(phase,)).fetchone()[0]
  if used+amount>20:raise RuntimeError('formatter_test_budget_exhausted: '+phase)
  rid=uuid.uuid4().hex;c.execute('INSERT INTO calls VALUES(?,?,?,NULL,NULL)',(rid,phase,amount))
 return rid
def settle(rid,usage=None,not_sent=False):
 if usage and 'input_tokens' in usage and 'output_tokens' in usage:
  charge=(usage['input_tokens']*5+usage['output_tokens']*15)/1e6
 elif not_sent:charge=0
 else:return # Unknown charges retain the entire reservation.
 with db() as c:c.execute('UPDATE calls SET charged=?,usage=? WHERE id=?',(charge,json.dumps(usage),rid))
