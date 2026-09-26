"""Paired live comparison of the default and competing-candidate workflows.

No deployment: two isolated ASGI runtimes share the same existing budget ledger.
References are frozen before model calls. Raw evidence stays outside the repo.
"""
import argparse
import asyncio
from contextlib import AsyncExitStack
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n')


def plan_request(case, parent=None):
    if case.get('parent') and not parent:
        raise ValueError('A contextual case requires its recorded parent run')
    return {'question':case['question'], 'session_id':parent['session_id'] if parent else None,
            'include_context':bool(parent), 'event_source':'audit_replay'}


def extract(selector, results):
    nodes = [n for result in results.values() for n in result.get('nodes', [])]
    typed = {str(n['id']) for n in nodes if selector.get('entity_type') in n.get('labels', [])}
    if selector['mode'] == 'nodes':
        ids = typed
    else:
        edges = [e for result in results.values() for e in result.get('edges', [])
                 if e.get('type') == selector['relation']]
        if selector.get('anchor_id'):
            edges = [e for e in edges if e.get(selector['anchor_endpoint']) == selector['anchor_id']]
        if selector['mode'] == 'coloc':
            ids = {str(e.get('properties', {}).get('gwas_signal_id') or '') + '|' +
                   str(e.get('properties', {}).get('qtl_signal_id') or '') for e in edges}
        else:
            ids = {str(e[selector['endpoint']]) for e in edges} & typed
    if 'only_ids' in selector:
        ids &= set(selector['only_ids'])
    return ids - set(selector.get('omit', []))


def evaluate(case, frozen, run):
    from pankagent_vnext.composable_planning import answer_results
    evidence = run.get('evidence') or (run.get('preview') or {}).get('evidence') or {}
    previous = {s['step_id']:s for s in evidence.get('steps', [])}
    selected = answer_results(run.get('plan') or {}, previous)
    checks = []
    for spec, reference in zip(case['checks'], frozen['checks']):
        actual = extract(spec['selector'], selected)
        expected = set(reference.get('ids', []))
        relation = spec.get('requires_relation')
        # No evidence is not a verified negative. Require an executed complete
        # check for the requested relation before accepting an empty reference.
        relevant = [previous.get(s['id'], {}) for s in (run.get('plan') or {}).get('steps', [])
                    if not relation or relation in s.get('relation_types', [])]
        complete = bool(relevant) and all(r.get('status') in {'complete','empty'}
                     and r.get('truncated') is False and not r.get('error') for r in relevant)
        overlap = len(actual & expected)
        checks.append({'name':spec['name'], 'comparison':spec['comparison'],
            'reference_ok':reference['ok'], 'reference_count':len(expected), 'actual_count':len(actual),
            'missing_count':len(expected-actual), 'extra_count':len(actual-expected),
            'precision':overlap/len(actual) if actual else (1.0 if not expected else 0.0),
            'recall':overlap/len(expected) if expected else (1.0 if not actual else 0.0),
            'membership_equal':bool(reference['ok'] and actual == expected), 'complete':complete,
            'pass':bool(reference['ok'] and actual == expected and complete)})
    exact = [c for c in checks if c['comparison'] == 'exact']
    needs_clarification = case.get('expected_clarification',False)
    return {'checks':checks, 'exact_reference_pass':all(c['pass'] for c in exact) if exact and not needs_clarification else None,
            'expected_clarification_pass':bool((run.get('plan') or {}).get('clarification')
                and (run.get('error') or {}).get('category') != 'planning_failure') if needs_clarification else None,
            'manual_review_required':True, 'answer_chars':len(run.get('graph_answer') or '')}


