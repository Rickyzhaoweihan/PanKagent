"""Single original-workflow arm; isolated ASGI execution, cumulative capped ledger.

Gold fixture is consumed by evaluation only, never supplied to the agent.
Use separate processes/checkouts for baseline and improvement. No deployment.
"""
import argparse, asyncio, fcntl, hashlib, json, os, sys, time
from pathlib import Path
from dataclasses import replace
from contextlib import AsyncExitStack
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from workflow50 import dump, model_accounting
from workflow57 import evaluate, group_summary


def frozen_manifest(path, value):
    encoded=json.dumps(value,sort_keys=True,indent=2)+'\n'
    if path.exists() and path.read_text()!=encoded:
        raise ValueError('Frozen manifest changed; use a new run directory')
    if not path.exists():path.write_text(encoded)


def available_budget(snapshot):
    """Reservations remain charged even when a provider outcome is unknown."""
    return min(float(snapshot.get('remaining_usd', 0)),
               max(0, float(snapshot.get('limit_usd', 0))
                   - float(snapshot.get('spent_usd', 0))
                   - float(snapshot.get('reserved_usd', 0))))


def budget_failure(*diagnostics):
    """Inspect error diagnostics, never question text or returned graph values."""
    for value in diagnostics:
        if isinstance(value, dict):
            reason = budget_failure(*value.values())
        elif isinstance(value, (list, tuple)):
            reason = budget_failure(*value)
        elif isinstance(value, str):
            text = value.lower().strip()
            reason = ('budget_exhausted' if text in {
                'budget_exhausted', 'development_budget_exhausted', 'budgetexceeded'}
                or 'evaluation budget is' in text else
                'provider_billing' if text in {'billing', 'insufficient_quota',
                    'billing_hard_limit_reached'} or 'credit balance is too low' in text
                or 'insufficient credits' in text else None)
        else:
            reason = None
        if reason:return reason
    return None


