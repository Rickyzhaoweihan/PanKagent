"""Reject unrequested existence restrictions on donor/sample result sets.

This guard never rewrites a query. A requested HAS_DONOR primary relation may
retain its disease endpoint for compatibility; stage metadata does not authorize
an additional disease-to-sample membership requirement.
"""
import hashlib
import re
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


def _requested_output_variables(tokens, step, bindings, paths, allowed, aliases, choices, parameters):
    """Authorize an explicitly requested, projected endpoint without an ID filter.

    Request roles are server-owned; label vocabulary and connectivity come from
    the schema. Merely mentioning a tissue in background/property text does not
    authorize an existence restriction on the requested sample population.
    """
    from .agent_schemas import module
    from .semantic_decision import _valid_decision, scope_text
    from .semantic_registry import _trusted_request, identity_authorization_text, _negated_at
    from .scientific_projection import View, _expr, _split, _strip_alias
    from .graph import _predicate_present
    raw, trusted = _trusted_request(step)
    if not trusted:
        return set()
    active = identity_authorization_text(scope_text(step, raw))
    # This exception is for the requested endpoint population, not optional
    # annotations on some other primary population (which must keep its rows).
    head = re.match(r'\s*(?:please\s+)?(?:briefly\s+)?(?:what|which|list|show|find|get|describe)\s+', active, re.I)
    if not head:
        return set()
    tail = active[head.end():]
    boundary = re.search(r'\b(?:have|has|are|is|do|does|from|for|that|with|where|and)\b|[.!?;]', tail, re.I)
    subject = tail[:boundary.start()] if boundary else tail
    if re.search(r'\b(?:annotations?|metadata|properties|fields)\b', subject, re.I):
        return set()
    if re.search(r'\b(?:do|does)\s+not\s+have\b|\bwithout\b', tail, re.I):
        return set()
    roles = _valid_decision(step, raw).get('request_phrase_roles', [])
    phrases = [item['text'] for item in roles if item['role'] == 'output'
               and active[item['start']:item['end']] == item['text']
               and not _negated_at(raw, item['start'], item['end'])]
    requested = {label for label, spec in module('database_schema')['nodes'].items()
                 if isinstance(spec.get('query_terms'), str) and spec['query_terms']
                 and re.search(spec['query_terms'], subject, re.I)
                 and any(re.search(spec['query_terms'], text, re.I) for text in phrases)}
    if not requested:
        return set()
    # Reuse the existing lossless projection parser, including WITH/collect
    # aliases. A count or an incidental predicate mention is not an identity.
    symbols = {name: View(nodes={name}, identities={name}) for name in bindings}
    projected = set()
    for kind, _, part in _clauses(tokens):
        if kind not in {'WITH', 'RETURN'}:
            continue
        output, next_symbols = View(), {}
        for item in _split(part[1:]):
            expression, alias = _strip_alias(item)
            value = _expr(expression, symbols, set(), {})
            output = output.merge(value)
            if alias:
                next_symbols[alias] = value
            elif len(expression) == 1 and expression[0].value in symbols:
                next_symbols[expression[0].value] = value
        if kind == 'WITH':
            symbols = next_symbols
        else:
            projected = output.nodes | output.identities
    result = set()
    for variable in projected & set(bindings):
        if not bindings[variable] or not bindings[variable] <= requested:
            continue
        if not any((a == variable and bindings.get(b, set()) & allowed
                    or b == variable and bindings.get(a, set()) & allowed)
                   for a, b, _ in paths):
            continue
        component = {variable}
        while True:
            linked = {b for a, b, _ in paths if a in component} | {a for a, b, _ in paths if b in component}
            if linked <= component:
                break
            component |= linked
        if step.get('semantic_registry', {}).get('donor_required') and not any('donor' in bindings.get(v, set()) for v in component):
            continue
        if any(c.get('entity_type') and not any(
                c['entity_type'] in bindings.get(v, set()) and any(
                    _predicate_present(tokens, choice, parameters, aliases.get(v, {v})) for choice in group)
                for v in component)
               for c, group in zip(step.get('constraints', []), choices)):
            continue
        result.add(variable)
    return result


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
    requested_outputs = _requested_output_variables(inspected, step, bindings, paths,
                                                    allowed, aliases, choices, parameters)
    errors = _optional_filter_errors(inspected)
    for variable, labels in bindings.items():
        primary_labels = labels - {'ontology', 'provenance'}
        if (not primary_labels or not primary_labels <= allowed) and variable not in requested_outputs:
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
