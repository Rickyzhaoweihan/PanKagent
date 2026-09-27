"""Resolve schema-declared edge references; never infer links from shared prefixes."""
import re
from .agent_schemas import module


def recipe_for(step):
    return next((r for r in module('query_patterns').get('referenced_dependency_entities', [])
                 if step.get('relation_types') == [r['relation']]), None)


def referenced_ids(recipe, evidence):
    identifiers = set()
    for edge in evidence.get('edges', []):
        if edge.get('type') != recipe['source_relation']:
            continue
        value = (edge.get('properties') or {}).get(recipe['source_property'])
        if value in (None, ''):
            continue
        values = re.split(r'[,;|\s]+', value.strip()) if isinstance(value, str) else value
        if not isinstance(values, (list, tuple)) or any(
                not isinstance(v, str) or not re.fullmatch(recipe['id_pattern'], v) for v in values):
            return [], 'dependency_reference_format_unverified'
        identifiers.update(values)
        if len(identifiers) > recipe['max_ids']:
            return [], 'dependency_variant_resolution_limit'
    return sorted(identifiers), None
