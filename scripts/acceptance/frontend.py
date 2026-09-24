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
from pankagent_vnext.config import Settings
from pankagent_vnext.graph import GraphAdapter
from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.app import normalize_plan
from pankagent_vnext.filter_recovery import recover_filter_failures
s=replace(Settings(),state_dir=Path(os.environ['PANK_ACCEPTANCE_BUDGET_STATE']),budget_usd=10)
async def emit(*args):pass
async def main():
 g=GraphAdapter(s);c=ClaudeGateway(s)
 try:
  print('budget_before',json.dumps(c.budget.snapshot()),flush=True)
  catalog=json.loads((REPO/'tests_vnext/fixtures/acceptance/frontend.json').read_text())
  requested=sys.argv[1:] or ['0','1','7']
  for idx in requested:
   case=catalog['cases'][int(idx)];q=case['question'];start=time.monotonic()
   original_create=c._create
   async def capture(*args, **kwargs):
    reply=await original_create(*args, **kwargs)
    with (ROOT/f'case-{idx}-model.jsonl').open('a') as f:f.write(json.dumps({'request':kwargs,'reply':reply.model_dump()},default=str)+'\n')
    return reply
   c._create=capture
   try:
    grounding=await g.ground_question(q)
    async def prep(p):
     p=await g.prepare_plan(normalize_plan(p),emit)
     return recover_filter_failures(p)
    p=await asyncio.wait_for(c.plan(q,[],grounding=grounding,resolver=g.resolve_entities,preparer=prep),120)
    (ROOT/f'case-{idx}-plan.json').write_text(json.dumps(p,default=str,indent=2))
    evidence={};repairs=0
    async def repair(*args):
     nonlocal repairs
     if repairs>=2 or p.get('planning_route',{}).get('claude_calls',0)+repairs>=7:return []
     repairs+=1
     return await c.repair_cypher(*args)
    g.query_repair=repair
    if not p.get('clarification'):
     for j,step in enumerate(p.get('steps',[])):
      if j==0:step['gpu_participation_required']=True
      if step.get('operation'):
       from pankagent_vnext.composable_planning import combine
       result=combine(step,evidence)
      else:result=await asyncio.wait_for(g.execute(step,evidence,emit),90)
      result['evidence_id']=step.get('evidence_id',f'G{j+1}');evidence[step['id']]=result
    answer=''
    if any(r.get('status') in {'complete','empty','partial'} for r in evidence.values()):
     answer=''.join([part async for part in c.synthesize(q,evidence)])
    (ROOT/f'case-{idx}.json').write_text(json.dumps({'question':q,'plan':p,'evidence':evidence,'answer':answer},default=str,indent=2))
    report={'case':idx,'question':q,'elapsed':round(time.monotonic()-start,1),'route':p.get('planning_route'),
       'clarification':p.get('clarification'),'issue':p.get('proposal_issue'),
       'execution':[(k,r.get('status'),len(r.get('nodes',[])),len(r.get('edges',[]))) for k,r in evidence.items()],
       'answer_chars':len(answer),'warnings':p.get('interpretation_warnings')}
   except Exception as e:report={'case':idx,'error':type(e).__name__,'detail':'See the protected run artifact.'}
   c._create=original_create
   print(json.dumps(report),flush=True)
   with (ROOT/'replay-report.jsonl').open('a') as f:f.write(json.dumps(report)+'\n')
  print('budget_after',json.dumps(c.budget.snapshot()),flush=True)
 finally:await g.close();await c.close()
asyncio.run(main())
