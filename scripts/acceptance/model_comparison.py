"""Provider comparison derived from the existing acceptance replay.
Identical runtime, evidence validation, and reference queries; graph-only.
Environment adds PANK_COMPARE_MODEL, PANK_COMPARE_LIMIT and optional definition case.
Budget state MUST point to the newly authorized cumulative provider ledger.
"""
import os,sys,json,asyncio,time,hashlib,re
from pathlib import Path
from dataclasses import replace
ROOT=Path(os.environ['PANK_ACCEPTANCE_ROOT']);ROOT.mkdir(parents=True,exist_ok=True,mode=0o700);os.umask(0o077)
if (ROOT/'report.jsonl').exists():raise RuntimeError('Use a new output directory; never overwrite acceptance evidence')
release=Path(os.environ.get('PANK_ACCEPTANCE_CODE',str(Path(__file__).resolve().parents[2])));sys.path.insert(0,str(release))
from deploy_results.manage import read_protected_env
os.environ.update(read_protected_env(Path(os.environ['PANK_ACCEPTANCE_ENV'])))
import httpx
from pankagent_vnext.config import Settings
from pankagent_vnext.graph import GraphAdapter
from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.app import create_app
from pankagent_vnext.composable_planning import answer_results
from pankagent_vnext.diagnostics import diagnostic
class NoLiterature:
 async def search(self,*args):return {'status':'unavailable','perspectives':[],'note':'Graph-only variant validation.'}
 async def probe(self):return {'state':'unavailable'}
 async def close(self):pass
manifest=json.loads((Path(__file__).resolve().parents[2]/'tests_vnext/fixtures/acceptance/regression.json').read_text())
cases=[c for c in manifest['cases'] if c['suite']==os.environ.get('PANK_ACCEPTANCE_SUITE','controls')]
if os.environ.get('PANK_ACCEPTANCE_CASES'):cases=[c for c in cases if c['key'] in os.environ['PANK_ACCEPTANCE_CASES'].split(',')]
if os.environ.get('PANK_COMPARE_DEFINITION')=='1':
 cases.insert(0,{'key':'definition','question':'What is T1D?','kind':'disease','parameters':{},'group':'definition','reference_query':'MATCH (d:disease {id:"MONDO_0005147"}) RETURN d.id AS id'})
for case in cases:
 if case['key']=='followup':case['parent']='stage-case'
