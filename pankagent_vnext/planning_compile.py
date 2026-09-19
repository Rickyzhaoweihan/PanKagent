"""Compile verified property ownership before a plan's scope is checked.

This stage never queries the graph or guesses a missing biological constraint.
Only release-owned fields, grounded identities and recorded sample-assay values
can disambiguate a planner's ownerless field. Operators retain their meaning.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST
from .semantic_registry import ALIASES as ASSAY_ALIASES, dataset_source_owner

VERSION = 'preplanning-property-owners-v5'
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


def _grounded_anatomy_ids(values, grounding):
    """Map complete, uniquely grounded identity spellings without guessing."""
    identifiers = []
    for value in values:
        matches = set()
        for mention in grounding.get('mentions', []):
            if mention.get('state') != 'resolved' or mention.get('identity_complete') is False:
                continue
            candidates = mention.get('candidates', [])
            if len(candidates) != 1:
                continue
            candidate = candidates[0]
            if candidate.get('entity_type') != 'anatomical_structure' or not candidate.get('id'):
                continue
            forms = [candidate['id'], candidate.get('name'), mention.get('requested')]
            if isinstance(value, str) and any(isinstance(form, str) and form.casefold() == value.casefold() for form in forms):
                matches.add(candidate['id'])
        if len(matches) != 1:
            return None
        identifiers.append(matches.pop())
    return identifiers or None


def _grounded_primary_gene_ids(constraint, question, grounding):
    """Normalize a requested gene's verified primary symbol, never an alias.

    This repairs a planner's choice of identity field. An explicit raw-property
    request, negative predicate, ambiguous or incomplete identity remains literal.
    The catalog must retain the actual release-read hgnc_symbol value; name and
    generic aliases do not prove that field's value.
    """
    if (grounding.get('catalog_complete') is not True
            or str(constraint.get('operator', '=')).upper() not in {'=', 'IN'}
            or not question or re.search(r'\bhgnc_symbol\b', question, re.I)):
        return None
    identifiers = []
    for value in _values(constraint):
        matches = set()
        for mention in grounding.get('mentions', []):
            candidates = mention.get('candidates', [])
            if (mention.get('state') != 'resolved' or mention.get('identity_complete') is False
                    or len(candidates) != 1):
                continue
            candidate = candidates[0]
            symbol = candidate.get('hgnc_symbol')
            if (candidate.get('entity_type') == 'Gene' and candidate.get('id')
                    and candidate.get('hgnc_symbol_unique') is True
                    and isinstance(value, str) and isinstance(symbol, str) and symbol
                    and value.casefold() == symbol.casefold()):
                matches.add(candidate['id'])
        if len(matches) != 1:
            return None
        identifiers.append(matches.pop())
    return identifiers or None


def _sample_tissue_identity(constraint, question, grounding):
    """Distinguish a requested tissue identity from a raw sample metadata value.

    Sample_node.anatomical_structure is a real metadata property, so schema
    ownership alone cannot repair a generated ontology-ID predicate on it.
    Only complete grounding plus the original request's sample-tissue role
    authorizes using the anatomical_structure endpoint instead.
    """
    if (not question or constraint.get('entity_type') != 'Sample_node'
            or constraint.get('property') != 'anatomical_structure'
            or str(constraint.get('operator', '=')).upper() not in {'=', '!=', '<>', 'IN', 'NOT IN'}):
        return None
    # An explicitly requested storage field remains a storage-field predicate,
    # even when its literal happens to resemble a verified ontology identifier.
    if (re.search(r'\bSample_node\s*\.\s*anatomical_structure\b', question, re.I)
            or re.search(r'\b(?:raw|metadata|property|field|column)\b[^.!?;\n]{0,60}\banatomical_structure\b', question, re.I)
            or re.search(r'\banatomical_structure\b[^.!?;\n]{0,35}\b(?:raw|metadata|property|field|column)\b', question, re.I)):
        return None
    identifiers = _grounded_anatomy_ids(_values(constraint), grounding)
    if not identifiers:
        return None
    for identifier in identifiers:
        role_verified = False
        for mention in grounding.get('mentions', []):
            if mention.get('state') != 'resolved' or mention.get('identity_complete') is False:
                continue
            candidates = mention.get('candidates', [])
            if len(candidates) != 1 or candidates[0].get('id') != identifier:
                continue
            candidate = candidates[0]
            if candidate.get('entity_type') != 'anatomical_structure':
                continue
            forms = [mention.get('requested'), candidate.get('name'), identifier]
            for clause in re.split(r'[.!?;\n]', question):
                if not any(isinstance(form, str) and form and re.search(
                        r'(?<!\w)' + re.escape(form) + r'(?!\w)', clause, re.I) for form in forms):
                    continue
                sample_word = re.search(r'\b(?:samples?|specimens?|biops(?:y|ies))\b', clause, re.I)
                donor_assay = (re.search(r'\bdonors?\b', clause, re.I)
                               and re.search(r'\b(?:[A-Za-z0-9]+[- ]?seq|multiome|multiomics|RNA|ATAC)\b', clause, re.I))
                if sample_word or donor_assay:
                    role_verified = True
                    break
            if role_verified:
                break
        if not role_verified:
            return None
    if str(constraint.get('operator', '=')).upper() in {'IN', 'NOT IN'}:
        return json.dumps(identifiers) if isinstance(constraint.get('value'), str) else identifiers
    return identifiers[0]


def _source_owner(constraint, question, grounding):
    values = _values(constraint)
    recorded = grounding.get('sample_terminology', {}).get('sources', [])
    owners = {dataset_source_owner(question, value) for value in values if _canonical(value, recorded)}
    if (not values or len(owners) != 1 or None in owners
            or not all(_canonical(value, recorded) for value in values)):
        return None
    return owners.pop()


def _annotation_source(constraint, question, grounding, relations):
    if (relations != ['FUNCTION_ANNOTATION'] or not question
            or str(constraint.get('operator', '=')).upper() not in {'=', 'IN'}):
        return None
    available = grounding.get('schema', {}).get('categories', {}).get('FUNCTION_ANNOTATION.data_source', [])
    values = _values(constraint)
    canonical = [_canonical(value, available) for value in values]
    if not canonical or not all(canonical):
        return None
    # Only an explicitly named recorded pathway resource authorizes ownership.
    # The later scope compiler still checks which gene belongs to each source.
    resource_labels = {label.casefold() for path in REGISTRY['relations']['FUNCTION_ANNOTATION']['paths'] for label in path['target']}
    if not all(value.casefold() in resource_labels and re.search(r'(?<!\w)' + re.escape(value) + r'(?!\w)', question, re.I)
               for value in canonical):
        return None
    return (json.dumps(canonical) if isinstance(constraint.get('value'), str) else canonical) if str(constraint.get('operator', '=')).upper() == 'IN' else canonical[0]


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


def compile_property_owners(plan, grounding, *, question=None):
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
            if sample_role and not relation and owner_kind != 'relationship':
                sample_tissue = _sample_tissue_identity(
                    {**constraint, 'entity_type': entity, 'property': prop}, question, grounding)
                if sample_tissue is not None:
                    entity, prop = 'anatomical_structure', 'id'
                    constraint['value'] = sample_tissue
            # The model's explicit owner cannot override a verified role in
            # the raw user request. A donor-cohort source and a sample provider
            # are different fields even when both are named data_source.
            if prop == 'data_source' and sample_role and not relation and entity in {None, 'donor', 'Sample_node'} and question:
                source_owner = _source_owner(constraint, question, grounding)
                if source_owner:
                    entity = source_owner
                    canonical_sources = [_canonical(value, grounding['sample_terminology']['sources']) for value in _values(constraint)]
                    constraint['value'] = ((json.dumps(canonical_sources) if isinstance(constraint.get('value'), str) else canonical_sources)
                                           if str(constraint.get('operator', '=')).upper() in {'IN', 'NOT IN'} else canonical_sources[0])
            if prop == 'data_source' and not entity and not relation and owner_kind != 'node':
                annotation_source = _annotation_source(constraint, question, grounding, relations)
                if annotation_source is not None:
                    relation = 'FUNCTION_ANNOTATION'
                    constraint['value'] = annotation_source
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
                if (prop == 'anatomical_structure' and sample_role and owner_kind != 'relationship'
                        and str(constraint.get('operator', '=')).upper() in {'=', '!=', '<>', 'IN', 'NOT IN'}):
                    identifiers = _grounded_anatomy_ids(values, grounding)
                    if identifiers:
                        entity = 'anatomical_structure'
                        prop = 'id'
                        constraint['value'] = (json.dumps(identifiers) if isinstance(constraint.get('value'), str) else identifiers) if str(constraint.get('operator', '=')).upper() in {'IN', 'NOT IN'} else identifiers[0]
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
            if entity == 'Gene' and prop == 'hgnc_symbol':
                identifiers = _grounded_primary_gene_ids(constraint, question, grounding)
                if identifiers:
                    prop = 'id'
                    constraint['value'] = ((json.dumps(identifiers) if isinstance(constraint.get('value'), str) else identifiers)
                                           if str(constraint.get('operator', '=')).upper() == 'IN' else identifiers[0])
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