def budget_response_failure(response):
    if response.status_code == 402:return 'provider_billing'
    if response.status_code not in (400, 429, 503):return None
    try:payload = response.json()
    except ValueError:payload = response.text
    reason = budget_failure(payload)
    return 'budget_admission' if reason and response.status_code == 503 else reason


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
    from pankagent_vnext.agent_schemas import run_context
    if not (args.ledger/'budget.sqlite3').is_file():raise ValueError('Initialize an explicitly authorized round ledger first')
    fixture=json.loads(args.fixture.read_text())
    cases=fixture['cases'];known={c['id'] for c in cases}
    if set(args.case_keys or [])-known:raise ValueError('Unknown case')
    if args.case_keys and len(set(args.case_keys)) != len(args.case_keys):raise ValueError('Duplicate case selection')
    by_id={c['id']:c for c in cases}
    selected=[by_id[key] for key in args.case_keys] if args.case_keys else cases
    def summary(rows, **fields):
        return {'attempts':len(rows),'selected_cases':len(selected),
                'unattempted_cases':[c['id'] for c in selected if c['id'] not in {r['case'] for r in rows}],
                'core_covered':sum(r.get('evaluation',{}).get('verified_core_covered',False) for r in rows),
                'groups':group_summary(rows,selected),**fields}
    settings=replace(Settings(),model=args.model,budget_dir=str(args.ledger),budget_usd=args.ceiling,
        state_dir=args.root/'sessions',provider_status_url='',plan_cache_enabled=False,reasoning_effort='none',
        cypher_url='http://127.0.0.1:33917')
    frozen_manifest(args.root/'manifest.json',{'fixture_sha256':hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        'context':run_context(settings),'arm':args.arm,'model':args.model,'ceiling':args.ceiling,
        'literature':False,'query_cache':'reset per case','inventory':'warm','include_context':True,'selected_cases':[c['id'] for c in selected]})
    report=args.root/'report.jsonl'
    rows=[json.loads(x) for x in report.read_text().splitlines()] if report.exists() else []
    if rows and not args.resume:raise ValueError('Use --resume; completed attempts are immutable')
    done={r['case'] for r in rows}
    inflight=args.root/'inflight.json'
    if inflight.exists():
        pending=json.loads(inflight.read_text())
        if pending['case'] not in done:
            raise ValueError('Interrupted attempt retained at inflight.json; recover its existing run before resuming. Never resubmit a paid request automatically.')
        inflight.unlink()
    class Literature:
        async def search(self,*a,**kw):return {'status':'unavailable','perspectives':[],'note':'Disabled equally for evaluation'}
        async def probe(self):return {'state':'unavailable'}
        async def close(self):pass
    gateway,graph=ClaudeGateway(settings),GraphAdapter(settings)
    app=create_app(settings,gateway,graph,Literature())
    counts={}
    for name,method in [('retrieve','_retrieve'),('generate','_generate'),('explain','_explain')]:
        original=getattr(graph,method)
        async def wrapped(*a,_name=name,_method=original,**kw):
            counts[_name]=counts.get(_name,0)+1
            return await _method(*a,**kw)
        setattr(graph,method,wrapped)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        await warm_grounding(graph)
        client=await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1',timeout=250))
        runtime=app.state.runtime
        for case in selected:
            if case['id'] in done:continue
            budget=await gateway.budget.asnapshot()
            if available_budget(budget)<.15:
                dump(args.root/'summary.json',summary(rows,complete=False,incomplete='budget',budget=budget));return
            await warm_grounding(graph)
            counts.clear();graph._query_cache=VerifiedCache();start=time.monotonic();run_id=None
            row={'case':case['id'],'question':case['question'],'arm':args.arm};stop_reason=None
            dump(inflight, {'case':case['id'],'state':'submitting','run_id':None})
            try:
                response=await client.post('/v2/plans',json={'question':case['question'],'include_context':True,'event_source':'audit_replay'})
                stop_reason=budget_response_failure(response)
                response.raise_for_status();run_id=response.json()['run_id']
                dump(inflight, {'case':case['id'],'state':'running','run_id':run_id})
                async def wait(states):
                    async with asyncio.timeout(250):
                        while True:
                            current=await runtime.io.call(runtime.store.get,run_id)
                            if current['status'] in states:return current
                            await asyncio.sleep(.05)
                raw=await wait({'awaiting_confirmation','failed','cancelled','completed','partial'})
                row['ready']=raw['status']=='awaiting_confirmation'
                row['first_preview_s']=row['settled_preview_s']=time.monotonic()-start if row['ready'] else None
                row['settled_eligible']=bool((raw.get('preview') or {}).get('confirmation_eligible'))
                if row['ready']:
                    response=await client.post('/v2/plans/'+raw['plan_id']+'/confirm')
                    row['confirm_http_status']=response.status_code
                    stop_reason=budget_response_failure(response)
                    if response.status_code==202:raw=await wait({'completed','partial','failed','cancelled'})
                stop_reason=stop_reason or budget_failure(raw.get('error'),
                    (raw.get('evidence') or {}).get('synthesis_error'),
                    (raw.get('evidence') or {}).get('answer_preparation_error'))
                row.update(status=raw['status'],evaluation=evaluate(case,raw))
                audit=await runtime.io.call(runtime.store.audit_snapshot,run_id)
                events=[];cursor=0
                while True:
                    batch=await runtime.io.call(runtime.store.events_after,run_id,cursor)
                    if not batch:break
                    events+=batch;cursor=batch[-1]['sequence']
                dump(args.root/(case['id']+'.json'),{'case':{'key':case['id'],'question':case['question']},'run':raw,'audit':audit,'events':events})
                row['accounting']=model_accounting(audit)
            except Exception as exc:
                stop_reason=stop_reason or budget_failure(type(exc).__name__,str(exc))
                row['harness_error']=type(exc).__name__
                if run_id:
                    await client.post('/v2/runs/'+run_id+'/cancel')
                    audit=await runtime.io.call(runtime.store.audit_snapshot,run_id)
                    row['accounting']=model_accounting(audit)
                    raw=await runtime.io.call(runtime.store.get,run_id)
                    dump(args.root/(case['id']+'.json'),{'run':raw,'audit':audit,'harness_error':type(exc).__name__})
            if stop_reason:row.update(evaluation_status='incomplete_budget',budget_stop_reason=stop_reason)
            row.update(elapsed_s=time.monotonic()-start,work=dict(counts));rows.append(row)
            with report.open('a') as out:out.write(json.dumps(row,default=str)+'\n')
            budget=await gateway.budget.asnapshot()
            fields={'incomplete':'budget','budget_stop_reason':stop_reason,'stopped_case':case['id']} if stop_reason else {}
            dump(args.root/'summary.json',summary(rows,complete=False,budget=budget,**fields))
            inflight.unlink()
            print(json.dumps({'case':case['id'],'status':row.get('status'),'core':row.get('evaluation',{}).get('verified_core_covered'),'cost':row.get('accounting',{}).get('settled_cost_usd')}),flush=True)
            if stop_reason:return
        dump(args.root/'summary.json',summary(rows,complete=True,budget=await gateway.budget.asnapshot()))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['root','env','ledger']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--ceiling',type=float,required=True);p.add_argument('--arm',required=True)
    p.add_argument('--model',default='claude-sonnet-5-5');p.add_argument('--resume',action='store_true')
    p.add_argument('--case-keys',nargs='+');p.add_argument('--fixture',type=Path,default=ROOT/'tests_vnext/fixtures/acceptance/workflow57.json')
    args=p.parse_args();os.umask(0o077);args.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (args.root/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);asyncio.run(run(args))
if __name__=='__main__':main()
