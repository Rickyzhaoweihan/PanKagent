import os,sys,json,asyncio,time,hashlib
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
  cases=json.loads((REPO/'tests_vnext/fixtures/acceptance/hpap.json').read_text())['cases']
  stages=await g._small_query('MATCH (d:donor) WHERE d.data_source=$source RETURN DISTINCT d.t1d_stage AS stage',{'source':'HPAP'})
  for idx in map(int,sys.argv[1:] or range(9)):
   case=cases[idx];q=case['question'];start=time.monotonic()
   try:
    params={'source':'HPAP'};where=['d.data_source = $source'];pattern='MATCH (d:donor)'
    if case.get('cohort')=='ND':params['clinical']='Control Without Diabetes';where.append('d.diabetes_type = $clinical')
    if case.get('cohort')=='T1D':params['disease']='MONDO_0005147';pattern='MATCH (x:disease)-[:HAS_DONOR]->(d:donor)';where.append('x.id = $disease')
    if case.get('stage'):
     values=[r['stage'] for r in stages if (r.get('stage') or '').startswith('Stage '+case['stage']+':')]
     assert len(values)==1;params['stage']=values[0];where.append('d.t1d_stage = $stage')
    donor_query=pattern+' WHERE '+' AND '.join(where)+' RETURN DISTINCT d.id AS id'
    donors={r['id'] for r in await g._small_query(donor_query,params)}
    samples=None
    if case.get('count_unit')=='sample':
     spattern=pattern+' MATCH (d)-[:HAS_SAMPLE]->(s:Sample_node)';sw=list(where)
     if 'scRNA-seq' in q:params['assays']=['scRNA-seq','snMultiomics'] if 'snMultiomics' in q else ['scRNA-seq'];sw.append('s.data_modality IN $assays')
     if 'spleen' in q:spattern+=' MATCH (t:anatomical_structure)-[:HAS_SAMPLE]->(s)';params['tissue']='UBERON_0002106';sw.append('t.id=$tissue')
     sample_query=spattern+' WHERE '+' AND '.join(sw)+' RETURN DISTINCT s.id AS id'
     samples={r['id'] for r in await g._small_query(sample_query,params)}
    ground=await g.ground_question(q)
    async def prep(p):return recover_filter_failures(await g.prepare_plan(normalize_plan(p),emit))
    plan=await asyncio.wait_for(c.plan(q,[],grounding=ground,resolver=g.resolve_entities,preparer=prep),120)
    results={}; repairs=0
    async def repair(*args):
     nonlocal repairs
     if repairs>=2 or plan.get('planning_route',{}).get('claude_calls',0)+repairs>=7:return []
     repairs+=1
     return await c.repair_cypher(*args)
    g.query_repair=repair
    if not plan.get('clarification'):
     for j,step in enumerate(plan.get('steps',[])):
      step['gpu_participation_required']=j==0
      if step.get('operation'):
       from pankagent_vnext.composable_planning import combine
       result=combine(step,results)
      else:result=await asyncio.wait_for(g.execute(step,results,emit),90)
      results[step['id']]=result
    selected=plan.get('answer_step_ids') or list(results)
    nodes=[n for k,r in results.items() if k in selected for n in r.get('nodes',[])]
    ids=lambda kind:{n.get('id') or n.get('properties',{}).get('id') for n in nodes if kind in n.get('labels',[])}
    actual=ids('donor' if samples is None else 'Sample_node');expected=donors if samples is None else samples
    match=actual==expected and bool(results) and all(r.get('status') in {'complete','empty'} for r in results.values())
    answer=''
    if match:answer=''.join([part async for part in c.synthesize(q,results)])
    record={'question':q,'plan':plan,'evidence':results,'answer':answer,'reference':{'donor_query':donor_query,'sample_query':locals().get('sample_query'),'parameters':params,'donor_ids':sorted(donors),'sample_ids':sorted(samples) if samples is not None else None}}
    (ROOT/f'hpap-{idx}.json').write_text(json.dumps(record,default=str,indent=2))
    report={'case':case['id'],'match':match,'expected_count':len(expected),'actual_count':len(actual),'missing':len(expected-actual),'extra':len(actual-expected),'issue':plan.get('proposal_issue'),'clarification':plan.get('clarification'),'execution':[(k,r.get('status')) for k,r in results.items()],'answer_chars':len(answer),'elapsed':round(time.monotonic()-start,1)}
   except Exception as e:report={'case':case['id'],'error':type(e).__name__,'detail':'See the protected run artifact.'}
   print(json.dumps(report),flush=True)
   with (ROOT/'hpap-report.jsonl').open('a') as f:f.write(json.dumps(report)+'\n')
  print('budget',json.dumps(c.budget.snapshot()),flush=True)
 finally:await g.close();await c.close()
asyncio.run(main())
