"""Paired live comparison of the default and competing-candidate workflows.

No deployment: two isolated ASGI runtimes share the same existing budget ledger.
References are frozen before model calls. Raw evidence stays outside the repo.
"""
import argparse
import asyncio
from contextlib import AsyncExitStack
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n')


def plan_request(case, parent=None):
    if case.get('parent') and not parent:
        raise ValueError('A contextual case requires its recorded parent run')
    return {'question':case['question'], 'session_id':parent['session_id'] if parent else None,
            'include_context':bool(parent), 'event_source':'audit_replay'}


def extract(selector, results):
    nodes = [n for result in results.values() for n in result.get('nodes', [])]
    typed = {str(n['id']) for n in nodes if selector.get('entity_type') in n.get('labels', [])}
    if selector['mode'] == 'nodes':
        ids = typed
    else:
        edges = [e for result in results.values() for e in result.get('edges', [])
                 if e.get('type') == selector['relation']]
        if selector.get('anchor_id'):
            edges = [e for e in edges if e.get(selector['anchor_endpoint']) == selector['anchor_id']]
        if selector['mode'] == 'coloc':
            ids = {str(e.get('properties', {}).get('gwas_signal_id') or '') + '|' +
                   str(e.get('properties', {}).get('qtl_signal_id') or '') for e in edges}
        else:
            ids = {str(e[selector['endpoint']]) for e in edges} & typed
    if 'only_ids' in selector:
        ids &= set(selector['only_ids'])
    return ids - set(selector.get('omit', []))


def evaluate(case, frozen, run):
    from pankagent_vnext.composable_planning import answer_results
    evidence = run.get('evidence') or (run.get('preview') or {}).get('evidence') or {}
    previous = {s['step_id']:s for s in evidence.get('steps', [])}
    selected = answer_results(run.get('plan') or {}, previous)
    checks = []
    for spec, reference in zip(case['checks'], frozen['checks']):
        actual = extract(spec['selector'], selected)
        expected = set(reference.get('ids', []))
        relation = spec.get('requires_relation')
        # No evidence is not a verified negative. Require an executed complete
        # check for the requested relation before accepting an empty reference.
        relevant = [previous.get(s['id'], {}) for s in (run.get('plan') or {}).get('steps', [])
                    if not relation or relation in s.get('relation_types', [])]
        complete = bool(relevant) and all(r.get('status') in {'complete','empty'}
                     and r.get('truncated') is False and not r.get('error') for r in relevant)
        overlap = len(actual & expected)
        checks.append({'name':spec['name'], 'comparison':spec['comparison'],
            'reference_ok':reference['ok'], 'reference_count':len(expected), 'actual_count':len(actual),
            'missing_count':len(expected-actual), 'extra_count':len(actual-expected),
            'precision':overlap/len(actual) if actual else (1.0 if not expected else 0.0),
            'recall':overlap/len(expected) if expected else (1.0 if not actual else 0.0),
            'membership_equal':bool(reference['ok'] and actual == expected), 'complete':complete,
            'pass':bool(reference['ok'] and actual == expected and complete)})
    exact = [c for c in checks if c['comparison'] == 'exact']
    needs_clarification = case.get('expected_clarification',False)
    return {'checks':checks, 'exact_reference_pass':all(c['pass'] for c in exact) if exact and not needs_clarification else None,
            'expected_clarification_pass':bool((run.get('plan') or {}).get('clarification')
                and (run.get('error') or {}).get('category') != 'planning_failure') if needs_clarification else None,
            'manual_review_required':True, 'answer_chars':len(run.get('graph_answer') or '')}


