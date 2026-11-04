"""Release-verified donor terminology and directed sample-path checks."""
from copy import deepcopy
import re
VERSION = 'donor-query-guard-v1'
DIAGNOSES = {'type 1 diabetes':'Diabetes (Type I)', 't1d':'Diabetes (Type I)',
             'type 2 diabetes':'Diabetes (Type II)', 't2d':'Diabetes (Type II)'}

def normalize_diagnosis(step):
    result = deepcopy(step)
    for constraint in result.get('constraints', []):
        if constraint.get('entity_type') != 'donor' or constraint.get('property') != 'diabetes_type' or constraint.get('operator') != '=':
            continue
        value = constraint.get('value')
        canonical = DIAGNOSES.get(str(value).strip().lower())
        if canonical:
            constraint['value'] = canonical
            result.setdefault('resolved_constraints', []).append({'requested':value, 'canonical_binding':deepcopy(constraint),
                'match_kind':'alias', 'registry_version':VERSION, 'source':'PanKgraph_08_04 verified donor.diabetes_type categorical values'})
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
