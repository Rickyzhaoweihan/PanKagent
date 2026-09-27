"""Summarize frozen original-workflow arms without equating retrieval with quality."""
import argparse, json, math, statistics
from pathlib import Path


def read(path, fallback=None):
    return json.loads(path.read_text()) if path.exists() else fallback


def rows(root):
    p=root/'report.jsonl'
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def quantile(values,p):
    values=sorted(values)
    return values[max(0,math.ceil(len(values)*p)-1)] if values else None


def stats(records):
    preview=[r['first_preview_s'] for r in records if r.get('first_preview_s') is not None]
    costs=[r.get('accounting',{}).get('settled_cost_usd',0) for r in records]
    stages={}
    for r in records:
        for name,value in r.get('accounting',{}).get('stages',{}).items():
            stages[name]=stages.get(name,0)+value.get('cost_usd',0)
    return {'attempts':len(records),'preview_ready':sum(bool(r.get('ready')) for r in records),
        'verified_core_covered':sum(bool(r.get('evaluation',{}).get('verified_core_covered')) for r in records),
        'preview_p50_s':statistics.median(preview) if preview else None,'preview_p95_s':quantile(preview,.95),
        'all_attempt_elapsed_p50_s':statistics.median([r['elapsed_s'] for r in records]) if records else None,
        'settled_cost_usd':sum(costs),'mean_attempt_cost_usd':statistics.mean(costs) if costs else None,
        'cost_by_stage_usd':stages,'work':{key:sum(r.get('work',{}).get(key,0) for r in records) for key in ['generate','explain','retrieve']}}


def verified_empty_result(key, raw, evidence):
    # Evaluation-only: only the reviewed empty references qualify. Intermediate
    # populations may be nonempty; it is the requested terminal population that
    # must be empty, with every prerequisite fully read.
    if key not in {'Q52', 'Q55'}:
        return False
    primary = [s for s in evidence.get('steps', []) if s.get('purpose') != 'context']
    plan_steps = (raw.get('plan') or {}).get('steps', [])
    parents = {parent for s in plan_steps for parent in s.get('depends_on', [])
               if isinstance(parent, str)}
    terminal = [s for s in primary if s.get('step_id') not in parents]
    return (bool(terminal) and all(s.get('status') == 'empty' for s in terminal)
            and all(s.get('status') in {'complete', 'empty'} and not s.get('truncated')
                    and (s.get('retrieval_execution') or {}).get('completed') is True
                    and (s.get('retrieval_execution') or {}).get('cursor_exhausted') is True
                    for s in primary))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    arms={'original':rows(a.root/'baseline'),'improved':rows(a.root/'improved')}
    lookup={arm:{r['case']:r for r in values} for arm,values in arms.items()}
    keys=sorted(set(lookup['original'])|set(lookup['improved']))
    cases=[]
    for key in keys:
        review=read(a.root/'review'/(key+'.json'),{})
        advisory={}
        for label,arm in review.get('label_mapping',{}).items():
            advisory[arm]=review.get('assessment',{}).get(label,{})
        item={'id':key,'arms':{},'advisory_review':advisory}
        for arm in arms:
            row=lookup[arm].get(key)
            if not row:continue
            raw=read(a.root/('baseline' if arm=='original' else 'improved')/(key+'.json'),{}).get('run',{})
            evidence=raw.get('evidence') or (raw.get('preview') or {}).get('evidence') or {}
            empty=verified_empty_result(key,raw,evidence)
            cov=row.get('evaluation',{});plan=raw.get('plan') or {}
            item['arms'][arm]={'status':row.get('status'),'core_covered':cov.get('verified_core_covered',False),
                'verified_empty':empty,'reference_unresolved':key in {'Q17','Q57'},
                'preview_s':row.get('first_preview_s'),'elapsed_s':row.get('elapsed_s'),
                'cost_usd':row.get('accounting',{}).get('settled_cost_usd'),
                'proposal_issue':plan.get('proposal_issue'),'clarification':plan.get('clarification'),
                'error':raw.get('error'),'missing_nodes':cov.get('missing_nodes'),
                'missing_edges':cov.get('missing_edges'),'property_failures':cov.get('property_failures'),
                'steps':cov.get('step_outcomes'), 'work':row.get('work')}
        cases.append(item)
    paired=[k for k in keys if all(lookup[x].get(k,{}).get('ready') for x in arms)]
    summary={'arms':{arm:stats(values) for arm,values in arms.items()},'matched_preview_cases':paired,
        'matched':{arm:stats([lookup[arm][k] for k in paired]) for arm in arms},'cases':cases,
        'limitations':['Baseline-first sequential order; warm inventory and reset query cache.',
            'Preview percentile covers ready previews; all-attempt timing/cost reported separately.',
            'Core coverage and advisory model opinion are not a human-supported-answer verdict.',
            'Literature disabled equally; final partial status alone does not mean graph failure.'],
        'ledgers':{'claude':read(a.root/'improved'/'summary.json',read(a.root/'baseline'/'summary.json',{})).get('budget'),
                   'openai':read(a.root/'review'/'summary.json',{}).get('budget')}}
    for arm in arms:
        summary['arms'][arm]['verified_empty']=sum(c['arms'].get(arm,{}).get('verified_empty',False) for c in cases)
        summary['arms'][arm]['advisory_usable']=sum(c['advisory_review'].get(arm,{}).get('usable') is True for c in cases)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k not in {'cases','matched_preview_cases'}},indent=2))
if __name__=='__main__':main()
