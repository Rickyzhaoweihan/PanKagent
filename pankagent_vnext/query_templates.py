"""Compile verified one-relationship checks; novel structures keep the GPU path.

Templates contain no gene, variant, disease, or answer examples. Parameters and
property ownership come from the prepared request. Every result still passes the
normal semantic validator and EXPLAIN before a database read. Shared endpoint
labels cover all registered paths compatible with the explicitly bound roles,
never just the first compatible path. Sample witnesses share one sample node.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST
from .scientific_projection import MEASUREMENT_FIELDS
from .constraint_values import list_value, DIGEST as VALUE_DIGEST
from .genomic_scope import DIGEST as GENOMIC_DIGEST

VERSION = 'typed-relation-templates-v3'
DIGEST = hashlib.sha256(Path(__file__).read_bytes() + SCHEMA_DIGEST.encode()
                       + VALUE_DIGEST.encode() + GENOMIC_DIGEST.encode()
                       + Path(__file__).with_name('annotation_selection.py').read_bytes()).hexdigest()
_SECONDARY_LABELS = {'ontology', 'sequence_variant', 'snv', 'insertion', 'indel',
                     'deletion', 'provenance'}
_OPERATORS = {'=', '!=', '<>', 'IN', '>', '>=', '<', '<=', 'CONTAINS', 'STARTS WITH', 'ENDS WITH'}
# Reuse the reviewed measurement inventory. Expression_call is categorical.
_NUMERIC_FIELDS = {kind: set(fields) - {'expression_call'}
                   for kind, fields in MEASUREMENT_FIELDS.items()}
# Quantitative membership fields independently inspected in the locus audit.
_NUMERIC_FIELDS.update({'PART_OF_QTL_SIGNAL': {'pip', 'rank', 'nominal_p'},
                        'PART_OF_GWAS_SIGNAL': {'pip', 'rank'}})
_REGION_FIELDS = {'chr', 'assembly', 'genome_assembly', 'start_loc', 'end_loc'}


def _common_endpoint(paths, side):
    common = set.intersection(*(set(path[side]) for path in paths)) - _SECONDARY_LABELS
    return next(iter(common)) if len(common) == 1 else None


def _scalar(value):
    return isinstance(value, (str, bool, int)) or isinstance(value, float) and math.isfinite(value)


def _value(value, operator, *, numeric=False):
    if operator == 'IN':
        value = list_value(value)
        if numeric:
            return [_value(member, '=', numeric=True) for member in value]
        return deepcopy(value)
    if operator in {'>', '>=', '<', '<='} or numeric and operator in {'=', '!=', '<>'}:
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


def _sample_witness(step, paths):
    """One requested donor and tissue joined to the same filtered sample."""
    if step.get('semantic_registry', {}).get('donor_required') is not True:
        return None
    requirements = step.get('sample_requirements', {})
    if requirements.get('paired') or requirements.get('separate_bindings'):
        return None
    for source in ('donor', 'anatomical_structure'):
        compatible = [p for p in paths if source in p['source']]
        if not compatible or _common_endpoint(compatible, 'target') != 'Sample_node':
            return None
    variables = {'donor': 'd', 'anatomical_structure': 't', 'Sample_node': 's'}
    filters, params, tissue_anchors, donor_predicates = [], {}, 0, 0
    try:
        for index, c in enumerate(step.get('constraints', [])):
            owner, prop, op, value = c.get('entity_type'), c.get('property'), c.get('operator', '='), c.get('value')
            if c.get('owner_kind') not in (None, 'node') or c.get('relationship_type'):
                return None  # An edge predicate needs a specific path owner.
            resolved = _resolved_entity(step, index, c)
            if resolved:
                if owner not in (None, resolved['entity_type']):
                    return None
                owner, prop, value = resolved['entity_type'], 'id', resolved['id']
                tissue_anchors += owner == 'anatomical_structure'
            if owner not in variables or prop not in REGISTRY['nodes'][owner] or op not in _OPERATORS:
                return None
            if op in {'>', '>=', '<', '<='} or owner == 'donor' and prop == 'age':
                return None  # Mixed-unit ages and unverified numeric storage.
            donor_predicates += owner == 'donor'
            parameter = 'template_' + str(index)
            params[parameter] = _value(value, op)
            cypher_op = '<>' if op == '!=' else op
            filters.append(f'{variables[owner]}.`{prop}` {cypher_op} ${parameter}')
    except (ValueError, TypeError, OverflowError):
        return None
    if tissue_anchors != 1 or not donor_predicates:
        return None
    query = ('MATCH (d:`donor`)-[rd:`HAS_SAMPLE`]->(s:`Sample_node`)<-[rt:`HAS_SAMPLE`]-(t:`anatomical_structure`)\n'
             'WHERE ' + ' AND '.join(filters) + '\n'
             'RETURN collect(DISTINCT d) + collect(DISTINCT t) + collect(DISTINCT s) AS nodes, '
             'collect(DISTINCT rd) + collect(DISTINCT rt) AS edges')
    return {'cypher': query, 'parameters': params, 'template_id': 'donor_tissue_same_sample_records',
            'version': VERSION, 'sha256': DIGEST, 'schema_sha256': SCHEMA_DIGEST,
            'endpoint_coverage': {'sources': ['donor', 'anatomical_structure'], 'target': 'Sample_node',
                'registered_path_count': len(paths), 'all_paths_covered': False,
                'all_requested_paths_covered': True, 'scope_basis': 'typed_donor_and_resolved_tissue_same_sample'}}


def _region_gene_records(step):
    """Enumerate the full verified interval without requiring a named anchor."""
    from .genomic_scope import has_verified_region_scope, is_verified_region_constraint
    if not has_verified_region_scope(step):
        return None
    filters, params = [], {}
    try:
        for index, constraint in enumerate(step.get('constraints', [])):
            prop, operator = constraint.get('property'), constraint.get('operator', '=')
            value = constraint.get('value')
            if (constraint.get('entity_type') != 'Gene' or constraint.get('owner_kind') not in (None, 'node')
                    or constraint.get('relationship_type') or prop not in REGISTRY['nodes']['Gene']
                    or operator not in _OPERATORS):
                return None
            verified_coordinate = is_verified_region_constraint(constraint, step)
            if prop in _REGION_FIELDS and not verified_coordinate:
                return None
            resolved = _resolved_entity(step, index, constraint)
            if resolved:
                prop, value = 'id', resolved['id']
            numeric = prop in {'start_loc', 'end_loc'} and verified_coordinate
            if operator in {'>', '>=', '<', '<='} and not numeric:
                return None
            parameter = 'template_' + str(index)
            params[parameter] = _value(value, operator, numeric=numeric)
            cypher_operator = '<>' if operator == '!=' else operator
            filters.append(f'g.`{prop}` {cypher_operator} ${parameter}')
    except (ValueError, TypeError, OverflowError):
        return None
    query = ('MATCH (g:`Gene`)\nWHERE ' + ' AND '.join(filters)
             + '\nRETURN collect(DISTINCT g) AS nodes, [] AS edges')
    return {'cypher': query, 'parameters': params, 'template_id': 'verified_gene_region_records',
            'version': VERSION, 'sha256': DIGEST, 'schema_sha256': SCHEMA_DIGEST,
            'endpoint_coverage': {'source': 'Gene', 'all_requested_paths_covered': True,
                                  'scope_basis': 'verified_complete_gene_interval'}}


def compile_query(step):
    from .annotation_selection import overview
    bounded_annotation = overview(step)
    if step.get('graph_version') != REGISTRY['release'] or (not step.get('complete', True) and not bounded_annotation):
        return None
    if any(step.get(key) for key in ('depends_on', 'ranking', 'semantic_issues', 'anatomy_scope_issue', 'coloc_scope_issue')):
        return None
    kinds = step.get('relation_types', [])
    if not kinds:
        return _region_gene_records(step)
    if len(kinds) != 1 or kinds[0] not in REGISTRY['relations']:
        return None
    kind = kinds[0]
    paths = REGISTRY['relations'][kind]['paths']
    if not paths:
        return None
    if kind == 'HAS_SAMPLE' and step.get('semantic_registry', {}).get('donor_required') is True:
        return _sample_witness(step, paths)
    selected_paths = paths
    left, right = (_common_endpoint(paths, side) for side in ('source', 'target'))
    if kind == 'HAS_SAMPLE':
        requirements = step.get('sample_requirements', {})
        if (step.get('semantic_registry', {}).get('donor_required') is not False
                or requirements.get('paired') or requirements.get('separate_bindings')):
            return None
        # HAS_SAMPLE has several source types. A positively resolved tissue
        # identity proves which source path the request means; retain every
        # registered path compatible with that identity, never an arbitrary one.
        try:
            anchors = [_resolved_entity(step, index, c)
                       for index, c in enumerate(step.get('constraints', []))]
        except (ValueError, TypeError):
            return None
        tissue_anchors = [a for a in anchors if a and a['entity_type'] == 'anatomical_structure']
        if len(tissue_anchors) != 1:
            return None
        selected_paths = [p for p in paths if 'anatomical_structure' in p['source']]
        if not selected_paths:
            return None
        left, right = (_common_endpoint(selected_paths, side) for side in ('source', 'target'))
    if not left or (not right and not bounded_annotation) or left == right:
        return None
    from .genomic_scope import has_verified_region_scope, is_verified_region_constraint
    region_scope = has_verified_region_scope(step)
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
            if owner is not None and owner in (left, right):
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
            verified_coordinate = owner == 'Gene' and is_verified_region_constraint(constraint, step)
            if region_scope and owner == 'Gene' and prop in _REGION_FIELDS and not verified_coordinate:
                return None
            numeric = (variable == 'r' and prop in _NUMERIC_FIELDS.get(kind, set())
                       or verified_coordinate and prop in {'start_loc', 'end_loc'})
            if operator in {'>', '>=', '<', '<='} and not numeric:
                # Numeric storage is not established by a property name alone.
                # Unregistered types retain the GPU route and normal guards.
                return None
            value = _value(value, operator, numeric=numeric)
            param = 'template_' + str(index)
            params[param] = value
            cypher_operator = '<>' if operator == '!=' else operator
            filters.append(f'{variable}.`{prop}` {cypher_operator} ${param}')
    except (ValueError, TypeError, OverflowError):
        return None
    if not filters or not (resolved_count or region_scope):
        return None
    target = f'(b:`{right}`)' if right else '(b)'
    query = f'MATCH (a:`{left}`)-[r:`{kind}`]->{target}\nWHERE ' + ' AND '.join(filters)
    if bounded_annotation:
        query += '\nWITH a, b, r ORDER BY a.id, b.id, elementId(r) LIMIT 10'
    query += '\nRETURN collect(DISTINCT a) + collect(DISTINCT b) AS nodes, collect(DISTINCT r) AS edges'
    return {'cypher': query, 'parameters': params, 'template_id': 'directed_relation_records',
            'version': VERSION, 'sha256': DIGEST, 'schema_sha256': SCHEMA_DIGEST,
            'endpoint_coverage': {'source': left, 'target': right,
                                  'registered_path_count': len(paths),
                                  'compatible_path_count': len(selected_paths),
                                  'all_paths_covered': selected_paths == paths,
                                  'all_requested_paths_covered': True,
                                  'scope_basis': 'resolved_tissue_identity' if selected_paths != paths else 'shared_registered_endpoint'}}


def compile_variant_dependencies(step, dependency_bindings):
    """Bind verified dependency variants to the source of a GWAS template.

    Every dependency predicate remains present, including an explicitly requested
    intersection. The existing validator still checks all filters and ownership.
    """
    from copy import deepcopy
    if step.get('relation_types') != ['PART_OF_GWAS_SIGNAL'] or not step.get('depends_on'):
        return None
    names = ['dep_' + str(i) for i in range(len(step['depends_on']))]
    for name in names:
        binding = dependency_bindings.get(name, {})
        labels = binding.get('id_labels', {})
        if binding.get('graph_version') != REGISTRY['release'] or not labels or any('variants' not in v for v in labels.values()):
            return None
    plain = deepcopy(step)
    plain['depends_on'] = []
    query = compile_query(plain)
    if not query or query['endpoint_coverage'].get('source') != 'variants':
        return None
    query['cypher'] = query['cypher'].replace('\nWHERE ', '\nWHERE ' + ' AND '.join('a.id IN $' + n for n in names) + ' AND ', 1)
    query['template_id'] = 'verified_variant_dependency_gwas'
    return query