base={'source':'HPAP','clinical':'Control Without Diabetes','disease':'MONDO_0005147'}
async def main():
 settings=replace(Settings(),state_dir=ROOT/'sessions',model=os.environ['PANK_COMPARE_MODEL'],budget_usd=float(os.environ['PANK_COMPARE_LIMIT']),budget_dir=os.environ['PANK_ACCEPTANCE_BUDGET_STATE'],provider_status_url='',plan_cache_enabled=False,reasoning_effort='none')
 if not (Path(os.environ['PANK_ACCEPTANCE_BUDGET_STATE'])/'budget.sqlite3').is_file():
  raise RuntimeError('Existing cumulative validation ledger required; never reset the allowance')
 gateway=ClaudeGateway(replace(settings,state_dir=Path(os.environ['PANK_ACCEPTANCE_BUDGET_STATE'])));graph=GraphAdapter(settings);app=create_app(settings,gateway,graph,NoLiterature())
 rows=await graph._small_query('MATCH (d:donor) WHERE d.data_source=$source RETURN DISTINCT d.t1d_stage AS stage,d.diabetes_type AS clinical',{'source':'HPAP'})
 for n in ['1','2','3']:
  vals={r['stage'] for r in rows if str(r.get('stage') or '').startswith('Stage '+n+':')};assert len(vals)==1;base['stage'+n]=next(iter(vals))
 vals={r['clinical'] for r in rows if str(r.get('clinical') or '').casefold() in {'t1d','type 1 diabetes','diabetes (type i)'}};assert len(vals)==1;base['t1d_category']=next(iter(vals))
 from contextvars import ContextVar
 keyvar=ContextVar('test_case',default='unknown');original=gateway._create
 async def traced(*args,**kwargs):
  started=time.monotonic();response=await original(*args,**kwargs)
  model_times.setdefault(keyvar.get(),[]).append(time.monotonic()-started)
  reservations.setdefault(keyvar.get(),[]).append(args[0])
  with (ROOT/(keyvar.get()+'-model.jsonl')).open('a') as out:out.write(json.dumps(response.model_dump(),default=str)+'\n')
  return response
 gateway._create=traced
 reports=[];prior={};lock=asyncio.Semaphore(1);model_times={};reservations={}
 (ROOT/'case-manifest.json').write_text(json.dumps({'release':str(release),'manifest':manifest,'cases':cases},indent=2))
 async with app.router.lifespan_context(app):
  from pankagent_vnext.preplanning_grounding import warm_grounding
  await warm_grounding(graph)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1',timeout=180) as client:
   async def test(case):
    async with lock:
     token=keyvar.set(case['key']);start=time.monotonic();params={**base,**case['parameters']}
     # Inventory values were resolved after construction, so retain those here.
     params.update({k:v for k,v in base.items() if k.startswith('stage') or k=='t1d_category'})
     try:
      expected={str(r['id']) for r in await graph._small_query(case['reference_query'],params)} if case.get('reference_query') else set()
      ref_ok=bool(case.get('reference_query'))
     except Exception as exc:
      expected=set();ref_ok=False
     parent=prior.get(case.get('parent'))
     plan_start=time.monotonic()
     created=(await client.post('/v2/plans',json={'question':case['question'],'session_id':parent.get('session_id') if parent else None,'event_source':'audit_replay'})).json()
     if 'run_id' not in created:
      print(json.dumps({'case':case['key'],'status':'not_admitted'}),flush=True);return
     async def wait(states):
      for _ in range(2300):
       run=(await client.get('/v2/runs/'+created['run_id'])).json()
       if run['status'] in states:return run
       await asyncio.sleep(.1)
      return run
     run=await wait({'awaiting_confirmation','failed','cancelled','completed','partial'})
     preview_seconds=time.monotonic()-plan_start
     ready=run['status']=='awaiting_confirmation'
     if ready:
      await client.post('/v2/plans/'+created['plan_id']+'/confirm');run=await wait({'completed','partial','failed','cancelled'})
     raw=await app.state.runtime.io.call(app.state.runtime.store.get,created['run_id']);prior[case['key']]=raw
     evidence=raw.get('evidence') or (raw.get('preview') or {}).get('evidence') or {};steps=evidence.get('steps',[])
     previous={s['step_id']:s for s in steps}
     try:selected=answer_results(raw.get('plan') or {},previous)
     except Exception:selected=previous
     if case['kind']=='coloc':
      actual={str(e.get('properties',{}).get('gwas_signal_id') or '')+'|'+str(e.get('properties',{}).get('qtl_signal_id') or '') for s in selected.values() for e in s.get('edges',[]) if e.get('type')=='SIGNAL_COLOC_WITH'}
     else:
      actual={str(n['id']) for s in selected.values() for n in s.get('nodes',[]) if case['kind'] in n.get('labels',[])}-set(case.get('omit',[]))
     # Enrichment questions may return independent detection context. Compare the
     # requested relation's endpoints, without deleting that useful context.
     if case['key'].startswith(('cftr-paraphrase','cftr-alias')):
      actual={str(e['end_id']) for result in selected.values() for e in result.get('edges',[]) if e.get('type')=='GENE_ENRICHED_IN'}
     audit=await app.state.runtime.io.call(app.state.runtime.store.audit_snapshot,created['run_id'])
     from datetime import datetime
     reserved={e['payload']['reservation_id']:e for e in audit['events'] if e['kind']=='model_reserved'}
     settled_events={e['payload']['reservation_id']:e for e in audit['events'] if e['kind']=='model_settled'}
     settled=bool(reserved) and set(reserved)==set(settled_events)
     cost=sum(e['payload']['actual_usd'] for e in settled_events.values()) if settled else None
     usage={};model_elapsed=0;stages={}
     for rid,event in settled_events.items():
      if rid in reserved:
       seconds=(datetime.fromisoformat(event['received_at'])-datetime.fromisoformat(reserved[rid]['received_at'])).total_seconds();model_elapsed+=seconds
       purpose=reserved[rid]['payload']['purpose'];stage=stages.setdefault(purpose,{'calls':0,'seconds':0,'cost_usd':0,'usage':{}})
       stage['calls']+=1;stage['seconds']+=seconds;stage['cost_usd']+=event['payload']['actual_usd']
       for field,value in event['payload']['usage'].items():
        if type(value) is int:stage['usage'][field]=stage['usage'].get(field,0)+value
      for key,value in event['payload']['usage'].items():
       if type(value) is int:usage[key]=usage.get(key,0)+value
     events=[];cursor=0
     while True:
      batch=await app.state.runtime.io.call(app.state.runtime.store.events_after,created['run_id'],cursor)
      if not batch:break
      events.extend(batch);cursor=batch[-1]['sequence']
     answer=raw.get('graph_answer') or '';deltas=''.join(e['payload'].get('text','') for e in events if e['type']=='graph_answer' and e['payload'].get('delta'))
     complete=bool(selected) and all(s.get('status') in {'complete','empty'} and not s.get('truncated') for s in selected.values())
     report={'model':settings.model,'stages':stages,'plan_ready':ready,'case':case['key'],'question':case['question'],'status':run['status'],'reference_ok':ref_ok,'reference_count':len(expected),'actual_count':len(actual),'membership_match':ref_ok and actual==expected,'retrieval_complete':complete,'answer_chars':len(answer),'stream_match':answer==deltas,'codes':sorted({d.get('code') for d in run.get('diagnostics',[])}),'model_seconds':round(model_elapsed,3),'claude_calls':len(reserved),'cost_settled':settled,'usage':usage,'settled_cost_usd':cost,'planning_route':(raw.get('plan') or {}).get('planning_route'),'preview_s':round(preview_seconds,3),'elapsed_s':round(time.monotonic()-start,3),'steps':[(s.get('step_id'),s.get('status')) for s in steps],'expected_clarification':case.get('expected_clarification',False)}
     artifact={'case':case,'reference':{'query':case['reference_query'],'parameters':params,'ids':sorted(expected)},'run':raw,'public_run':run,'events':events,'audit':audit,'report':report}
     (ROOT/(case['key']+'.json')).write_text(json.dumps(artifact,default=str,indent=2));reports.append(report)
     with (ROOT/'report.jsonl').open('a') as out:out.write(json.dumps(report)+'\n')
     print(json.dumps({k:v for k,v in report.items() if k not in ['stages','usage']}),flush=True);keyvar.reset(token)
   for group in dict.fromkeys(c['group'] for c in cases):
    group_cases=[c for c in cases if c['group']==group]
    parents={c.get('parent') for c in group_cases}
    for case in group_cases:
     if case['key'] in parents:await test(case)
    for case in group_cases:
     if case['key'] not in parents:await test(case)
   (ROOT/'summary.json').write_text(json.dumps({'release':str(release),'reports':reports,'budget':gateway.budget.snapshot()},indent=2))
   print('BUDGET '+json.dumps(gateway.budget.snapshot()),flush=True)
asyncio.run(main())