def model_accounting(audit):
    reserved = {e['payload']['reservation_id']:e for e in audit['events'] if e['kind']=='model_reserved'}
    settled = {e['payload']['reservation_id']:e for e in audit['events'] if e['kind']=='model_settled'}
    stages = {}
    for key, event in settled.items():
        if key not in reserved:
            continue
        reservation = reserved[key]
        stage = stages.setdefault(reservation['payload']['purpose'], {'calls':0,'seconds':0,'cost_usd':0,'usage':{}})
        stage['calls'] += 1
        stage['seconds'] += (datetime.fromisoformat(event['received_at']) - datetime.fromisoformat(reservation['received_at'])).total_seconds()
        stage['cost_usd'] += event['payload']['actual_usd']
        for k,v in event['payload']['usage'].items():
            if type(v) is int:stage['usage'][k] = stage['usage'].get(k,0) + v
    pending = sum(e['payload']['reserved_usd'] for k,e in reserved.items() if k not in settled)
    actual = sum(e['payload']['actual_usd'] for e in settled.values())
    return {'stages':stages, 'settled_cost_usd':actual, 'pending_bound_usd':pending,
            'cost_settled':set(reserved)==set(settled), 'model_calls':len(reserved)}


def summarize(rows):
    summary = {}
    for arm in ('original','candidate'):
        items = [r for r in rows if r['arm']==arm]
        exact = [r for r in items if r.get('evaluation',{}).get('exact_reference_pass') is not None]
        metrics = {}
        for name in ('first_preview_s','settled_preview_s','elapsed_s'):
            values = [r[name] for r in items if r.get(name) is not None and
                      (r.get('ready') if name=='first_preview_s' else
                       r.get('settled_eligible') if name=='settled_preview_s' else
                       r.get('evaluation',{}).get('answer_chars',0)>0)]
            metrics[name] = {'n':len(values),'median':statistics.median(values) if values else None}
        stages = {}
        for row in items:
            for purpose, usage in row.get('accounting',{}).get('stages',{}).items():
                aggregate = stages.setdefault(purpose, {'calls':0,'seconds':0,'cost_usd':0})
                for field in aggregate:
                    aggregate[field] += usage.get(field,0)
        clarifications = [r['evaluation']['expected_clarification_pass'] for r in items
                          if r.get('evaluation',{}).get('expected_clarification_pass') is not None]
        summary[arm] = {'attempted':len(items),'ready':sum(r.get('ready',False) for r in items),
            'settled_eligible':sum(r.get('settled_eligible',False) for r in items),
            'answers':sum(r.get('evaluation',{}).get('answer_chars',0)>0 for r in items),
            'exact_reference_passes':sum(r['evaluation']['exact_reference_pass'] for r in exact),
            'exact_reference_cases':len(exact), 'latency_successful_only':metrics,
            'expected_clarifications':{'passed':sum(clarifications),'cases':len(clarifications)},
            'model_stages':stages,
            'cost_usd_all_attempts':sum(r.get('accounting',{}).get('settled_cost_usd',0) for r in items),
            'pending_bound_usd':sum(r.get('accounting',{}).get('pending_bound_usd',0) for r in items),
            'database_reads':sum(r.get('work',{}).get('retrieve',0) for r in items),
            'gpu_requests':sum(r.get('work',{}).get('generate',0) for r in items)}
    pairs = {r['case']:{x['arm']:x for x in rows if x['case']==r['case']} for r in rows}
    summary['matched_successes'] = {}
    for name in ('first_preview_s','settled_preview_s','elapsed_s'):
        matched = [p for p in pairs.values() if set(p)=={'original','candidate'}
                   and all(r.get('settled_eligible')
                           and r.get(name) is not None for r in p.values())]
        summary['matched_successes'][name] = {'n':len(matched), **{arm:statistics.median(p[arm][name] for p in matched)
            if matched else None for arm in ('original','candidate')}}
    return summary


