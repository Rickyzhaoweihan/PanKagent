"""Validate anatomical endpoint roles using a complete, release-specific inventory.

HAS_CELL_TYPE is stored cell -> tissue although both endpoints have the same
Neo4j label. Marker annotation belongs to a cell, not its enclosing tissue.
IDs are matched exactly; CL/UBERON prefixes are never role rules.
The inventory is schema metadata, not a fallback query or a biological answer.
"""
import hashlib
import json
from pathlib import Path

_RAW = Path(__file__).with_name('anatomy_roles.json').read_bytes()
REGISTRY = json.loads(_RAW)
ROLES = REGISTRY['roles']
ENDPOINT_ROLES = REGISTRY['endpoint_roles']
DIGEST = hashlib.sha256(_RAW + Path(__file__).read_bytes()).hexdigest()


def endpoint_role_errors(tokens, step, parameters):
    """Check a single UNION branch without changing its Cypher or constraints."""
    from .graph import _constraint_choices, _pattern_bindings, _predicate_present

    if not any(t.value in ENDPOINT_ROLES and t.kind in ('WORD', 'IDENT') for t in tokens):
        return []
    # Optional evidence must have correct endpoints too. This token view does
    # not let optional predicates satisfy mandatory scientific constraints.
    structural = [t for t in tokens if not (t.kind == 'WORD' and t.value.upper() == 'OPTIONAL')]
    undirected = []
    nodes, paths = _pattern_bindings(structural, undirected_patterns=undirected)
    groups = []
    resolved = {e.get('constraint_index'): e for e in step.get('resolved_entities', [])
                if e.get('state') == 'resolved' and e.get('entity_type') == 'anatomical_structure'}
    for index, constraint in enumerate(step.get('constraints', [])):
        canonical = resolved.get(index, {}).get('id')
        if canonical in ROLES:
            groups.append((_constraint_choices(step, index, constraint), {ROLES[canonical]}))
    # Direct IDs and parameter sets also cover dependency-bound and optional
    # endpoints. Only exact IDs in the inspected release acquire a role.
    values = [t.value for t in structural if t.kind == 'STRING' and t.value in ROLES]
    values += [v for v in parameters.values() if isinstance(v, str) and v in ROLES]
    for value in set(values):
        groups.append(([{'property': 'id', 'operator': '=', 'value': value}], {ROLES[value]}))
    for value in parameters.values():
        if isinstance(value, list) and value and all(isinstance(v, str) and v in ROLES for v in value):
            groups.append(([{'property': 'id', 'operator': 'IN', 'value': value}], {ROLES[v] for v in value}))
    for role in set(ROLES.values()):
        groups.append(([{'property': 'category', 'operator': '=', 'value': role}], {role}))
    roles = {variable: set() for variable in nodes}
    for variable in nodes:
        for predicates, categories in groups:
            if any(_predicate_present(structural, predicate, parameters, {variable}) for predicate in predicates):
                roles[variable].update(categories)
    aliases = [(structural[i-1].value, structural[i+1].value)
               for i, token in enumerate(structural[1:-1], 1)
               if token.kind == 'WORD' and token.value.upper() == 'AS'
               and structural[i-1].value in nodes and structural[i+1].value in nodes
               and (i < 2 or structural[i-2].value != '.')]
    for _ in range(len(aliases) + 1):
        for old, new in aliases:
            shared = roles[old] | roles[new]
            roles[old].update(shared)
            roles[new].update(shared)

    def compatible(source, target, spec):
        def permits(variable, allowed):
            return allowed is None or not roles[variable] or bool(roles[variable] & set(allowed))
        return permits(source, spec['source_categories']) and permits(target, spec['target_categories'])

    errors = []
    for source, target, kinds in paths:
        for kind in kinds & ENDPOINT_ROLES.keys():
            spec = ENDPOINT_ROLES[kind]
            if not compatible(source, target, spec) and not ((source, target, kinds) in undirected and compatible(target, source, spec)):
                errors.append('invalid_anatomical_endpoint_role:' + kind + '_requires_' + spec['description'])
    return sorted(set(errors))
