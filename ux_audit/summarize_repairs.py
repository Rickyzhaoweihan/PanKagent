"""Allowlisted repair outcomes; raw questions, answers and histories stay private."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics


def stage_times(events):
    def first(predicate):
        return next((e['elapsed_ms'] for e in events if predicate(e)), None)
    confirmed = first(lambda e: e.get('type') == 'progress' and e.get('status') == 'queued')
    def since_confirm(value):
        return round((value-confirmed)/1000, 3) if value is not None and confirmed is not None else None
    chunks=[e['elapsed_ms'] for e in events if e.get('type')=='graph_answer']
    return {'first_server_event_s': (events[0]['elapsed_ms']/1000) if events else None,
        'plan_ready_s': (lambda x: x/1000 if x is not None else None)(first(lambda e:e.get('type')=='plan_ready')),
        'graph_first_chunk_after_confirmation_s':since_confirm(chunks[0] if chunks else None),
        'graph_last_chunk_after_confirmation_s':since_confirm(chunks[-1] if chunks else None),
        'literature_after_confirmation_s':since_confirm(first(lambda e:e.get('type')=='literature_complete')),
        'preview_reused_events':sum(e.get('type')=='preview_reused' for e in events),
        'graph_query_starts_after_confirmation':sum(e.get('type')=='progress' and e.get('stage')=='querying_graph' and confirmed is not None and e['elapsed_ms']>=confirmed for e in events)}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--state',type=Path,required=True);args=parser.parse_args()
    bundle={'version':1,'measurement':'server API events, not browser or human timing','acceptance':'not accepted','batches':[]}
    for folder in ('cases','schema-cases'):
        rows=[]
        for path in sorted((args.root/folder).glob('*-vnext.json')):
            c=json.loads(path.read_text());r=c.get('final') or (c.get('revisions') or [{}])[-1].get('run') or c.get('initial') or {}
            evidence=r.get('evidence') or (r.get('preview') or {}).get('evidence') or (c.get('result') or {}).get('evidence') or {}
            events=[]
            if r.get('run_id'):
                with sqlite3.connect(f'file:{args.state/"sessions.sqlite3"}?mode=ro',uri=True) as db:
                    events=[json.loads(x[0]) for x in db.execute('SELECT envelope FROM events WHERE run_id=? ORDER BY sequence',(r['run_id'],))]
            rows.append({'id':c['task_id'],'capture_status':c['status'],'run_status':r.get('status'),'error_category':(r.get('error') or {}).get('category'),
                'preview_status':(r.get('preview') or {}).get('status'),'nodes':len(evidence.get('nodes',[])),'edges':len(evidence.get('edges',[])),
                'revision_versions_recorded':len(c.get('revisions',[])), 'revision_scientific_review':'pending' if c.get('revisions') else 'not_applicable',
                'literature_status':(r.get('literature') or {}).get('status'), 'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'manifest_sha256':c.get('manifest_sha256'),'timing':stage_times(events),
                'failed_step_categories':sorted({str(reason).split(':')[0] for s in evidence.get('steps',[]) if s.get('status')=='failed' for v in s.get('validation',[]) for reason in v.get('reasons',[])})})
        medians={key:statistics.median(vals) if (vals:=[r['timing'][key] for r in rows if r['run_status']=='completed' and r['timing'][key] is not None]) else None for key in ('plan_ready_s','graph_last_chunk_after_confirmation_s','literature_after_confirmation_s')}
        bundle['batches'].append({'name':folder,'tasks':rows,'completed_run_only_medians_s':medians,'counts':dict(Counter(r['run_status'] or r['capture_status'] for r in rows))})
    with sqlite3.connect(f'file:{args.state/"budget.sqlite3"}?mode=ro',uri=True) as db:
        spent,reserved,calls=db.execute('SELECT COALESCE(SUM(actual),0),COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0),COUNT(*) FROM usage').fetchone()
    bundle['cumulative_budget']={'cap_usd':10,'spent_usd':spent,'reserved_usd':reserved,'remaining_usd':10-spent-reserved,'calls':calls,'hirn_cost':'unattributed'}
    print(json.dumps(bundle,indent=2))

if __name__=='__main__':main()
