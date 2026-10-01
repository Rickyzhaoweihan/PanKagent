"""Core/extra review-suite evaluation and the current isolated single-arm runner.

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
import re
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).parent))


def _typed(value):
    """Keep booleans, numbers and strings distinct in category comparisons."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _values(value):
    return value if isinstance(value, list) else [value]


def projection_fields(selector, result):
    """Recognize direct/collect projections using the executed query's owner.

    Generic aliases such as ``value`` are accepted only when that query binds
    the requested typed owner and projects its property. This is deliberately
    a small scoring grammar, not a Cypher validator or an LLM answer parser.
    """
    queries = result.get('queries') or []
    if not queries:
        return set()
    query = queries[-1].get('cypher', '') if isinstance(queries[-1], dict) else queries[-1]
    fields = set()
    sources = [selector.get('node_property') or {}]
    identity = selector.get('node_identity') or {}
    if identity:
        sources.append({'label': identity.get('label'), 'property': identity.get('field')})
    for source in sources:
        if not source.get('label') or not source.get('property'):
            continue
        label, prop = re.escape(source['label']), re.escape(source['property'])
        bindings = re.findall(r'\(\s*`?(\w+)`?\s*:\s*`?' + label + r'`?(?=[\s:{)])', query)
        for variable in bindings:
            expression = r'`?' + re.escape(variable) + r'`?\s*\.\s*`?' + prop + r'`?'
            projections = re.findall(r'\bRETURN\b(.*?)(?=\b(?:ORDER\s+BY|LIMIT|SKIP|UNION)\b|$)',
                                     query, flags=re.I | re.S)
            for projection in projections:
                for match in re.finditer(r'(?:collect\s*\(\s*(?:DISTINCT\s+)?)?' + expression +
                                         r'\s*\)?\s+AS\s+`?(\w+)`?', projection, flags=re.I):
                    fields.add(match.group(1))
                if re.search(r'(?:^|,)\s*(?:DISTINCT\s+)?' + expression + r'\s*(?=,|$)', projection, re.I):
                    # Neo4j uses a bare expression itself as the column key.
                    fields.add(variable + '.' + source['property'])
    return fields


def filtered_scalar_fields(node, result):
    """Prove row-local property filters on the same typed projected owner.

    A category alias alone is not ownership evidence. Collect projections cannot
    preserve name/category pairing, so this deliberately accepts only ordinary
    scalar projections from one RETURN, without UNION branches.
    """
    queries = result.get('queries') or []
    if not queries:
        return {}
    query = queries[-1].get('cypher', '') if isinstance(queries[-1], dict) else queries[-1]
    if re.search(r'\bUNION\b', query, re.I):
        return {}
    projections = re.findall(r'\bRETURN\b(.*?)(?=\b(?:ORDER\s+BY|LIMIT|SKIP)\b|$)',
                             query, flags=re.I | re.S)
    if len(projections) != 1 or not node.get('label') or not node.get('property'):
        return {}
    bindings = re.findall(r'\(\s*`?(\w+)`?\s*:\s*`?' + re.escape(node['label']) +
                          r'`?(?=[\s:{)])', query)
    fields = {}
    for variable in bindings:
        owner_fields = {}
        for prop in {node['property'], *node.get('where', {})}:
            expression = r'`?' + re.escape(variable) + r'`?\s*\.\s*`?' + re.escape(prop) + r'`?'
            matches = re.finditer(r'(?:^|,)\s*(?:DISTINCT\s+)?' + expression +
                                  r'(?:\s+AS\s+`?(\w+)`?)?\s*(?=,|$)',
                                  projections[0], flags=re.I)
            owner_fields[prop] = {m.group(1) or variable + '.' + prop for m in matches}
        if all(owner_fields[prop] for prop in node.get('where', {})):
            conditions = [(owner_fields[prop], expected) for prop, expected in node.get('where', {}).items()]
            for field in owner_fields[node['property']]:
                fields[field] = conditions
    return fields