def model_accounting(audit):
    reserved = {e['payload']['reservation_id']:e for e in audit['events'] if e['kind']=='model_reserved'}
    settled = {e['payload']['reservation_id']:e for e in audit['events'] if e['kind']=='model_settled'}
    stages = {}
    for key, event in settled.items():
        if key not in reserved:
            continue
        reservation = reserved[key]
        stage = stages.setdefault(reservation['payload']['purpose'], {'calls':0,'seconds':0,'cost_usd':0,'usage':{}})
        stage['calls'] += 1
        stage['seconds'] += (datetime.fromisoformat(event['received_at']) - datetime.fromisoformat(reservation['received_at'])).total_seconds()
        stage['cost_usd'] += event['payload']['actual_usd']
        for k,v in event['payload']['usage'].items():
            if type(v) is int:stage['usage'][k] = stage['usage'].get(k,0) + v
    pending = sum(e['payload']['reserved_usd'] for k,e in reserved.items() if k not in settled)
    actual = sum(e['payload']['actual_usd'] for e in settled.values())
    return {'stages':stages, 'settled_cost_usd':actual, 'pending_bound_usd':pending,
            'cost_settled':set(reserved)==set(settled), 'model_calls':len(reserved)}


def summarize(rows):
    summary = {}
    for arm in ('original','candidate'):
        items = [r for r in rows if r['arm']==arm]
        exact = [r for r in items if r.get('evaluation',{}).get('exact_reference_pass') is not None]
        metrics = {}
        for name in ('first_preview_s','settled_preview_s','elapsed_s'):
            values = [r[name] for r in items if r.get(name) is not None and
                      (r.get('ready') if name=='first_preview_s' else
                       r.get('settled_eligible') if name=='settled_preview_s' else
                       r.get('evaluation',{}).get('answer_chars',0)>0)]
            metrics[name] = {'n':len(values),'median':statistics.median(values) if values else None}
        stages = {}
        for row in items:
            for purpose, usage in row.get('accounting',{}).get('stages',{}).items():
                aggregate = stages.setdefault(purpose, {'calls':0,'seconds':0,'cost_usd':0})
                for field in aggregate:
                    aggregate[field] += usage.get(field,0)
        clarifications = [r['evaluation']['expected_clarification_pass'] for r in items
                          if r.get('evaluation',{}).get('expected_clarification_pass') is not None]
        summary[arm] = {'attempted':len(items),'ready':sum(r.get('ready',False) for r in items),
            'settled_eligible':sum(r.get('settled_eligible',False) for r in items),
            'answers':sum(r.get('evaluation',{}).get('answer_chars',0)>0 for r in items),
            'exact_reference_passes':sum(r['evaluation']['exact_reference_pass'] for r in exact),
            'exact_reference_cases':len(exact), 'latency_successful_only':metrics,
            'expected_clarifications':{'passed':sum(clarifications),'cases':len(clarifications)},
            'model_stages':stages,
            'cost_usd_all_attempts':sum(r.get('accounting',{}).get('settled_cost_usd',0) for r in items),
            'pending_bound_usd':sum(r.get('accounting',{}).get('pending_bound_usd',0) for r in items),
            'database_reads':sum(r.get('work',{}).get('retrieve',0) for r in items),
            'gpu_requests':sum(r.get('work',{}).get('generate',0) for r in items)}
    pairs = {r['case']:{x['arm']:x for x in rows if x['case']==r['case']} for r in rows}
    summary['matched_successes'] = {}
    for name in ('first_preview_s','settled_preview_s','elapsed_s'):
        matched = [p for p in pairs.values() if set(p)=={'original','candidate'}
                   and all(r.get('settled_eligible')
                           and r.get(name) is not None for r in p.values())]
        summary['matched_successes'][name] = {'n':len(matched), **{arm:statistics.median(p[arm][name] for p in matched)
            if matched else None for arm in ('original','candidate')}}
    return summary


async def run(args):
    raise RuntimeError('The competing workflow was removed. Historical paired runs '
                       'require checkout d274133; offline scoring remains available.')


if __name__ == '__main__':
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--env',type=Path,required=True)
    parser.add_argument('--ledger',type=Path,required=True)
    parser.add_argument('--ceiling',type=float,required=True)
    parser.add_argument('--manifest',type=Path,default=REPO/'tests_vnext/fixtures/acceptance/workflow50.json')
    parser.add_argument('--references-only',action='store_true')
    parser.add_argument('--limit',type=int,default=50)
    parser.add_argument('--resume',action='store_true',help='Resume a stopped comparison, preserving completed attempts and cumulative accounting')
    parser.add_argument('--case-keys',nargs='+',help='Explicit bounded subset; include parent cases in a fresh run')
    args = parser.parse_args()
    os.umask(0o077)
    args.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (args.root/'.benchmark.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(run(args))
