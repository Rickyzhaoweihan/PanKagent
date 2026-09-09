"""Compile verified one-relationship checks; novel structures keep the GPU path.

Templates contain no gene, variant, disease, or answer examples. Parameters and
property ownership come from the prepared request. Every result still passes the
normal semantic validator and EXPLAIN before a database read. Shared endpoint
labels must cover every registered path, never just the first compatible path.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST
from .scientific_projection import MEASUREMENT_FIELDS

VERSION = 'typed-relation-templates-v1'
DIGEST = hashlib.sha256(Path(__file__).read_bytes() + SCHEMA_DIGEST.encode()).hexdigest()
_SECONDARY_LABELS = {'ontology', 'sequence_variant', 'snv', 'insertion', 'indel',
                     'deletion', 'provenance'}
_OPERATORS = {'=', 'IN', '>', '>=', '<', '<=', 'CONTAINS', 'STARTS WITH', 'ENDS WITH'}
# Reuse the reviewed measurement inventory. Expression_call is categorical.
_NUMERIC_FIELDS = {kind: set(fields) - {'expression_call'}
                   for kind, fields in MEASUREMENT_FIELDS.items()}
# Quantitative membership fields independently inspected in the locus audit.
_NUMERIC_FIELDS.update({'PART_OF_QTL_SIGNAL': {'pip', 'rank', 'nominal_p'},
                        'PART_OF_GWAS_SIGNAL': {'pip', 'rank'}})


def _common_endpoint(paths, side):
    common = set.intersection(*(set(path[side]) for path in paths)) - _SECONDARY_LABELS
    return next(iter(common)) if len(common) == 1 else None


def _scalar(value):
    return isinstance(value, (str, bool, int)) or isinstance(value, float) and math.isfinite(value)


def _value(value, operator, *, numeric=False):
    if operator == 'IN':
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, list) or not all(_scalar(v) for v in value):
            raise ValueError('unsupported_list_value')
        if numeric:
            return [_value(member, '=', numeric=True) for member in value]
        return deepcopy(value)
    if operator in {'>', '>=', '<', '<='} or numeric and operator == '=':
        # JSON decoding preserves integers beyond 2**53, unlike a float cast.
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not _scalar(value):
            raise ValueError('unsupported_numeric_value')
    if not _scalar(value):
        raise ValueError('unsupported_scalar_value')
    if operator in {'CONTAINS', 'STARTS WITH', 'ENDS WITH'} and not isinstance(value, str):
        raise ValueError('unsupported_string_value')
    return value


def _resolved_entity(step, index, constraint):
    choices = [e for e in step.get('resolved_entities', []) if e.get('constraint_index') == index]
    if not choices:
        return None
    if len(choices) != 1:
        raise ValueError('duplicate_resolution')
    entity = choices[0]
    if (entity.get('state') == 'literal_predicate'
            and entity.get('graph_version') == step['graph_version']
            and entity.get('requested') == constraint):
        # No entity replacement occurs. The canonical, typed predicate below
        # remains exact, including verified QTL tissue property bindings.
        return None
    if (entity.get('state') != 'resolved' or entity.get('graph_version') != step['graph_version']
            or entity.get('requested') != constraint or not isinstance(entity.get('id'), str)
            or not entity['id'] or entity.get('entity_type') not in entity.get('labels', [])
            or constraint.get('property') not in {'id', 'name'}
            or constraint.get('operator', '=') != '='):
        raise ValueError('unverified_resolution')
    return entity


def compile_query(step):
    if step.get('graph_version') != REGISTRY['release'] or not step.get('complete', True):
        return None
    if any(step.get(key) for key in ('depends_on', 'ranking', 'semantic_issues', 'anatomy_scope_issue', 'coloc_scope_issue')):
        return None
    kinds = step.get('relation_types', [])
    if len(kinds) != 1 or kinds[0] not in REGISTRY['relations']:
        return None
    kind = kinds[0]
    paths = REGISTRY['relations'][kind]['paths']
    if not paths:
        return None
    left, right = (_common_endpoint(paths, side) for side in ('source', 'target'))
    if not left or not right or left == right:
        return None
    filters, params, resolved_count = [], {}, 0
    try:
        for index, constraint in enumerate(step.get('constraints', [])):
            prop, owner = constraint.get('property', ''), constraint.get('entity_type')
            owner_kind, relationship = constraint.get('owner_kind'), constraint.get('relationship_type')
            value, operator = constraint.get('value'), constraint.get('operator', '=')
            if owner_kind not in (None, 'node', 'relationship') or relationship and relationship != kind:
                return None
            if owner is not None and (owner_kind == 'relationship' or relationship):
                return None
            resolved = _resolved_entity(step, index, constraint)
            if resolved is not None:
                if owner not in (None, resolved['entity_type']) or relationship or owner_kind == 'relationship':
                    return None
                owner, prop, value = resolved['entity_type'], 'id', resolved['id']
                resolved_count += 1
            if owner in (left, right):
                variable = 'a' if owner == left else 'b'
                allowed = REGISTRY['nodes'].get(owner, [])
            elif owner is None:
                if owner_kind == 'node':
                    return None
                # Generic fields require explicit ownership. Never choose edge
                # data_source instead of the donor/gene/sample source silently.
                if not relationship and prop in set(REGISTRY['nodes'].get(left, [])) | set(REGISTRY['nodes'].get(right, [])):
                    return None
                variable, allowed = 'r', REGISTRY['relations'][kind]['properties']
            else:
                return None
            if prop not in allowed or operator not in _OPERATORS:
                return None
            if owner == 'donor' and prop == 'age' and operator in {'=', '>', '>=', '<', '<='}:
                # Recorded ages mix months and years; the reviewed expression is
                # required rather than raw numeric filtering of this field.
                return None
            numeric = variable == 'r' and prop in _NUMERIC_FIELDS.get(kind, set())
            if operator in {'>', '>=', '<', '<='} and not numeric:
                # Numeric storage is not established by a property name alone.
                # Unregistered types retain the GPU route and normal guards.
                return None
            value = _value(value, operator, numeric=numeric)
            param = 'template_' + str(index)
            params[param] = value
            filters.append(f'{variable}.`{prop}` {operator} ${param}')
    except (ValueError, TypeError, OverflowError):
        return None
    if not filters or not resolved_count:
        return None
    query = f'MATCH (a:`{left}`)-[r:`{kind}`]->(b:`{right}`)\nWHERE ' + ' AND '.join(filters)
    query += '\nRETURN collect(DISTINCT a) + collect(DISTINCT b) AS nodes, collect(DISTINCT r) AS edges'
    return {'cypher': query, 'parameters': params, 'template_id': 'directed_relation_records',
            'version': VERSION, 'sha256': DIGEST, 'schema_sha256': SCHEMA_DIGEST,
            'endpoint_coverage': {'source': left, 'target': right,
                                  'registered_path_count': len(paths), 'all_paths_covered': True}}
