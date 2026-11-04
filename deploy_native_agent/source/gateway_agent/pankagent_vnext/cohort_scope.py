"""Reject unrequested existence restrictions on donor/sample result sets.

This guard never rewrites a query. A requested HAS_DONOR primary relation may
retain its disease endpoint for compatibility; stage metadata does not authorize
an additional disease-to-sample membership requirement.
"""
import hashlib
from pathlib import Path

VERSION = 'cohort-existence-scope-v1'
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_CLAUSES = {'MATCH', 'WHERE', 'WITH', 'RETURN', 'UNWIND', 'ORDER', 'LIMIT', 'SKIP'}


def _clauses(tokens):
    starts, depth = [], 0
    for index, token in enumerate(tokens):
        word = token.value.upper() if token.kind == 'WORD' else ''
        if not depth and word in _CLAUSES:
            optional = word == 'MATCH' and index and tokens[index - 1].value.upper() == 'OPTIONAL'
            starts.append((index - 1 if optional else index, word, bool(optional)))
        if token.value in {'(', '[', '{'}:
            depth += 1
        elif token.value in {')', ']', '}'}:
            depth = max(0, depth - 1)
    return [(kind, optional, tokens[start:(starts[i + 1][0] if i + 1 < len(starts) else len(tokens))])
            for i, (start, kind, optional) in enumerate(starts)]


def _name_anonymous_patterns(tokens):
    """Assign local inspection names; leave the actual query untouched."""
    from .graph import Token
    result, depth, matching, serial = [], 0, False, 0
    occupied = {token.value for token in tokens}
    for index, token in enumerate(tokens):
        if not depth and token.kind == 'WORD' and token.value.upper() in _CLAUSES:
            matching = token.value.upper() == 'MATCH'
        result.append(token)
        if (matching and not depth and token.value == '(' and index + 1 < len(tokens)
                and tokens[index + 1].value in {':', ')', '{'}):
            name = '__cohort_anonymous_' + str(serial)
            while name in occupied:
                serial += 1
                name = '__cohort_anonymous_' + str(serial)
            serial += 1
            result.append(Token('WORD', name))
        if token.value in {'(', '[', '{'}:
            depth += 1
        elif token.value in {')', ']', '}'}:
            depth = max(0, depth - 1)
    return result


def _optional_filter_errors(tokens):
    from .graph import _pattern_bindings
    from .release_schema import relationship_bindings
    clauses = _clauses(tokens)
    declared, tainted, errors, optional_where = set(), set(), [], False
    for kind, optional, part in clauses:
        refs = {token.value for token in part if token.kind in {'WORD', 'IDENT'}}
        if kind == 'MATCH' and optional:
            nodes, _ = _pattern_bindings(part[1:])  # remove OPTIONAL for inspection
            tainted.update((set(nodes) | set(relationship_bindings(part))) - declared)
            optional_where = True
            continue
        if kind in {'MATCH', 'UNWIND'} or kind == 'WHERE' and not optional_where:
            if refs & tainted:
                errors.append('filtering_optional_cohort_context')
        if kind == 'MATCH':
            nodes, _ = _pattern_bindings(part)
            declared.update(nodes)
            declared.update(relationship_bindings(part))
        if kind == 'WITH':
            # Track optional-derived counts/collections through ordinary aliases.
            # UNWIND or a later WHERE on them can otherwise remove primary rows.
            start, depth = 1, 0
            for index, token in enumerate(part):
                if token.value in {'(', '[', '{'}:
                    depth += 1
                elif token.value in {')', ']', '}'}:
                    depth = max(0, depth - 1)
                if not depth and token.value == ',':
                    start = index + 1
                if not depth and token.kind == 'WORD' and token.value.upper() == 'AS' and index + 1 < len(part):
                    if any(t.value in tainted for t in part[start:index] if t.kind in {'WORD', 'IDENT'}):
                        tainted.add(part[index + 1].value)
                    else:
                        declared.add(part[index + 1].value)
        if kind != 'WHERE':
            optional_where = False
    return errors


def validation_errors(tokens, step, parameters, choices):
    from .graph import _pattern_bindings, _predicate_present
    semantics = step.get('semantic_registry') or {}
    relations = set(step.get('relation_types') or [])
    # Legacy untyped plans and other scientific families retain their existing
    # validators; this contract applies to compiled donor/sample investigations.
    if not semantics or not relations or not relations <= {'HAS_DONOR', 'HAS_SAMPLE'}:
        return []
    inspected = _name_anonymous_patterns(tokens)
    bindings, paths = _pattern_bindings(inspected, graph_release=step.get('graph_version'))
    aliases = {name: {name} for name in bindings}
    for index, token in enumerate(inspected[1:-1], 1):
        if (token.kind == 'WORD' and token.value.upper() == 'AS'
                and inspected[index - 1].value in aliases and inspected[index + 1].value in aliases
                and (index < 2 or inspected[index - 2].value != '.')):
            group = aliases[inspected[index - 1].value] | aliases[inspected[index + 1].value]
            for name in group:
                aliases[name] = group
    constraints = step.get('constraints') or []
    owners = {c.get('entity_type') for c in constraints}
    requirements = step.get('sample_requirements') or {}
    donor_required = semantics.get('donor_required', True)
    modality_requested = bool(requirements.get('modality_groups') or requirements.get('excluded_modality_constraints'))
    allowed = {'Sample_node'} if 'HAS_SAMPLE' in relations else set()
    if donor_required:
        allowed.add('donor')
    if 'anatomical_structure' in owners:
        allowed.add('anatomical_structure')
    if 'disease' in owners or 'HAS_DONOR' in relations:
        allowed.add('disease')
    if semantics.get('modality_links_verified') and modality_requested:
        allowed.add('data_modality')
    errors = _optional_filter_errors(inspected)
    for variable, labels in bindings.items():
        primary_labels = labels - {'ontology', 'provenance'}
        if not primary_labels or not primary_labels <= allowed:
            errors.append('unrequested_mandatory_cohort_owner:' + ','.join(sorted(primary_labels or {'unverified'})))
    for source, target, kinds in paths:
        if kinds - (relations | ({'HAS_DONOR'} if 'disease' in owners else set())):
            errors.append('unrequested_mandatory_cohort_relation:' + ','.join(sorted(kinds - relations)))
        if 'HAS_SAMPLE' not in kinds:
            continue
        labels = bindings.get(source, set())
        if 'disease' in labels and 'disease' not in owners:
            errors.append('unrequested_mandatory_sample_source:disease')
        for label in {'donor', 'anatomical_structure', 'disease'} & labels:
            scoped = [group for c, group in zip(constraints, choices) if c.get('entity_type') == label]
            if scoped and not all(any(_predicate_present(inspected, c, parameters, aliases.get(source, {source})) for c in group) for group in scoped):
                errors.append('unrequested_mandatory_sample_source:' + label)
        if 'data_modality' in labels:
            wanted = [{'property': 'id', 'operator': 'IN' if len(group) > 1 else '=',
                       'value': group if len(group) > 1 else group[0]}
                      for group in requirements.get('modality_groups', []) if group]
            wanted.extend({**c, 'property': 'id'} for c in requirements.get('excluded_modality_constraints', []))
            if not (semantics.get('modality_links_verified') and wanted
                    and any(_predicate_present(inspected, c, parameters, aliases.get(source, {source})) for c in wanted)):
                errors.append('unrequested_mandatory_sample_source:data_modality')
    return list(dict.fromkeys(errors))
