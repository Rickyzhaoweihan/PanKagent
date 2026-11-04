"""Compile planner-selected scope without reinterpreting natural language.

Property/vocabulary ownership belongs to the schema pack. This module verifies
recorded values and emits existing execution metadata; it never parses a user's
words to invent, remove, or change a predicate.
"""
from copy import deepcopy
import json
from .agent_schemas import module as schema_module


def prepare(step, vocabulary, release):
    out = deepcopy(step)
    config = schema_module('validation').get('planner_bindings', {})
    digest = vocabulary.get('inventory_sha256')
    fields = {(x['owner'], x['property']): x['vocabulary_path']
              for x in config.get('fields', [])}
    issues, proofs, groups, excluded = [], [], [], []
    for constraint in out.get('constraints', []):
        key = (constraint.get('entity_type'), constraint.get('property'))
        path = fields.get(key)
        op = constraint.get('operator', '=')
        value = constraint.get('value')
        try:
            values = json.loads(value) if op in {'IN', 'NOT IN'} and isinstance(value, str) else value
        except ValueError:
            values = None
        values = values if isinstance(values, list) else [values]
        if path:
            available = vocabulary
            for part in path:
                available = available.get(part) if isinstance(available, dict) else None
            if not isinstance(available, list) or not digest:
                issues.append(f'Recorded values could not be verified for {key[0]}.{key[1]}.')
            elif not values or any(v not in available for v in values):
                issues.append(f'The planner-selected value for {key[0]}.{key[1]} is not in the recorded category inventory.')
            else:
                proofs.append({'canonical_binding': deepcopy(constraint),
                    'match_kind': 'verified_runtime_planner_binding',
                    'source': 'current graph inventory; interpretation selected by planner',
                    'graph_release': release, 'inventory_sha256': digest})
        if list(key) == config.get('assay_field'):
            if op in {'!=', '<>', 'NOT IN'}:
                excluded.append(deepcopy(constraint))
            elif op in {'=', 'IN'}:
                groups.append(values)
    out['semantic_issues'] = issues
    out['resolved_constraints'] = proofs
    out['semantic_registry'] = {'version': 'planner-selected-bindings-v1',
        'scope_authority': 'planner', 'graph_release': release,
        'inventory_sha256': digest,
        'modality_links_verified': vocabulary.get('modality_links_verified', False),
        'donor_required': any(c.get('entity_type') == config.get('cohort_owner')
                              for c in out.get('constraints', []))}
    out['sample_requirements'] = {'modality_groups': groups, 'paired': False,
        'separate_bindings': len(groups) > 1, 'source': 'planner_selected_constraints',
        'file_availability': 'not_verified', 'excluded_modality_constraints': excluded,
        'capability_scope_verified': False}
    out['scope_language_advice'] = []
    return out
