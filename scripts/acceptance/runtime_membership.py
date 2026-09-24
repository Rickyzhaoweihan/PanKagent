import os,sys,json,asyncio
from pathlib import Path
ROOT=Path(os.environ['PANK_ACCEPTANCE_ROOT'])
ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from deploy_results.manage import read_protected_env
os.environ.update(read_protected_env(Path(os.environ['PANK_ACCEPTANCE_ENV'])))
from pankagent_vnext.graph import GraphAdapter
from pankagent_vnext.config import Settings
async def main():
 g=GraphAdapter(Settings());reports=[]
 try:
  for label in ['followup','nd','comparison']:
   run=json.loads((ROOT/f'runtime-{label}.json').read_text())['run']
   steps=(run.get('evidence') or {}).get('steps',[])
   ids=lambda s,t:{n['id'] for n in s.get('nodes',[]) if t in n.get('labels',[])}
   expected=[]
   if label=='followup':
    parent=json.loads((ROOT/'runtime-parent.json').read_text())['run']
    donors=set().union(*(ids(s,'donor') for s in parent['evidence']['steps']))
    samples={r['id'] for r in await g._small_query('UNWIND $ids AS id MATCH (d:donor {id:id})-[:HAS_SAMPLE]->(s:Sample_node) WHERE s.data_modality=$assay RETURN DISTINCT s.id AS id',{'ids':sorted(donors),'assay':'scRNA-seq'})}
    expected=[('donor',donors),('Sample_node',samples)]
   else:
    cohorts=['ND','T1D'] if label=='comparison' else ['ND']
    for cohort in cohorts:
     pattern='MATCH (d:donor)' if cohort=='ND' else 'MATCH (:disease {id:$disease})-[:HAS_DONOR]->(d:donor)'
     predicate='d.data_source=$source'+(' AND d.diabetes_type=$clinical' if cohort=='ND' else '')
     args={'source':'HPAP','clinical':'Control Without Diabetes','disease':'MONDO_0005147'}
     donors={r['id'] for r in await g._small_query(pattern+' WHERE '+predicate+' RETURN DISTINCT d.id AS id',args)}
     samples={r['id'] for r in await g._small_query(pattern+' MATCH (d)-[:HAS_SAMPLE]->(s:Sample_node) WHERE '+predicate+' RETURN DISTINCT s.id AS id',args)}
     expected.extend([('donor',donors),('Sample_node',samples)])
   checks=[{'type':t,'expected':len(wanted),'matched_complete_step':any(ids(s,t)==wanted and s.get('status') in {'complete','empty'} for s in steps)} for t,wanted in expected]
   reports.append({'label':label,'checks':checks,'pass':all(c['matched_complete_step'] for c in checks)})
  (ROOT/'runtime-membership-report.json').write_text(json.dumps(reports,indent=2));print(json.dumps(reports))
 finally:await g.close()
asyncio.run(main())
