"""Serial API replay companion. Browser observations/timings are separate.

No cache clearing, prompt edits, or retries of uncertain POST requests.
Run as the isolated service owner; manifests and outputs remain private.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
import time
from urllib.parse import quote
import httpx

TERMINAL = {'completed', 'partial', 'failed', 'cancelled', 'interrupted', 'superseded'}

def stamp(): return datetime.now(timezone.utc).isoformat()

def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)

def load_env(path):
    result = {}
    for line in path.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            parts = shlex.split(value)
            result[key] = parts[0] if parts else ''
    return result

class Replay:
    def __init__(self, output, env):
        self.output = output
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state = Path(env['PANK_VNEXT_STATE_DIR'])
        auth_path = output.parent/'results-auth.json'
        self.results_auth = json.loads(auth_path.read_text()) if auth_path.exists() else None
        self.token = env.get('PANK_VNEXT_OPERATOR_TOKEN', '')
        self.client = httpx.Client(timeout=httpx.Timeout(130, connect=10), follow_redirects=False)

    def budget(self):
        with sqlite3.connect(f'file:{self.state/"budget.sqlite3"}?mode=ro', uri=True) as db:
            spent, reserved, count = db.execute('SELECT COALESCE(SUM(actual),0),COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0),COUNT(*) FROM usage').fetchone()
        return dict(spent_usd=spent, reserved_usd=reserved, calls=count, remaining_usd=10-spent-reserved)

    def persist(self): save(self.path, self.current)

    def call(self, side, method, path, body=None):
        bases = {'official': 'https://jieliulab3.dcmb.med.umich.edu/pankgraph-agent',
                 'vnext': 'http://127.0.0.1:8794', 'results': 'http://127.0.0.1:8795/api'}
        headers = {'Authorization': 'Bearer '+self.token} if side == 'vnext' and self.token else {}
        if side == 'results' and self.results_auth:
            headers = {'Authorization': 'Basic '+base64.b64encode((self.results_auth['username']+':'+self.results_auth['password']).encode()).decode()}
        entry = dict(side=side, method=method, path=path, started_at=stamp(), state='in_flight')
        self.current['requests'].append(entry)
        self.persist()
        started = time.monotonic()
        try:
            response = self.client.request(method, bases[side]+path, json=body, headers=headers)
            entry.update(http_status=response.status_code, elapsed_s=round(time.monotonic()-started, 3), state='received', bytes=len(response.content))
            if len(response.content) > 8*1024*1024: raise ValueError('response_limit')
            value = response.json()
            entry['response_sha256'] = hashlib.sha256(response.content).hexdigest()
            self.persist()
            response.raise_for_status()
            return value
        except Exception as exc:
            entry.update(state='uncertain' if method == 'POST' and 'http_status' not in entry else 'failed', error_category=type(exc).__name__, elapsed_s=round(time.monotonic()-started, 3))
            self.persist()
            raise

    def poll(self, rid, states, deadline=110):
        started = time.monotonic()
        while time.monotonic()-started < deadline:
            value = self.call('vnext', 'GET', '/v2/runs/'+rid)
            if value['status'] in states: return value
            time.sleep(.75)
        raise TimeoutError('run_observation_deadline')

    def presentation(self, body):
        created = self.call('results', 'POST', '/results', body)
        started = time.monotonic()
        while time.monotonic()-started < 65:
            result = self.call('results', 'GET', '/results/'+created['result_id'])
            if result.get('status') != 'preparing' and not any(v in ('pending', 'queued', 'running') for v in result.get('component_status', {}).values()): return result
            time.sleep(.75)
        return result

    def vnext(self, task):
        if self.budget()['remaining_usd'] < .3: raise RuntimeError('budget_gate')
        if task['family'] == 'conventional':
            self.current['result'] = self.presentation({'template_id': task['template_id'], 'parameters': task['parameters']})
            self.current['status'] = 'captured'
            return
        created = self.current.get('created') or self.call('vnext', 'POST', '/v2/plans', {'question': task['question'], 'event_source': 'audit_replay'})
        self.current['created'] = created
        self.persist()
        run = self.poll(created['run_id'], TERMINAL | {'awaiting_confirmation'})
        self.current['initial'] = run
        self.persist()
        if ((run.get('preview') or {}).get('evidence') or {}).get('graph_version'):
            self.current['preview_result'] = self.presentation({'run_id': run['run_id'], 'phase': 'preview'})
            self.persist()
        self.current['revisions'] = []
        for revision in task.get('revisions', []):
            if run['status'] != 'awaiting_confirmation': break
            # Preserve the actual submitted instruction; metadata does not
            # repair planner context or change its existing execution behavior.
            created = self.call('vnext', 'POST', f'/v2/plans/{run["plan_id"]}/revise',
                {'question': revision['instruction'], 'revision_instruction': revision['instruction'], 'revision_mode': 'instruction', 'event_source': 'audit_replay'})
            run = self.poll(created['run_id'], TERMINAL | {'awaiting_confirmation'})
            self.current['revisions'].append({'instruction': revision['instruction'], 'run': run})
            self.persist()
        if run['status'] == 'awaiting_confirmation' and not run['plan'].get('clarification'):
            started = time.monotonic()
            self.current['confirmed_at'] = stamp()
            self.current['confirmation'] = self.call('vnext', 'POST', f'/v2/plans/{run["plan_id"]}/confirm', {})
            run = self.poll(run['run_id'], TERMINAL)
            self.current['confirmation_to_terminal_s'] = round(time.monotonic()-started, 3)
        self.current['final'] = run
        self.persist()
        if (run.get('evidence') or {}).get('graph_version'):
            self.current['result'] = self.presentation({'run_id': run['run_id'], 'phase': 'final'})
        self.current['audit'] = self.call('vnext', 'GET', f'/v2/runs/{run["run_id"]}/audit')
        with sqlite3.connect(f'file:{self.state/"sessions.sqlite3"}?mode=ro', uri=True) as db:
            self.current['events'] = [json.loads(row[0]) for row in db.execute('SELECT envelope FROM events WHERE run_id=? ORDER BY sequence', (run['run_id'],))]
        self.current['revisit'] = self.call('vnext', 'GET', '/v2/runs/'+run['run_id'])
        self.current['status'] = 'captured'

    def official(self, task):
        if task['family'] == 'conventional':
            self.current['status'] = 'requires_browser_template_flow'
            return
        initial = self.current.get('initial') or self.call('official', 'POST', '/chat/start', {'question': task['question'], 'rigor': True, 'use_literature': True, 'auto_confirm': False})
        self.current['initial'] = initial
        self.persist()
        value = initial
        self.current['revisions'] = []
        pending = initial.get('pending_plan_session_id')
        for revision in task.get('revisions', []):
            # Pending plans use /plan/revise. /chat/revise only accepts an
            # already confirmed chat round and would return 409 here.
            value = self.call('official', 'POST', '/plan/revise' if pending else '/chat/revise',
                {'session_id': pending or initial['session_id'], 'prompt': revision['instruction']})
            self.current['revisions'].append({'instruction': revision['instruction'], 'response': value})
            self.persist()
        if pending:
            self.current['final'] = self.call('official', 'POST', '/chat/plan/confirm', {'chat_session_id': initial['session_id'], 'plan_session_id': pending})
        else: self.current['final'] = value
        self.current['revisit'] = self.call('official', 'GET', '/chat/history?session_id='+quote(initial['session_id']))
        self.current['status'] = 'captured'

    def task(self, task, side, manifest_hash, resume_auth=False, resume_revision=False):
        self.path = self.output/(task['id']+'-'+side+'.json')
        if self.path.exists():
            existing = json.loads(self.path.read_text())
            if existing.get('manifest_sha256') != manifest_hash: raise ValueError('manifest_changed')
            resumable = (resume_auth and side == 'vnext' and existing['status'] == 'blocked'
                and existing.get('created') and not existing.get('revisions') and not existing.get('confirmation')
                and existing['requests'][-1].get('http_status') == 401 and existing['requests'][-1]['side'] == 'results')
            revision_resumable = (resume_revision and side == 'official' and existing['status'] == 'blocked'
                and existing.get('initial', {}).get('pending_plan_session_id') and not existing.get('revisions')
                and existing['requests'][-1].get('http_status') == 409
                and existing['requests'][-1]['path'] == '/chat/revise')
            resumable = resumable or revision_resumable
            if not resumable:
                print(json.dumps({'task': task['id'], 'side': side, 'status': 'preserved_existing', 'existing_status': existing['status']}), flush=True)
                return
            self.current = existing
            self.current['harness_correction'] = {'reason': 'pending_plan_revision_endpoint' if revision_resumable else 'missing_demo_authentication', 'at': stamp(), 'reuse_created_run': True, 'exclude_from_product_latency': True}
            self.current.pop('error_category', None)
        else:
            self.current = dict(version=1, task_id=task['id'], side=side, manifest_sha256=manifest_hash,
                started_at=stamp(), measurement_layer='API lifecycle, not browser timing', status='running', requests=[], budget_before=self.budget())
        self.persist()
        try: getattr(self, side)(task)
        except Exception as exc: self.current.update(status='blocked', error_category=type(exc).__name__)
        finally:
            self.current.update(ended_at=stamp(), budget_after=self.budget())
            self.persist()
            print(json.dumps({'task': task['id'], 'side': side, 'status': self.current['status'], 'run_status': self.current.get('final', {}).get('status'), 'error': self.current.get('error_category'), 'remaining_usd': round(self.budget()['remaining_usd'], 4)}), flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--resume-auth-failure', action='store_true')
    parser.add_argument('--resume-revision-conflict', action='store_true')
    parser.add_argument('--ids', default='')
    parser.add_argument('--side', choices=['both', 'official', 'vnext'], default='both')
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    replay = Replay(args.output, load_env(args.env_file))
    selected = set(args.ids.split(',')) if args.ids else None
    for index, task in enumerate(manifest['tasks']):
        if selected is not None and task['id'] not in selected: continue
        if task.get('parent_task') or task['id'] == 'X02': continue
        sides = [args.side] if args.side != 'both' else (['official', 'vnext'] if index % 2 == 0 else ['vnext', 'official'])
        for side in sides: replay.task(task, side, digest, args.resume_auth_failure, args.resume_revision_conflict)
