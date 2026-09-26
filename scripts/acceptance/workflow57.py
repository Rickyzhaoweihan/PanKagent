"""Core/extra review-suite comparison; reuses workflow50's isolated two-arm runner.

No deployments or backend changes. Every paid run requires an existing authorized
ledger and explicit ceiling. Core coverage is not a claim of scientific correctness.
"""
import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).parent))
import workflow50 as runner


def edge_key(edge):
    return tuple(str(edge.get(k, '')) for k in ('type', 'start_id', 'end_id'))


def coverage(reference, results):
    nodes = {str(n['id']) for result in results.values() for n in result.get('nodes', [])}
    edges = [e for result in results.values() for e in result.get('edges', [])]
    keys = {edge_key(e) for e in edges}
    expected_nodes = {n['id'] for n in reference['core']['nodes']}
    expected_edges = {edge_key(e) for e in reference['core']['edges']}
    property_failures = []
    for expected in reference['core']['edges']:
        # Only explicitly requested quantitative/identity properties constrain
        # the score. Provenance is retained without requiring every source field.
        props = expected.get('properties', {})
        important = {k:v for k,v in props.items() if k in {
            'p_value','pip','effect_allele','non_effect_allele','credible_set_id',
            'credible_set','gwas_signal_id','qtl_signal_id','coloc_dataset'}
            or k.endswith('_ocr_gene_activity_score_mean')
            or k.endswith('_ocr_gene_activity_score_median')}
        candidates = [e.get('properties', {}) for e in edges if edge_key(e)==edge_key(expected)]
        if important and not any(all(actual.get(k)==v for k,v in important.items()) for actual in candidates):
            property_failures.append({'edge':list(edge_key(expected)), 'required':important})
    extra_nodes = {n['id'] for n in reference['extra']['nodes']}
    extra_edges = {edge_key(e) for e in reference['extra']['edges']}
    return {'required_nodes':len(expected_nodes),'required_edges':len(expected_edges),
        'missing_nodes':sorted(expected_nodes-nodes),
        'missing_edges':[list(e) for e in sorted(expected_edges-keys)],
        'property_failures':property_failures,
        'core_covered':bool(expected_nodes or expected_edges) and expected_nodes<=nodes and expected_edges<=keys and not property_failures,
        'extra_nodes_retrieved':len(extra_nodes & nodes),'extra_nodes_available':len(extra_nodes),
        'extra_edges_retrieved':len(extra_edges & keys),'extra_edges_available':len(extra_edges),
        'unlisted_nodes':len(nodes-expected_nodes-extra_nodes),
        'unlisted_edges':len(keys-expected_edges-extra_edges)}


def evaluate(reference, run):
    from pankagent_vnext.composable_planning import answer_results
    evidence = run.get('evidence') or (run.get('preview') or {}).get('evidence') or {}
    previous = {s['step_id']:s for s in evidence.get('steps', [])}
    selected = answer_results(run.get('plan') or {}, previous)
    result = coverage(reference, selected)
    # Retained payload from a failed/conflicted candidate is diagnostic evidence,
    # not an accepted result. Report its presence separately from usable coverage.
    verified = {key:value for key,value in selected.items()
                if value.get('status') in {'complete','partial','empty'}
                and not value.get('error')}
    result['verified_core_covered'] = coverage(reference, verified)['core_covered']
    result['atomic_coverage'] = coverage(reference, previous)
    result['answer_chars'] = len(run.get('graph_answer') or '')
    result['manual_review_required'] = True
    result['status'] = run.get('status')
    result['error'] = run.get('error')
    result['step_outcomes'] = [{k:s.get(k) for k in ('step_id','status','truncated','error','reason')} for s in previous.values()]
    result['reference_gap'] = reference['id'] in {'Q17','Q57'}
    result['empty_target_review'] = reference['id'] in {'Q52','Q55'}
    # Empty targets and unknown references must not pass vacuously.
    result['exact_reference_pass'] = None
    result['expected_clarification_pass'] = None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--env',type=Path,required=True)
    parser.add_argument('--ledger',type=Path,required=True)
    parser.add_argument('--ceiling',type=float,required=True)
    parser.add_argument('--fixture',type=Path,default=REPO/'tests_vnext/fixtures/acceptance/workflow57.json')
    parser.add_argument('--references-only',action='store_true')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--limit',type=int,default=57)
    parser.add_argument('--case-keys',nargs='+')
    args=parser.parse_args()
    os.umask(0o077)
    args.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    fixture=json.loads(args.fixture.read_text())
    cases={c['id']:c for c in fixture['cases']}
    manifest={'version':1,'model':'claude-sonnet-5','graph_release':fixture['graph_release'],
        'review_fixture_sha256':hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        'cases':[{'key':c['id'],'question':c['question'],'checks':[]} for c in fixture['cases']]}
    args.manifest=args.root/'workflow57-runner-manifest.json'
    encoded=json.dumps(manifest,indent=2)+'\n'
    if args.manifest.exists() and args.manifest.read_text()!=encoded:
        raise ValueError('Do not change a frozen run manifest')
    args.manifest.write_text(encoded)
    runner.evaluate=lambda case,frozen,run:evaluate(cases[case['key']],run)
    original_summary=runner.summarize
    def summary(rows):
        result=original_summary(rows)
        for arm in ('original','candidate'):
            result[arm].pop('exact_reference_passes',None)
            result[arm].pop('exact_reference_cases',None)
            items=[r for r in rows if r['arm']==arm]
            result[arm]['core_covered']=sum(r.get('evaluation',{}).get('core_covered',False) for r in items)
            result[arm]['answer_with_core']=sum(bool(r.get('evaluation',{}).get('core_covered') and r['evaluation']['answer_chars'] and r.get('confirm_http_status')==202) for r in items)
            result[arm]['quality_review_pending']=True
        return result
    runner.summarize=summary
    if args.references_only:
        raise ValueError('Validate graph references separately; empty legacy checks are not reference verification')
    with (args.root/'.benchmark.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        asyncio.run(runner.run(args))


if __name__=='__main__':main()
