"""Exact case-equivalent donor labels from the current complete inventory.

This does not equate sex and gender, merge diagnoses, or invent aliases.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

VERSION = 'donor-categorical-case-v1'
CATEGORICAL_FIELDS = ('gender', 'sex_at_birth', 'race', 'donation_type',
                      'aab_state', 'hla_status')
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def resolve_categories(constraints, vocabulary, properties, release):
    result = deepcopy(constraints)
    matches = []
    if release != 'PanKgraph_08_04':
        return result, matches
    values = vocabulary.get('donor_categorical_values') or {}
    for c in result:
        prop = c.get('property')
        owners = [label for label, fields in properties.items() if prop in fields]
        if c.get('entity_type') is None and owners == ['donor']:
            original = deepcopy(c)
            c['entity_type'] = 'donor'
            matches.append({'requested': original, 'canonical_binding': deepcopy(c),
                            'match_kind': 'verified_property_owner', 'registry_version': VERSION,
                            'source': 'release-specific property ownership'})
        if (c.get('entity_type') != 'donor' or prop not in CATEGORICAL_FIELDS
                or vocabulary.get('donor_categories_complete') is not True
                or c.get('operator', '=') not in ('=', 'IN')):
            continue
        available = values.get(prop)
        if not isinstance(available, list) or any(not isinstance(v, str) for v in available):
            continue
        raw = c.get('value')
        is_list = c.get('operator') == 'IN'
        try:
            wanted = json.loads(raw) if is_list and isinstance(raw, str) else raw if is_list else [raw]
        except (TypeError, ValueError):
            continue
        if not isinstance(wanted, list) or not wanted or any(not isinstance(v, str) for v in wanted):
            continue
        mapped = []
        for value in wanted:
            # Exact matches win; two differently cased recorded labels are not merged.
            candidates = [value] if value in available else list(dict.fromkeys(
                v for v in available if v.casefold() == value.casefold()))
            if len(candidates) != 1:
                break
            mapped.append(candidates[0])
        if len(mapped) != len(wanted) or mapped == wanted:
            continue
        original = deepcopy(c)
        c['value'] = (json.dumps(mapped) if isinstance(raw, str) else mapped) if is_list else mapped[0]
        matches.append({'requested': original, 'canonical_binding': deepcopy(c),
                        'match_kind': 'verified_case_alias', 'registry_version': VERSION,
                        'source': 'complete distinct donor categorical inventory',
                        'graph_release': release})
    return result, matches
