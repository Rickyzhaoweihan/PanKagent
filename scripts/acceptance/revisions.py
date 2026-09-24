import os,sys,json,asyncio,time
from pathlib import Path
from dataclasses import replace
ROOT=Path(os.environ['PANK_ACCEPTANCE_ROOT'])
ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from deploy_results.manage import read_protected_env
os.environ.update(read_protected_env(Path(os.environ['PANK_ACCEPTANCE_ENV'])))
os.umask(0o077)
import httpx
from pankagent_vnext.config import Settings
from pankagent_vnext.graph import GraphAdapter
from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.app import create_app
class NoLiterature:
 async def search(self,*args):return {'status':'unavailable','perspectives':[],'note':'Literature excluded from isolated graph acceptance.'}
 async def probe(self):return {'state':'unavailable'}
 async def close(self):pass
async def main():
 import faulthandler
 stack=(ROOT/'runtime-stack.log').open('w');faulthandler.dump_traceback_later(30,repeat=True,file=stack)
 settings=replace(Settings(),state_dir=ROOT/('runtime-'+str(int(time.time()))),budget_usd=10,provider_status_url='')
 budget_settings=replace(settings,state_dir=Path(os.environ['PANK_ACCEPTANCE_BUDGET_STATE']))
 gateway=ClaudeGateway(budget_settings);graph=GraphAdapter(settings);app=create_app(settings,gateway,graph,NoLiterature())
 original_create=gateway._create
 async def traced(*args,**kwargs):
  reply=await original_create(*args,**kwargs)
  with (ROOT/'runtime-model.jsonl').open('a') as out:out.write(json.dumps(reply.model_dump(),default=str)+'\n')
  return reply
 gateway._create=traced
 async with app.router.lifespan_context(app):
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1',timeout=180) as client:
   async def query(label,q,session=None):
    start=time.monotonic();created=(await client.post('/v2/plans',json={'question':q,'session_id':session})).json()
    async def wait(states):
     for _ in range(600):
      r=(await client.get('/v2/runs/'+created['run_id'])).json()
      if r['status'] in states:return r
      await asyncio.sleep(1)
     return r
    run=await wait({'awaiting_confirmation','failed','cancelled','completed','partial'})
    if run['status']=='awaiting_confirmation':
     await client.post('/v2/plans/'+created['plan_id']+'/confirm')
     run=await wait({'completed','partial','failed','cancelled'})
    events=[];cursor=0
    while True:
     batch=await app.state.runtime.io.call(app.state.runtime.store.events_after,created['run_id'],cursor)
     if not batch:break
     events.extend(batch);cursor=batch[-1]['sequence']
    deltas=''.join(e['payload'].get('text','') for e in events if e['type']=='graph_answer' and e['payload'].get('delta'))
    refresh=(await client.get('/v2/runs/'+created['run_id'])).json()
    print(json.dumps({'label':label,'stream_matches':deltas==run.get('graph_answer',''),'refresh_matches':refresh.get('graph_answer')==run.get('graph_answer'),'event_count':len(events)}),flush=True)
    raw=await app.state.runtime.io.call(app.state.runtime.store.get,created['run_id'])
    (ROOT/('runtime-'+label+'.json')).write_text(json.dumps({'run':raw,'public_run':run,'events':events},default=str,indent=2))
    print(json.dumps({'label':label,'status':run['status'],'elapsed':round(time.monotonic()-start,1),'issue':(run.get('plan')or{}).get('proposal_issue'),'diagnostics':[d.get('code')for d in run.get('diagnostics',[])],'answer_chars':len(run.get('graph_answer')or''),'steps':[(r.get('step_id'),r.get('status'),len(r.get('nodes',[]))) for r in (run.get('evidence')or (run.get('preview')or{}).get('evidence')or{}).get('steps',[])]}),flush=True)
    return run
   prior=None;sets=[];reports=[]
   cases=[('all',None),('narrow','Restrict to ND donors.'),('widen','Remove the ND restriction; count all HPAP donors.'),('replace','Count T1D donors in HPAP instead.')]
   for label,instruction in cases:
    if prior is None:
     response=await client.post('/v2/plans',json={'question':'How many HPAP donors are available?'})
    else:
     response=await client.post('/v2/plans/'+prior['plan_id']+'/revise',json={'question':prior['question'],'revision_mode':'instruction','revision_instruction':instruction})
    assert response.status_code==202
    created=response.json()
    for _ in range(90):
     current=(await client.get('/v2/runs/'+created['run_id'])).json()
     if current['status'] in {'awaiting_confirmation','failed'}:break
     await asyncio.sleep(1)
    raw=await app.state.runtime.io.call(app.state.runtime.store.get,created['run_id'])
    steps=(raw.get('preview') or {}).get('evidence',{}).get('steps',[])
    actual={n['id'] for step in steps for n in step.get('nodes',[]) if 'donor' in n.get('labels',[])}
    pattern='MATCH (d:donor)' if label!='replace' else 'MATCH (:disease {id:$disease})-[:HAS_DONOR]->(d:donor)'
    where='d.data_source=$source'+(' AND d.diabetes_type=$clinical' if label=='narrow' else '')
    expected={r['id'] for r in await graph._small_query(pattern+' WHERE '+where+' RETURN DISTINCT d.id AS id',{'source':'HPAP','clinical':'Control Without Diabetes','disease':'MONDO_0005147'})}
    report={'case':label,'status':current['status'],'expected':len(expected),'actual':len(actual),'membership_match':actual==expected and all(s.get('status') in {'complete','empty'} for s in steps),'standalone_question': 'Requested change:' not in raw['question']}
    if label=='widen':report['new_ids_absent_from_old']=len(actual-sets[-1])
    print(json.dumps(report),flush=True);reports.append(report);sets.append(actual)
    (ROOT/('revision-'+label+'.json')).write_text(json.dumps(raw,default=str,indent=2));prior=raw
    if current['status']!='awaiting_confirmation':break
   (ROOT/'revision-report.json').write_text(json.dumps(reports,indent=2))
   if prior and prior['status']=='awaiting_confirmation':await client.post('/v2/runs/'+prior['run_id']+'/cancel')
   print('budget',json.dumps(gateway.budget.snapshot()),flush=True)
asyncio.run(main())
