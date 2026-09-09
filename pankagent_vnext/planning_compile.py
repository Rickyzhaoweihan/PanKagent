"""Compile verified property ownership before a plan's scope is checked.

This stage never queries the graph or guesses a missing biological constraint.
Only release-owned fields, grounded identities and recorded sample-assay values
can disambiguate a planner's ownerless field. Operators retain their meaning.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST
from .semantic_registry import ALIASES as ASSAY_ALIASES

VERSION = 'preplanning-property-owners-v2'
DIGEST = hashlib.sha256(Path(__file__).read_bytes() + SCHEMA_DIGEST.encode()).hexdigest()


def _canonical(value, choices):
    matches = [name for name in choices if isinstance(value, str) and name.casefold() == value.casefold()]
    return matches[0] if len(matches) == 1 else None


def _values(constraint):
    value = constraint.get('value')
    operator = str(constraint.get('operator', '=')).upper()
    if operator in {'IN', 'NOT IN'}:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return []
        return value if isinstance(value, list) else []
    return [value]


def _grounded_kind(values, grounding, expected=None, exact_id=False):
    kinds = []
    for value in values:
        candidates = set()
        for mention in grounding.get('mentions', []):
            if mention.get('state') != 'resolved' or mention.get('identity_complete') is False:
                continue
            items = mention.get('candidates', [])
            if len(items) != 1:
                continue
            candidate = items[0]
            kind = candidate.get('entity_type')
            forms = [candidate.get('id')]
            if not exact_id:
                forms.extend([candidate.get('name'), mention.get('requested')])
            if (not expected or kind == expected) and isinstance(value, str) and any(
                    isinstance(form, str) and form.casefold() == value.casefold() for form in forms):
                candidates.add(kind)
        if len(candidates) != 1:
            return None
        kinds.append(candidates.pop())
    return kinds[0] if kinds and len(set(kinds)) == 1 else None


def _sample_assay(values, grounding):
    import re
    recorded = grounding.get('sample_terminology', {}).get('modalities', [])
    canonical = []
    for value in values:
        if not isinstance(value, str):
            return None
        alias = ASSAY_ALIASES.get(re.sub('[^a-z0-9]', '', value.casefold()), value)
        match = _canonical(alias, recorded)
        if not match:
            return None
        canonical.append(match)
    return canonical or None


def _qtl_tissue(constraint, relations):
    """Resolve the generic field from the complete release category inventory."""
    if (relations != ['PART_OF_QTL_SIGNAL'] or constraint.get('property') != 'tissue'
            or constraint.get('entity_type') or constraint.get('owner_kind') == 'node'
            or constraint.get('relationship_type') not in {None, 'PART_OF_QTL_SIGNAL'}
            or constraint.get('operator', '=') not in {'=', '!=', '<>', 'IN', 'NOT IN'}):
        return None
    values = _values(constraint)
    matches = []
    for value in values:
        possible = [(prop, canonical) for prop in ('tissue_name', 'tissue_id')
                    if (canonical := _canonical(value, REGISTRY['categories'].get('PART_OF_QTL_SIGNAL.' + prop, [])))]
        if len(possible) != 1:
            return None
        matches.append(possible[0])
    if not matches or len({prop for prop, _ in matches}) != 1:
        return None
    canonical = [value for _, value in matches]
    if constraint.get('operator', '=') in {'IN', 'NOT IN'}:
        value = json.dumps(canonical) if isinstance(constraint.get('value'), str) else canonical
    else:
        value = canonical[0]
    return matches[0][0], value


def compile_property_owners(plan, grounding):
    """Return ``(compiled_copy, error_or_none)``; never mutate the input.

    The whole plan is rejected on ambiguous ownership. A selected relationship
    alone cannot prefer an edge property over a supported endpoint property.
    """
    result = deepcopy(plan)
    if (not isinstance(grounding, dict) or grounding.get('status') != 'ready'
            or grounding.get('identity', {}).get('graph_release') != REGISTRY['release']):
        return result, None
    for step in result.get('steps', []):
        relations = [_canonical(value, REGISTRY['relations']) or value for value in step.get('relation_types', [])]
        labels = {label for relation in relations for path in REGISTRY['relations'].get(relation, {}).get('paths', [])
                  for label in path['source'] + path['target']}
        sample_role = 'HAS_SAMPLE' in relations
        changes = step.setdefault('constraint_compilation', [])
        for index, constraint in enumerate(step.get('constraints', [])):
            before = deepcopy(constraint)
            prop = constraint.get('property')
            entity = constraint.get('entity_type')
            relation = constraint.get('relationship_type')
            owner_kind = constraint.get('owner_kind')
            if owner_kind not in {None, 'node', 'relationship'}:
                return result, f'unsupported_property_owner_kind:{step.get("id", "step")}:{prop}'
            # Explicit schema qualification is a typed owner, not a free-text alias.
            if isinstance(prop, str) and '.' in prop and not entity and not relation:
                prefix, field = prop.split('.', 1)
                node = _canonical(prefix, REGISTRY['nodes'])
                edge = _canonical(prefix, REGISTRY['relations'])
                if bool(node) != bool(edge):
                    entity, relation, prop = node, edge, field
            entity = _canonical(entity, REGISTRY['nodes']) or entity
            relation = _canonical(relation, REGISTRY['relations']) or relation
            if (entity and relation or entity and owner_kind == 'relationship'
                    or relation and owner_kind == 'node'):
                return result, f'conflicting_property_owners:{step.get("id", "step")}:{prop}'
            tissue = _qtl_tissue({**constraint, 'property': prop, 'entity_type': entity,
                                  'relationship_type': relation}, relations)
            if tissue:
                prop, constraint['value'] = tissue
                relation = 'PART_OF_QTL_SIGNAL'
            if entity:
                if prop not in REGISTRY['nodes'].get(entity, []):
                    return result, f'invalid_property_owner:{step.get("id", "step")}:{entity}.{prop}'
            elif relation:
                if relation not in relations or prop not in REGISTRY['relations'].get(relation, {}).get('properties', []):
                    return result, f'invalid_property_owner:{step.get("id", "step")}:{relation}.{prop}'
            else:
                values = _values(constraint)
                if prop == 'anatomical_structure' and sample_role and owner_kind != 'relationship':
                    entity = _grounded_kind(values, grounding, expected='anatomical_structure', exact_id=True)
                    if entity:
                        prop = 'id'
                elif prop == 'data_modality' and sample_role and owner_kind != 'relationship' and (assays := _sample_assay(values, grounding)):
                    entity = 'Sample_node'
                    if str(constraint.get('operator', '=')).upper() in {'IN', 'NOT IN'}:
                        constraint['value'] = json.dumps(assays) if isinstance(constraint.get('value'), str) else assays
                    else:
                        constraint['value'] = assays[0]
                elif prop in {'id', 'name'} and owner_kind != 'relationship':
                    entity = _grounded_kind(values, grounding, exact_id=prop == 'id')
                    if entity and entity not in labels:
                        entity = None
                if not entity:
                    owners = [('node', label) for label in sorted(labels) if prop in REGISTRY['nodes'].get(label, [])]
                    owners.extend(('relationship', kind) for kind in relations
                                  if prop in REGISTRY['relations'].get(kind, {}).get('properties', []))
                    if owner_kind:
                        owners = [owner for owner in owners if owner[0] == owner_kind]
                    if len(owners) != 1:
                        category = 'ambiguous_property_owner' if owners else 'unknown_property_owner'
                        names = ','.join(owner for _, owner in owners)
                        return result, f'{category}:{step.get("id", "step")}:{prop}:{names}'
                    owner_kind, owner = owners[0]
                    entity, relation = (owner, None) if owner_kind == 'node' else (None, owner)
            constraint.update(property=prop, entity_type=entity, owner_kind='node' if entity else 'relationship')
            if relation:
                constraint['relationship_type'] = relation
            else:
                constraint.pop('relationship_type', None)
            if constraint != before:
                changes.append({'constraint_index': index, 'requested': before,
                                'canonical_binding': deepcopy(constraint), 'version': VERSION,
                                'source': 'verified release ownership and resolved request role',
                                'schema_digest': SCHEMA_DIGEST})
        if not changes:
            step.pop('constraint_compilation', None)
    return result, None
