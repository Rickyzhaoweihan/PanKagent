"""Compare matched successful controls, never reward a failed baseline's low cost."""
import argparse
import json
from pathlib import Path
from statistics import median, mean

def success(r):
    return (r.get('membership_match') is True and r.get('retrieval_complete') is True
            and r.get('stream_match') is True and r.get('answer_chars',0)>0)

def compare(baseline,candidate):
    a={r['case']:r for r in baseline};b={r['case']:r for r in candidate}
    lost=sorted(k for k,r in a.items() if success(r) and (k not in b or not success(b[k])))
    matched=[k for k in a.keys() & b.keys() if success(a[k]) and success(b[k])]
    groups={k.rsplit('-',1)[0] for k in matched};metrics=[]
    for group in sorted(groups):
        keys=[k for k in matched if k.rsplit('-',1)[0]==group]
        for field in ['preview_s','elapsed_s']:
            before=median(a[k][field] for k in keys);after=median(b[k][field] for k in keys)
            metrics.append({'control':group,'metric':field,'matched_runs':len(keys),'baseline':before,
                            'candidate':after,'allowance':max(.2*before,2),'pass':after-before<=max(.2*before,2)})
    cost_known=bool(matched) and all(a[k].get('cost_settled') and b[k].get('cost_settled') for k in matched)
    cost=None
    if cost_known:
        before=mean(a[k]['settled_cost_usd'] for k in matched);after=mean(b[k]['settled_cost_usd'] for k in matched)
        cost={'baseline_mean':before,'candidate_mean':after,'pass':after<=1.2*before}
    return {'pass':bool(matched) and not lost and all(m['pass'] for m in metrics) and bool(cost and cost['pass']),
            'lost_successes':lost,'latency':metrics,'cost':cost,
            'new_successes':sorted(k for k in b if success(b[k]) and (k not in a or not success(a[k]))),
            'matched_successful_runs':len(matched),'baseline_failures_excluded_from_cost':sorted(k for k in a if not success(a[k]))}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('baseline');p.add_argument('candidate');p.add_argument('output');a=p.parse_args()
    result=compare(json.loads(Path(a.baseline).read_text())['reports'],json.loads(Path(a.candidate).read_text())['reports'])
    Path(a.output).write_text(json.dumps(result,indent=2)+'\n')
    raise SystemExit(0 if result['pass'] else 1)
