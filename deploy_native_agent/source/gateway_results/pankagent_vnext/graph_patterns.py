"""Reusable release-grounded query shapes, without entity or answer examples.

The complete directed registry supplies the triples. Reviewed JSON supplies
composition rules. These are guidance facts, not an execution allowlist: novel
questions still use the grounded generator and full query validation.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST, guidance as schema_guidance

_RAW = Path(__file__).with_suffix('.json').read_bytes()
LIBRARY = json.loads(_RAW)
VERSION = LIBRARY['version']
DIGEST = hashlib.sha256(_RAW + Path(__file__).read_bytes() + SCHEMA_DIGEST.encode()).hexdigest()


def _verified_rule(rule):
    return (LIBRARY['graph_release'] == REGISTRY['release']
            and all(relation in REGISTRY['relations'] for relation in rule['relations'])
            and all(any(source in path['source'] and target in path['target']
                        for path in REGISTRY['relations'].get(relation, {}).get('paths', []))
                    for source, relation, target in rule['required_paths']))


def patterns_for(relation_types):
    """Return fresh compact facts for known requested categories only.

    Every alternative endpoint label set is preserved; two alternatives are
    never flattened into an invented cross-product path. Unknown categories
    simply have no stored pattern and remain with ordinary schema discovery.
    """
    if LIBRARY['graph_release'] != REGISTRY['release']:
        return []
    result = []
    for relation in dict.fromkeys(relation_types or []):
        spec = REGISTRY['relations'].get(relation)
        if spec is None:
            continue
        paths = sorted({(tuple(sorted(path['source'])), tuple(sorted(path['target']))) for path in spec['paths']})
        result.append({'id': 'direct:' + relation, 'graph_release': REGISTRY['release'],
                       'relationship': relation,
                       'directed_paths': [{'source_labels': list(source), 'target_labels': list(target)} for source, target in paths],
                       'relationship_properties': list(spec['properties']),
                       'join_rules': [deepcopy(rule) for rule in LIBRARY['rules']
                                      if relation in rule['relations'] and _verified_rule(rule)]})
    return result


def guidance(relation_types, include_paths=True):
    patterns = patterns_for(relation_types)
    rules = {rule['id']: rule['guidance'] for pattern in patterns for rule in pattern['join_rules']}
    prefix = schema_guidance(relation_types) if include_paths else ''
    return prefix + ('\nVerified composition rules:\n' + '\n'.join(rules.values()) if rules else '')
