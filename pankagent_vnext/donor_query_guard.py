"""Runtime-verified donor terminology and directed sample-path checks."""
from copy import deepcopy
import re
VERSION = 'donor-query-guard-v2-runtime'


def _kind(value):
    value = re.sub(r'[^a-z0-9]+', ' ', str(value).casefold()).strip()
    if re.search(r'\b(?:t1d(?:m)?|type (?:1|i) diabetes|diabetes type (?:1|i)|diabetes mellitus type (?:1|i))\b', value):
        return '1'
    if re.search(r'\b(?:t2d(?:m)?|type (?:2|ii) diabetes|diabetes type (?:2|ii)|diabetes mellitus type (?:2|ii))\b', value):
        return '2'
    return None


def normalize_diagnosis(step, vocabulary=None):
    result = deepcopy(step)
    values = ((vocabulary or {}).get('donor_categorical_values') or {}).get('diabetes_type')
    complete = (vocabulary or {}).get('donor_categories_complete') is True and isinstance(values, list)
    for constraint in result.get('constraints', []):
        if constraint.get('entity_type') != 'donor' or constraint.get('property') != 'diabetes_type' or constraint.get('operator') != '=':
            continue
        value = constraint.get('value')
        requested_kind = _kind(value)
        candidates = [candidate for candidate in values or []
                      if isinstance(candidate, str) and _kind(candidate) == requested_kind]
        if complete and requested_kind and len(candidates) == 1:
            constraint['value'] = candidates[0]
            proof = {'requested':value, 'canonical_binding':deepcopy(constraint),
                'match_kind':'verified_runtime_diagnosis_category', 'registry_version':VERSION,
                'source':'current complete distinct donor.diabetes_type inventory',
                'graph_release': result.get('graph_version') or (result.get('semantic_registry') or {}).get('graph_release'),
                'inventory_sha256': (vocabulary or {}).get('inventory_sha256')}
            existing = [match for match in result.setdefault('resolved_constraints', [])
                        if match.get('canonical_binding') == proof['canonical_binding']
                        and str(match.get('match_kind', '')).startswith('verified_runtime_')
                        and match.get('graph_release') == proof['graph_release']
                        and match.get('inventory_sha256') == proof['inventory_sha256']]
            if not existing:
                result['resolved_constraints'].append(proof)
        elif requested_kind and value not in (values or []):
            result.setdefault('semantic_issues', []).append(
                'Requested recorded diabetes category cannot be uniquely resolved in the current graph inventory.')
    return result

def sample_path_errors(bindings, paths):
    errors=[]
    for source,target,kinds in paths:
        if 'HAS_SAMPLE' not in kinds: continue
        a,b=bindings.get(source,set()),bindings.get(target,set())
        # Require explicit release direction. Rejecting an undirected spelling
        # is conservative; the bounded retry can emit the verified direction.
        if 'Sample_node' in a or (b and 'Sample_node' not in b):
            errors.append('invalid_sample_path:use_donor_or_anatomy_or_modality_HAS_SAMPLE_to_Sample_node')
    return list(dict.fromkeys(errors))