async def run(args):
    import httpx
    from deploy_results.manage import read_protected_env
    os.environ.update(read_protected_env(args.env))
    from pankagent_vnext.config import Settings
    from pankagent_vnext.graph import GraphAdapter
    from pankagent_vnext.llm import ClaudeGateway
    from pankagent_vnext.app import create_app
    from pankagent_vnext.planning_contract import VerifiedCache
    from pankagent_vnext.preplanning_grounding import warm_grounding
    os.umask(0o077)
    args.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = json.loads(args.manifest.read_text())
    selected_keys = set(args.case_keys or [])
    known_keys = {case['key'] for case in manifest['cases']}
    if selected_keys-known_keys:
        raise ValueError('Unknown case keys: '+','.join(sorted(selected_keys-known_keys)))
    if selected_keys and not args.resume:
        for case in manifest['cases']:
            if case['key'] in selected_keys and case.get('parent') and case['parent'] not in selected_keys:
                raise ValueError('Selected follow-ups require their parent cases in a fresh run')
    if not (args.ledger/'budget.sqlite3').is_file():
        raise ValueError('An existing authorized cumulative ledger is required')
    common = replace(Settings(), model=manifest['model'], budget_usd=args.ceiling,
        budget_dir=str(args.ledger), provider_status_url='', plan_cache_enabled=False,
        reasoning_effort='none', cypher_url='http://127.0.0.1:33917')
    # Read-only references cannot incur API cost.
    frozen_path = args.root/'references.json'
    manifest_hash = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    if not frozen_path.exists():
        graph = GraphAdapter(common)
        references = {'manifest_sha256':manifest_hash,'graph_version':common.graph_version,'cases':{}}
        try:
            rows = await graph._small_query('MATCH (d:donor) WHERE d.data_source=$source RETURN DISTINCT d.t1d_stage AS stage,d.diabetes_type AS clinical',{'source':'HPAP'})
            inventory = {}
            for n in ('1','2','3'):
                values = {r['stage'] for r in rows if str(r.get('stage') or '').startswith('Stage '+n+':')}
                if len(values)!=1:raise ValueError('Ambiguous stage inventory')
                inventory['stage'+n] = next(iter(values))
            values = {r['clinical'] for r in rows if str(r.get('clinical') or '').casefold() in {'t1d','type 1 diabetes','diabetes (type i)'}}
            if len(values)!=1:raise ValueError('Ambiguous clinical inventory')
            inventory['t1d_category'] = next(iter(values))
            for case in manifest['cases']:
                checks = []
                for spec in case['checks']:
                    params = {**spec['parameters'], **inventory}
                    try:
                        rows = await graph._small_query(spec['query'],params)
                        checks.append({'ok':True,'ids':sorted({str(r['id']) for r in rows}),'parameters':params})
                    except Exception as exc:
                        checks.append({'ok':False,'error':type(exc).__name__})
                references['cases'][case['key']] = {'checks':checks}
            dump(frozen_path,references)
        finally:
            await graph.close()
    frozen = json.loads(frozen_path.read_text())
    if frozen['manifest_sha256'] != manifest_hash:raise ValueError('Frozen reference manifest changed')
    if args.references_only:
        print(json.dumps({'reference_cases':len(frozen['cases']), 'errors':sum(not ch['ok'] for c in frozen['cases'].values() for ch in c['checks']), 'model_calls':0}),flush=True)
        return
    report_path = args.root/'report.jsonl'
    if report_path.exists() and not args.resume:
        raise ValueError('Never overwrite or implicitly resume a live comparison; use --resume after it stops')
    saved_manifest = json.loads((args.root/'run-manifest.json').read_text()) if args.resume else None
    if saved_manifest and saved_manifest['manifest_sha256'] != manifest_hash:
        raise ValueError('Cannot resume a different question manifest')
    class NoLiterature:
        async def search(self,*a,**kw):return {'status':'unavailable','perspectives':[],'note':'Graph workflow comparison; literature disabled equally.'}
        async def probe(self):return {'state':'unavailable'}
        async def close(self):pass
    keyvar = ContextVar('case',default=None)
    work = {}
    rows = [json.loads(line) for line in report_path.read_text().splitlines()] if args.resume else []
    prior = {'original':{},'candidate':{}}
    completed = set()
    cases_by_key = {case['key']:case for case in manifest['cases']}
    for row in rows:
        key = (row['case'],row['arm'])
        if key in completed or row['question'] != cases_by_key[row['case']]['question']:
            raise ValueError('Duplicate or changed completed case')
        completed.add(key)
        artifact = json.loads((args.root/row['arm']/(row['case']+'.json')).read_text())
        prior[row['arm']][row['case']] = artifact['run']
    async with AsyncExitStack() as stack:
        arms = {}
        for arm in ('original','candidate'):
            settings = replace(common,state_dir=args.root/arm/'sessions',competing_candidates=arm=='candidate')
            gateway, graph = ClaudeGateway(settings), GraphAdapter(settings)
            for metric, method in [('retrieve','_retrieve'),('explain','_explain'),('generate','_generate'),('metadata_reads','_small_query')]:
                original = getattr(graph,method)
                async def wrapper(*a,_method=original,_metric=metric,**kw):
                    key = keyvar.get(); start = time.monotonic()
                    if key:work.setdefault(key,{})[_metric] = work.setdefault(key,{}).get(_metric,0)+1
                    try:return await _method(*a,**kw)
                    finally:
                        if key:
                            name = _metric+'_seconds';work[key][name] = work[key].get(name,0)+time.monotonic()-start
                setattr(graph,method,wrapper)
            app = create_app(settings,gateway,graph,NoLiterature())
            await stack.enter_async_context(app.router.lifespan_context(app))
            await warm_grounding(graph)
            client = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1',timeout=240))
            arms[arm] = (app,client,gateway,graph)
        initial_budget = await arms['original'][2].budget.asnapshot()
        run_manifest = {'manifest':manifest,'manifest_sha256':manifest_hash,'budget_before':initial_budget,
            'ceiling_usd':args.ceiling,'code_identity':arms['original'][3].preview_identity(),'confirmation':'settled latest preview','settings':{
            k:getattr(common,k) for k in ['model','reasoning_effort','plan_timeout','grouped_preview_timeout','answer_timeout','graph_timeout','cypher_timeout','cypher_initial_requests','max_nodes','max_edges','max_bytes']}}
        if saved_manifest:
            if any(saved_manifest[k] != run_manifest[k] for k in ('code_identity','settings','confirmation')):
                raise ValueError('Resume requires identical backend identity, limits and models')
            saved_manifest.setdefault('resumptions',[]).append({'at':datetime.now().isoformat(),
                'completed_attempts':len(rows),'budget_before':initial_budget,'ceiling_usd':args.ceiling})
            run_manifest = saved_manifest
        dump(args.root/'run-manifest.json',run_manifest)
        for index,case in enumerate(manifest['cases'][:args.limit]):
            if selected_keys and case['key'] not in selected_keys:
                continue
            for arm in (('original','candidate') if index%2==0 else ('candidate','original')):
                if (case['key'],arm) in completed:
                    continue
                app,client,gateway,graph = arms[arm]
                budget = await gateway.budget.asnapshot()
                if budget['remaining_usd'] < .12:
                    dump(args.root/'summary.json',{'incomplete':'remaining_budget','arms':summarize(rows),'budget':budget})
                    return
                key = arm+'/'+case['key'];token = keyvar.set(key);started = time.monotonic()
                graph._query_cache = VerifiedCache()
                parent = prior[arm].get(case.get('parent'))
                record = {'case':case['key'],'arm':arm,'question':case['question'],'order':len(rows),'ready':False}
                raw = None
                run_id = None
                try:
                    response = await client.post('/v2/plans',json=plan_request(case,parent))
                    response.raise_for_status();created=response.json();run_id=created['run_id']
                    async def wait(states):
                        async with asyncio.timeout(245):
                            while True:
                                current = await app.state.runtime.io.call(app.state.runtime.store.get,run_id)
                                if current['status'] in states:return current
                                await asyncio.sleep(.05)
                    raw = await wait({'awaiting_confirmation','failed','cancelled','completed','partial'})
                    record['ready'] = raw['status']=='awaiting_confirmation'
                    record['first_preview_s'] = time.monotonic()-started if record['ready'] else None
                    race = app.state.runtime.candidate_previews.get(run_id)
                    if race and not race.task.cancelled():
                        try:
                            await asyncio.wait_for(asyncio.shield(race.task),130)
                        except asyncio.CancelledError:
                            if not race.task.cancelled():raise
                    raw = await app.state.runtime.io.call(app.state.runtime.store.get,run_id)
                    record['settled_preview_s'] = time.monotonic()-started if record['ready'] else None
                    record['settled_eligible'] = bool((raw.get('preview') or {}).get('confirmation_eligible'))
                    if raw['status']=='awaiting_confirmation':
                        response = await client.post('/v2/plans/'+raw['plan_id']+'/confirm')
                        record['confirm_http_status'] = response.status_code
                        if response.status_code==202:raw=await wait({'completed','partial','failed','cancelled'})
                        else:
                            record['settled_eligible'] = False
                            record['confirmation_error'] = response.json()
                            raw = await app.state.runtime.io.call(app.state.runtime.store.get,run_id)
                    prior[arm][case['key']] = raw
                    audit = await app.state.runtime.io.call(app.state.runtime.store.audit_snapshot,run_id)
                    events = [];cursor=0
                    while True:
                        batch = await app.state.runtime.io.call(app.state.runtime.store.events_after,run_id,cursor)
                        if not batch:break
                        events.extend(batch);cursor=batch[-1]['sequence']
                    ready_events = [e for e in events if e['type']=='plan_ready']
                    record.update(status=raw['status'],evaluation=evaluate(case,frozen['cases'][case['key']],raw),
                        accounting=model_accounting(audit),preview_versions=len(ready_events),work=work.get(key,{}))
                    record['budget_limited'] = bool((raw.get('error') or {}).get('category') == 'budget_exhausted' or
                        ((raw.get('evidence') or {}).get('synthesis_error') or {}).get('category') == 'budget_exhausted')
                    record['assistance_calls'] = sum(e['kind']=='execution_repair_claim' for e in audit['events'])
                    record['candidate_decisions'] = [e['payload'].get('decision') for e in audit['events'] if e['kind']=='query_candidate']
                    deltas = ''.join(e['payload'].get('text','') for e in events if e['type']=='graph_answer' and e['payload'].get('delta'))
                    record['stream_match'] = deltas == (raw.get('graph_answer') or '')
                    path=args.root/arm/(case['key']+'.json');path.parent.mkdir(exist_ok=True,mode=0o700)
                    dump(path,{'case':case,'run':raw,'events':events,'audit':audit,'reference':frozen['cases'][case['key']]})
                except Exception as exc:
                    record['harness_error'] = type(exc).__name__
                    # Never proceed with an unfinished competitor sharing capacity.
                    if run_id:
                        await client.post('/v2/runs/'+run_id+'/cancel')
                        audit = await app.state.runtime.io.call(app.state.runtime.store.audit_snapshot,run_id)
                        record['accounting'] = model_accounting(audit)
                        raw = await app.state.runtime.io.call(app.state.runtime.store.get,run_id)
                        path = args.root/arm/(case['key']+'.json');path.parent.mkdir(exist_ok=True,mode=0o700)
                        dump(path,{'case':case,'run':raw,'audit':audit,'harness_error':type(exc).__name__})
                finally:
                    record['elapsed_s'] = time.monotonic()-started
                    rows.append(record)
                    with (args.root/'report.jsonl').open('a') as out:out.write(json.dumps(record,default=str)+'\n')
                    dump(args.root/'summary.json',{'arms':summarize(rows),'budget':await gateway.budget.asnapshot()})
                    print(json.dumps({k:v for k,v in record.items() if k not in {'evaluation','accounting','candidate_decisions','question'}}),flush=True)
                    keyvar.reset(token)
                if record.get('budget_limited'):
                    dump(args.root/'summary.json',{'incomplete':'model_reservation_exceeds_remaining_budget',
                        'arms':summarize(rows),'budget':await gateway.budget.asnapshot()})
                    return
        dump(args.root/'summary.json',{'complete':len(rows)==2*len(manifest['cases']),'arms':summarize(rows),
            'budget':await arms['original'][2].budget.asnapshot()})


if __name__ == '__main__':
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--env',type=Path,required=True)
    parser.add_argument('--ledger',type=Path,required=True)
    parser.add_argument('--ceiling',type=float,required=True)
    parser.add_argument('--manifest',type=Path,default=REPO/'tests_vnext/fixtures/acceptance/workflow50.json')
    parser.add_argument('--references-only',action='store_true')
    parser.add_argument('--limit',type=int,default=50)
    parser.add_argument('--resume',action='store_true',help='Resume a stopped comparison, preserving completed attempts and cumulative accounting')
    parser.add_argument('--case-keys',nargs='+',help='Explicit bounded subset; include parent cases in a fresh run')
    args = parser.parse_args()
    os.umask(0o077)
    args.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (args.root/'.benchmark.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(run(args))
