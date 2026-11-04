"""Evaluate held-out questions against canonical graph membership, not Cypher text.

Inputs and detailed results stay outside Git. No gold answer is passed to planning
or generation. This CLI spends from the same persistent development budget.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import time
from pathlib import Path
from .config import Settings
from .graph import GraphAdapter
from .llm import ClaudeGateway


def members(graph):
    nodes={str(x['id']) for x in graph.get('nodes',[]) if x.get('id') is not None}
    edges={json.dumps([x.get('start_id'),x.get('type'),x.get('end_id'),x.get('properties',{})],sort_keys=True,separators=(',',':'),default=str) for x in graph.get('edges',[])}
    return nodes,edges

def score(candidate,gold):
    cn,ce=members(candidate);gn,ge=members(gold)
    def calc(c,g):
        correct=len(c&g)
        precision=correct/len(c) if c else (1.0 if not g else 0.0)
        recall=correct/len(g) if g else (1.0 if not c else 0.0)
        return {'precision':precision,'recall':recall,'f1':2*precision*recall/(precision+recall) if precision+recall else 0.0}
    n=calc(cn,gn);e=calc(ce,ge)
    return {'node':n,'edge':e,'node_count':len(cn),'edge_count':len(ce),'gold_node_count':len(gn),'gold_edge_count':len(ge),
            'exact_match': cn==gn and ce==ge,
            'f1':(n['f1']+e['f1'])/2 if ge else n['f1'],
            'passed':n['f1']>=.9 and (e['f1']>=.9 if ge else not ce)}

async def run(args):
    os.umask(0o077)
    settings=Settings();gateway=ClaudeGateway(settings);graph=GraphAdapter(settings)
    health=await graph.probe()
    if health.get('state') not in ('healthy','degraded'):
        raise RuntimeError('graph_identity_gate_failed: '+json.dumps(health))
    questions=[x for x in json.loads(args.questions.read_text()) if x['split']=='test']
    if args.limit: questions=questions[:args.limit]
    gold={x['id']:x for x in json.loads(args.answers.read_text())}
    source_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}
    identity={'model':settings.model,'graph_version':settings.graph_version,
              'graph_manifest_sha256':hashlib.sha256(Path(settings.graph_identity_file).read_bytes()).hexdigest(),
              'questions_sha256':hashlib.sha256(args.questions.read_bytes()).hexdigest(),
              'answers_sha256':hashlib.sha256(args.answers.read_bytes()).hexdigest(),
              'source_sha256':source_hashes}
    metadata=args.output.with_suffix('.metadata.json')
    if args.output.exists() and (not metadata.exists() or json.loads(metadata.read_text()) != identity):
        await gateway.close();await graph.close()
        raise RuntimeError('evaluation_identity_changed: use a new output file')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    metadata.write_text(json.dumps(identity,indent=2)+'\n')
    done={}
    if args.output.exists():
        done={x['id']:x for x in (json.loads(line) for line in args.output.read_text().splitlines())}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    sem=asyncio.Semaphore(args.concurrency)
    async def one(q):
        if q['id'] in done:return
        async with sem:
            start=time.monotonic();row={'id':q['id'],'type':q['type'],
                'gold_limited':bool(re.search(r'\bLIMIT\b',str(gold[q['id']].get('cypher','')),re.I))}
            try:
                plan=await asyncio.wait_for(gateway.plan(q['question'],[]),25)
                row['plan_s']=round(time.monotonic()-start,3)
                previous={}
                async def emit(*a):pass
                for step in plan.get('steps',[]):
                    previous[step['id']]=await graph.execute(step,previous,emit)
                row['graph_s']=round(time.monotonic()-start-row['plan_s'],3)
                result={'nodes':[],'edges':[]}
                for e in previous.values():
                    result['nodes'].extend(e.get('nodes',[]));result['edges'].extend(e.get('edges',[]))
                row.update(score(result,gold[q['id']]['result']))
                if q['type'].startswith(('composite_hpap:cohort_and_material','composite_pathway:expression_qtl_variants')):
                    row['passed']=row['passed'] and row['node']['recall']==1 and row['edge']['recall']==1
                row['step_statuses']=[e.get('status') for e in previous.values()]
                row['validation']=[e.get('validation') for e in previous.values()]
                row['plan']=plan
                row['clarification_required']=bool(plan.get('clarification'))
                if not previous: row['passed']=False
                row['queries']=[e.get('queries',[]) for e in previous.values()]
            except Exception as exc:
                row.update({'passed':False,'error':type(exc).__name__,'f1':0.0})
            row['wall_s']=round(time.monotonic()-start,3)
            with args.output.open('a') as out:out.write(json.dumps(row,default=str)+'\n')
            done[q['id']]=row
            print(json.dumps({k:row[k] for k in ['id','passed','f1','wall_s','error'] if k in row}),flush=True)
    try:await asyncio.gather(*(one(q) for q in questions))
    finally:await gateway.close();await graph.close()
    rows=[done[q['id']] for q in questions]
    def latency(key):
        values=sorted(x[key] for x in rows if key in x)
        return {'n':len(values),'median_s':statistics.median(values) if values else None,
                'p95_s':values[max(0,math.ceil(len(values)*.95)-1)] if values else None}
    summary={'questions':len(rows),'passed':sum(x['passed'] for x in rows),'exact_matches':sum(x.get('exact_match',False) for x in rows),
             'gold_limited_questions':sum(x.get('gold_limited',False) for x in rows),
             'mean_f1':statistics.mean(x['f1'] for x in rows),
             'median_wall_s':statistics.median(x['wall_s'] for x in rows),'p95_wall_s':sorted(x['wall_s'] for x in rows)[max(0,math.ceil(len(rows)*.95)-1)],
             'planning_latency':latency('plan_s'),'graph_retrieval_latency':latency('graph_s'),
             'model':settings.model,'synthesis_included':False,'concurrency':args.concurrency,
             'claude_budget':gateway.budget.snapshot(),'graph_version':settings.graph_version,
             'source_sha256':source_hashes}
    args.output.with_suffix('.summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--questions',type=Path,required=True);p.add_argument('--answers',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--limit',type=int,default=0)
    p.add_argument('--concurrency',type=int,choices=[1,2],default=2)
    asyncio.run(run(p.parse_args()))
