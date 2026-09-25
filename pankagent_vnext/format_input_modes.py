"""Per-query input capabilities. This module never generates or rewrites output."""
from copy import deepcopy
import json
from .result_size_policy import EXAMPLE_LIMIT, oversized_result_metadata


def add_chain_identities(entry, evidence, max_bytes):
    paths = evidence.get('path_records') or []
    if not paths:
        return entry
    result = deepcopy(entry)
    fallback = result.get('result_size_fallback') or oversized_result_metadata(evidence, max_bytes)
    if fallback:
        result['result_size_fallback'] = fallback
    result['identity_path_records'] = []
    for path in paths:
        # Keep a whole ordered witness or omit it. No clipped IDs or orphan edges.
        witness = {'nodes': [{k: n[k] for k in ('id', 'role', 'labels')} for n in path['nodes']],
                   'edges': [{k: e[k] for k in ('role', 'type', 'start_id', 'end_id', 'fingerprint')} for e in path['edges']]}
        result['identity_path_records'].append(witness)
        if len(json.dumps(result, ensure_ascii=False).encode()) > max_bytes - 600:
            result['identity_path_records'].pop()
            break
        if len(result['identity_path_records']) >= (EXAMPLE_LIMIT if fallback else 200):
            break
    result['path_input_scope'] = {'verified_connectivity': True, 'measurements_available': False,
        'selected_path_count': len(result['identity_path_records']),
        'omitted_path_count': len(paths) - len(result['identity_path_records']),
        'retrieval_complete': evidence.get('status') == 'complete' and not evidence.get('truncated')}
    return result


def input_structure(evidence):
    return {'queries': [{'step_id': s.get('step_id'), 'evidence_id': s.get('evidence_id'),
        'input_bindings': deepcopy(s.get('input_bindings', [])),
        'operation': deepcopy((s.get('derivation') or {}).get('operation')),
        'has_verified_paths': bool(s.get('path_records'))} for s in evidence.values()],
        'interpretation': 'Each evidence item has its own input mode. Node identities alone do not establish relationships. '
        'identity_path_records are verified ordered connectivity witnesses without measurement values. '
        'Do not combine independent evidence items into an inferred chain. Omitted properties are not missing source values. '
        'Selected identities and paths are examples, not complete populations; use only explicitly verified totals.'}