def selected_values(assertion, results):
    """Extract only declared fields/properties; never search answer prose for gold."""
    selector = assertion.get('selector', {})
    fields = set(selector.get('row_fields', []))
    node = selector.get('node_property') or {}
    identity = selector.get('node_identity') or {}
    values, contributors, unproved_rows = {}, [], set()
    for key, result in results.items():
        derived_fields = projection_fields(selector, result)
        fields_for_step = fields | derived_fields
        filtered_fields = filtered_scalar_fields(node, result) if node.get('where') else None
        if filtered_fields is not None:
            fields_for_step = set(filtered_fields)
        found = bool(filtered_fields if filtered_fields is not None else derived_fields) and not result.get('rows')
        for row in result.get('rows', []):
            if filtered_fields and not (fields_for_step & row.keys()):
                unproved_rows.add(key)
            for field in fields_for_step & row.keys():
                if filtered_fields is not None:
                    conditions = filtered_fields[field]
                    if any(not (columns & row.keys()) for columns, _ in conditions):
                        unproved_rows.add(key)
                        continue
                    # Lists erase row-local pairing; even singleton lists do not
                    # establish a scalar category/value ownership contract.
                    if isinstance(row[field], (list, dict)) or any(
                            isinstance(row[column], (list, dict))
                            for columns, _ in conditions for column in columns & row.keys()):
                        unproved_rows.add(key)
                        continue
                    found = True  # Nonmatching categories can prove an empty subset.
                    if any(any(_typed(row[column]) != _typed(expected)
                               for column in columns & row.keys()) for columns, expected in conditions):
                        continue
                found = True
                for value in _values(row[field]):
                    if value is not None:
                        values[_typed(value)] = value
        if node:
            for record in result.get('nodes', []):
                if node.get('label') not in record.get('labels', []):
                    continue
                props = record.get('properties') or {}
                if any(field not in props or _typed(props[field]) != _typed(value)
                       for field, value in node.get('where', {}).items()):
                    continue
                if node.get('property') in props:
                    found = True
                    for value in _values(props[node['property']]):
                        if value is not None:
                            values[_typed(value)] = value
        if identity:
            for record in result.get('nodes', []):
                if identity.get('label') in record.get('labels', []) and identity.get('field') in record:
                    found = True
                    value = record[identity['field']]
                    if value is not None:
                        values[_typed(value)] = value
        if found:
            contributors.append(key)
    complete = bool(contributors) and not unproved_rows and all(
        results[key].get('status') in {'complete', 'empty'}
        and results[key].get('truncated') is False and not results[key].get('error')
        for key in contributors)
    return values, contributors, complete


def assertion_coverage(reference, results):
    checks = []
    for assertion in reference.get('assertions', []):
        kind = assertion.get('kind')
        verified = assertion.get('reference_status') == 'verified'
        check = {'id': assertion['id'], 'kind': kind, 'reference_verified': verified,
                 'pass': False}
        if kind in {'schema_evidence', 'scope_evidence'}:
            # A schema checksum or a mention of a term does not establish a
            # grounded explanation. Keep this explicit until a human reviews it.
            check.update(manual_review_required=True,
                         expected_concepts=assertion.get('expected_concepts', []))
            if kind == 'scope_evidence':
                check['required_constraints'] = assertion.get('required_constraints', [])
            check['pass'] = None
        elif kind in {'distinct_values', 'cardinality'}:
            actual, contributors, complete = selected_values(assertion, results)
            expected = assertion.get('expected_values')
            expected_keys = {_typed(value) for value in expected} if isinstance(expected, list) else None
            count = assertion.get('expected_count')
            value_match = expected_keys is not None and set(actual) == expected_keys
            cardinality_match = type(count) is int and len(actual) == count
            passed = value_match if kind == 'distinct_values' else cardinality_match
            check.update(actual_values=[actual[key] for key in sorted(actual)],
                         actual_count=len(actual), expected_values=expected,
                         expected_count=count, contributing_steps=contributors,
                         complete=complete, missing_values=[] if expected_keys is None else
                         [json.loads(key) for key in sorted(expected_keys - actual.keys())],
                         unexpected_values=[] if expected_keys is None else
                         [actual[key] for key in sorted(actual.keys() - expected_keys)])
            check['pass'] = bool(verified and complete and passed)
        else:
            check['error'] = 'unsupported_assertion_kind'
        checks.append(check)
    return checks


