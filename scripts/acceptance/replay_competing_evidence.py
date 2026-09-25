"""Offline saved-evidence replay; does not call models or claim generation quality."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pankagent_vnext.competing_candidates import Selection
from pankagent_vnext.composable_planning import answer_results, combine


def identities(case, plan, results):
    selected = answer_results(plan, results).values()
    if case['kind'] == 'coloc':
        return {str(e.get('properties', {}).get('gwas_signal_id', '')) + '|' +
                str(e.get('properties', {}).get('qtl_signal_id', ''))
                for r in selected for e in r.get('edges', []) if e['type'] == 'SIGNAL_COLOC_WITH'}
    if case['key'].startswith(('cftr-paraphrase', 'cftr-alias')):
        return {str(e['end_id']) for r in selected for e in r.get('edges', [])
                if e['type'] == 'GENE_ENRICHED_IN'}
    return {str(n['id']) for r in selected for n in r.get('nodes', [])
            if case['kind'] in n.get('labels', [])} - set(case.get('omit', []))


def replay(path):
    data = json.loads(path.read_text())
    if not all(k in data for k in ('case', 'reference', 'run')):
        return None
    case, run = data['case'], data['run']
    plan = run.get('plan') or {'steps': []}
    evidence = run.get('evidence') or (run.get('preview') or {}).get('evidence') or {}
    old = {s['step_id']:s for s in evidence.get('steps', [])}
    results, accepted, rejected = {}, [], []
    for step in plan.get('steps', []):
        parents = {k:results[k] for k in step.get('depends_on', []) if k in results}
        saved = old.get(step['id'])
        if saved is None:
            continue
        if step.get('operation'):
            try:
                results[step['id']] = combine(step, parents)
            except ValueError:
                results[step['id']] = deepcopy(saved)
                rejected.append(step['id'])
            continue
        selector = Selection(step, parents)
        selector.offer(saved, 'saved-evidence')
        selector.offer(deepcopy(saved), 'equivalent-duplicate')
        if selector.current() is not None:
            results[step['id']] = selector.current()
            accepted.append(step['id'])
            assert selector.revision == 1 and not selector.conflict
        else:
            # Preserve explicit failures, never promote an unusable saved result.
            results[step['id']] = {**deepcopy(saved), 'status':'failed',
                'error':{'category':'offline_candidate_rejected'}}
            rejected.append(step['id'])
    before, after = identities(case, plan, old), identities(case, plan, results)
    expected = set(map(str, data['reference'].get('ids', [])))
    return {'source':str(path), 'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'case':case['key'], 'accepted_steps':accepted, 'rejected_steps':rejected,
            'membership_unchanged':before == after, 'exact_reference_match':after == expected,
            'reference_count':len(expected), 'actual_count':len(after),
            'previous_recorded_match':data.get('report', {}).get('membership_match')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directories', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    rows = [r for directory in args.directories for path in sorted(directory.glob('*.json'))
            if (r := replay(path)) is not None]
    report = {'mode':'offline recorded evidence; equivalent candidates only',
              'new_api_cost_usd':0, 'live_latency_measured':False,
              'cases':len(rows), 'unchanged_membership':sum(r['membership_unchanged'] for r in rows),
              'exact_reference_matches':sum(r['exact_reference_match'] for r in rows), 'results':rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k:v for k,v in report.items() if k != 'results'}))
