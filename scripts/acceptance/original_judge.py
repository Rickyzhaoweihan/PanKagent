"""Advisory blinded review for separately frozen original-workflow arms."""
import argparse, asyncio, json, os, time, fcntl
from pathlib import Path
from dataclasses import replace
from workflow57_judge import SYSTEM, digest, read_protected_env, Settings, ClaudeGateway, BudgetExceeded


async def run(args):
    os.environ.update(read_protected_env(args.env))
    if not (args.ledger/'budget.sqlite3').is_file():
        raise ValueError('An explicitly authorized existing OpenAI ledger is required')
    settings=replace(Settings(),model='gpt-6-sol',reasoning_effort='low',budget_usd=args.ceiling,
        budget_dir=str(args.ledger),state_dir=args.output,provider_status_url='')
    gateway=ClaudeGateway(settings)
    cases=json.loads(args.fixture.read_text())['cases']
    deadline=time.monotonic()+10800
    try:
        while time.monotonic()<deadline:
            progressed=False
            for index,case in enumerate(cases):
                key=case['id'];dest=args.output/(key+'.json')
                paths=[root/(key+'.json') for root in (args.baseline,args.improved)]
                if dest.exists() or not all(p.exists() for p in paths):continue
                artifacts=[json.loads(p.read_text()) for p in paths]
                mapping=['original','improved'] if index%2==0 else ['improved','original']
                byarm=dict(zip(['original','improved'],artifacts))
                payload={'question':case['question'],'reference_core':case['core'],
                    'reference_scope':'Minimum required nodes and edges, with optional relevant exploration allowed. Unknown targets are not verified empty targets.',
                    'optional_extra_counts':{k:len(v) for k,v in case['extra'].items()}}
                for label,arm in zip(['A','B'],mapping):
                    raw=byarm[arm]['run'];payload[label]={'answer':raw.get('graph_answer') or '',
                        'status':raw.get('status'),'error':raw.get('error'),
                        'clarification':(raw.get('plan') or {}).get('clarification'),'evidence':digest(raw)}
                body=json.dumps(payload,ensure_ascii=False,default=str)
                if len(body.encode())>180000:
                    for label in ['A','B']:
                        for step in payload[label]['evidence']:
                            step['node_excerpt']=[];step['edge_excerpt']=step['edge_excerpt'][:6]
                            step['row_excerpt']=[];step['excerpt_complete']=False
                    body=json.dumps(payload,ensure_ascii=False,default=str)
                if len(body.encode())>240000:
                    dest.write_text(json.dumps({'case':key,'status':'skipped_payload_limit'}));continue
                # A reservation is persisted before the call. Never blindly retry an interrupted paid review.
                marker=args.output/(key+'.pending')
                if marker.exists():continue
                try:rid=await gateway._reserve('blinded_original_quality_review',SYSTEM,body,2200)
                except BudgetExceeded:
                    (args.output/'summary.json').write_text(json.dumps({'status':'budget_exhausted','budget':await gateway.budget.asnapshot()}));return
                marker.write_text(json.dumps({'reservation':rid,'case':key}))
                try:
                    reply=await gateway._create(rid,model=settings.model,max_tokens=2200,system=SYSTEM,messages=[{'role':'user','content':body}])
                    usage=reply.usage.model_dump();await gateway.budget.asettle(rid,usage)
                    text=''.join(c.text for c in reply.content if c.type=='text').strip()
                    if text.startswith('```'):text=text.split('\n',1)[1].rsplit('```',1)[0]
                    try:assessment=json.loads(text)
                    except ValueError:assessment={'unparsed':text}
                    result={'case':key,'status':'reviewed','label_mapping':dict(zip(['A','B'],mapping)),
                        'assessment':assessment,'usage':usage,'advisory_only':True}
                except Exception as exc:
                    result={'case':key,'status':'review_failed','error':type(exc).__name__}
                dest.write_text(json.dumps(result,indent=2)+'\n');marker.unlink();progressed=True
                print(json.dumps({'case':key,'status':result['status']}),flush=True)
            summaries=[json.loads((r/'summary.json').read_text()) if (r/'summary.json').exists() else {} for r in (args.baseline,args.improved)]
            ended=all(s.get('complete') or s.get('incomplete') for s in summaries)
            (args.output/'summary.json').write_text(json.dumps({'status':'complete' if ended else 'running',
                'reviewed':len(list(args.output.glob('Q*.json'))),'budget':await gateway.budget.asnapshot()},indent=2))
            if ended:return
            if not progressed:await asyncio.sleep(10)
    finally:await gateway.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ['baseline','improved','output','ledger','env','fixture']:
        parser.add_argument('--'+key,type=Path,required=True)
    parser.add_argument('--ceiling',type=float,required=True);args=parser.parse_args()
    os.umask(0o077);args.output.mkdir(parents=True,exist_ok=True)
    with (args.output/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);asyncio.run(run(args))
if __name__=='__main__':main()