def group_summary(rows, cases):
    """Report regressions separately from new coverage, including denominators."""
    groups = {'original_57': [], 'introductions_10': []}
    for case in cases:
        number = int(case['id'][1:])
        groups['original_57' if number <= 57 else 'introductions_10'].append(case['id'])
    return {name: {
        'selected_cases': len(ids),
        'attempts': len(items := [row for row in rows if row['case'] in ids]),
        'verified_core_covered': sum(bool(row.get('evaluation', {}).get('verified_core_covered')) for row in items),
        'answers': sum(bool(row.get('evaluation', {}).get('answer_chars')) for row in items),
        'reference_review_pending': sum(bool(row.get('evaluation', {}).get('reference_gap') or
                                             row.get('evaluation', {}).get('manual_assertion_review')) for row in items),
        'cost_usd': sum(row.get('accounting', {}).get('settled_cost_usd', 0) for row in items),
        'pending_bound_usd': sum(row.get('accounting', {}).get('pending_bound_usd', 0) for row in items)}
        for name, ids in groups.items()}


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
    assertions = assertion_coverage(reference, results)
    reference_verified = reference.get('reference_validation', {}).get('status', 'verified') == 'verified'
    assertions_ok = all(check['pass'] is True for check in assertions)
    return {'required_nodes':len(expected_nodes),'required_edges':len(expected_edges),
        'missing_nodes':sorted(expected_nodes-nodes),
        'missing_edges':[list(e) for e in sorted(expected_edges-keys)],
        'property_failures':property_failures,
        'core_covered':bool(expected_nodes or expected_edges or assertions) and reference_verified
            and expected_nodes<=nodes and expected_edges<=keys and not property_failures and assertions_ok,
        'assertions':assertions,
        'manual_assertion_review':any(check.get('manual_review_required') for check in assertions),
        'reference_verified':reference_verified,
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
    result['reference_gap'] = reference['id'] in {'Q17','Q57'} or not result['reference_verified'] or any(
        not check['reference_verified'] for check in result['assertions'])
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
    parser.add_argument('--limit',type=int,help='Optional maximum; defaults to all fixture cases')
    parser.add_argument('--case-keys',nargs='+')
    parser.add_argument('--arm',default='candidate')
    parser.add_argument('--model',default='claude-sonnet-5-5')
    args=parser.parse_args()
    os.umask(0o077)
    args.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    fixture=json.loads(args.fixture.read_text())
    cases={c['id']:c for c in fixture['cases']}
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    selected = args.case_keys or list(cases)
    if args.limit is not None:
        selected = selected[:args.limit]
    args.case_keys = selected
    manifest={'version':2,'model':args.model,'graph_release':fixture['graph_release'],
        'review_fixture_sha256':hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        'cases':[{'key':c['id'],'question':c['question'],'checks':[]} for c in fixture['cases']]}
    args.manifest=args.root/'workflow57-runner-manifest.json'
    encoded=json.dumps(manifest,indent=2)+'\n'
    if args.manifest.exists() and args.manifest.read_text()!=encoded:
        raise ValueError('Do not change a frozen run manifest')
    args.manifest.write_text(encoded)
    if args.references_only:
        raise ValueError('Validate graph references separately; empty legacy checks are not reference verification')
    from original_workflow import run
    with (args.root/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        asyncio.run(run(args))


if __name__=='__main__':main()
