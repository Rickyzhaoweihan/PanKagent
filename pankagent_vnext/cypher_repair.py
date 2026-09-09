"""Conservative, auditable schema repair of generated read-only Cypher.

This is a candidate producer, never an approval or query executor. A repaired
candidate must still pass the full request validator and Neo4j EXPLAIN. It does
not alter literals, parameter values, filters, result projections, or limits.
Direction changes require both bound endpoint labels and exactly one compatible
orientation in this release. No endpoint type is inferred to justify a repair.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import time

from .release_schema import REGISTRY, DIGEST as REGISTRY_DIGEST

VERSION = 'schema-candidate-repair-2'
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_IDENTIFIER = {'WORD', 'IDENT'}
_CLAUSES = {'MATCH', 'WHERE', 'WITH', 'RETURN', 'ORDER', 'SKIP', 'LIMIT', 'UNION'}
_UNSUPPORTED = {'CALL', 'CREATE', 'MERGE', 'SET', 'DELETE', 'REMOVE', 'DROP',
                'LOAD', 'FOREACH', 'UNWIND', 'YIELD', 'LET', 'FINISH', 'INSERT',
                'DETACH', 'EXISTS'}


class _Unsupported(ValueError):
    pass


@dataclass(frozen=True)
class _Binding:
    kind: str
    names: frozenset


@dataclass
class _Node:
    variable: str | None
    labels: set
    start: int
    end: int
    property_tokens: list


@dataclass
class _Relationship:
    variable: str | None
    kinds: set
    start: int
    end: int
    left_arrow: int | None
    right_arrow: int | None
    left_dash: int
    right_dash: int
    property_tokens: list


def _word(token, word):
    return token.kind == 'WORD' and token.value.upper() == word


def _identifier(value):
    return '`' + value.replace('`', '``') + '`'


def _canonical(value, names):
    # Case is only a documented spelling variant when the release contains a
    # unique target. Do not turn a near spelling or a general parent into it.
    matches = [name for name in names if name.casefold() == value.casefold()]
    return matches[0] if len(matches) == 1 else value


def _depths(tokens):
    stack, depths = [], []
    pairs = {')': '(', ']': '[', '}': '{'}
    for token in tokens:
        depths.append(len(stack))
        if token.kind != 'SYMBOL':
            continue
        if token.value in ('(', '[', '{'):
            stack.append(token.value)
        elif token.value in pairs:
            if not stack or stack.pop() != pairs[token.value]:
                raise _Unsupported('unbalanced_pattern')
    if stack:
        raise _Unsupported('unbalanced_pattern')
    return depths


def _closed(tokens, start):
    opener = tokens[start].value
    closer = {'(': ')', '[': ']', '{': '}'}[opener]
    depth = 0
    for i in range(start, len(tokens)):
        if tokens[i].kind != 'SYMBOL':
            continue
        if tokens[i].value == opener:
            depth += 1
        elif tokens[i].value == closer:
            depth -= 1
            if depth == 0:
                return i
    raise _Unsupported('unbalanced_pattern')


def _declaration(tokens, start, names, symbol_kind, edits, notes):
    """Parse only variable[:Label[:Label...]][{literal property map}]."""
    end = _closed(tokens, start)
    at, variable, labels, properties = start + 1, None, set(), []
    if at < end and tokens[at].kind in _IDENTIFIER:
        variable = tokens[at].value
        at += 1
    while at < end and tokens[at].value in (':', '|'):
        if tokens[at].value == '|' and symbol_kind != 'relationship':
            raise _Unsupported('label_expression')
        at += 1
        if at < end and tokens[at].value == ':' and symbol_kind == 'relationship':
            at += 1
        if at >= end or tokens[at].kind not in _IDENTIFIER:
            raise _Unsupported('dynamic_schema_symbol')
        token = tokens[at]
        canonical = _canonical(token.value, names)
        labels.add(canonical)
        if canonical != token.value:
            _edit(edits, notes, token, _identifier(canonical),
                  'schema_case', symbol_kind=symbol_kind, canonical=canonical)
        at += 1
    if at < end and tokens[at].value == '{':
        map_end = _closed(tokens, at)
        if map_end >= end:
            raise _Unsupported('invalid_property_map')
        properties = tokens[at + 1:map_end]
        at = map_end + 1
    if at != end:
        # Variable-length relationships, inline WHERE, dynamic labels and
        # property-parameter maps need broader parsing; leave them untouched.
        raise _Unsupported('unsupported_pattern_shape')
    return variable, labels, end, properties


def _edit(edits, notes, token, replacement, kind, **extra):
    key = (token.start, token.end)
    existing = edits.get(key)
    if existing is not None and existing != replacement:
        raise _Unsupported('conflicting_repair')
    if existing is None:
        edits[key] = replacement
        notes.append({'kind': kind, 'start': token.start, 'end': token.end,
                      'original': token.value, 'replacement': replacement, **extra})


def _bind(scope, variable, kind, names):
    if variable is None:
        return
    old = scope.get(variable)
    if old and old.kind != kind:
        raise _Unsupported('conflicting_variable_binding')
    scope[variable] = _Binding(kind, frozenset(names) | (old.names if old else frozenset()))


def _properties(binding):
    if not binding or not binding.names:
        return set()
    owners = []
    for name in binding.names:
        props = (REGISTRY['nodes'].get(name) if binding.kind == 'node' else
                 REGISTRY['relations'].get(name, {}).get('properties'))
        if props is None:
            return set()
        owners.append(set(props))
    return set.intersection(*owners) if owners else set()


def _property_tokens(tokens, binding, edits, notes):
    """Only direct keys of the node/relationship pattern property map."""
    depths = _depths(tokens)
    allowed = _properties(binding)
    for i, token in enumerate(tokens[:-1]):
        if depths[i] == 0 and token.kind in _IDENTIFIER and tokens[i + 1].value == ':' and (
                i == 0 or tokens[i - 1].value == ','):
            canonical = _canonical(token.value, allowed)
            if canonical != token.value:
                _edit(edits, notes, token, _identifier(canonical), 'property_case',
                      owner_kind=binding.kind, owners=sorted(binding.names), canonical=canonical)


def _dot_properties(tokens, scope, edits, notes):
    for i in range(2, len(tokens)):
        if tokens[i - 1].value != '.' or tokens[i].kind not in _IDENTIFIER:
            continue
        owner = tokens[i - 2]
        if owner.kind not in _IDENTIFIER or (i >= 3 and tokens[i - 3].value == '.'):
            continue
        binding = scope.get(owner.value)
        canonical = _canonical(tokens[i].value, _properties(binding))
        if canonical != tokens[i].value:
            _edit(edits, notes, tokens[i], _identifier(canonical), 'property_case',
                  variable=owner.value, owner_kind=binding.kind,
                  owners=sorted(binding.names), canonical=canonical)


def _fits(kinds, source, target):
    if not kinds or not source or not target:
        return False
    return all(any(source <= set(path['source']) and target <= set(path['target'])
                   for path in REGISTRY['relations'].get(kind, {}).get('paths', []))
               for kind in kinds)


def _direction(tokens, left, relation, right, scope, edits, notes):
    if relation.left_arrow is None and relation.right_arrow is None:
        return
    if relation.left_arrow is not None and relation.right_arrow is not None:
        raise _Unsupported('bidirectional_arrow')
    # Alternative relationship types and self loops are intentionally left to
    # the validator. A reverse pattern must not combine unrelated directions.
    if len(relation.kinds) != 1 or left.variable == right.variable and left.variable is not None:
        return
    left_labels = left.labels or set(scope.get(left.variable, _Binding('node', frozenset())).names)
    right_labels = right.labels or set(scope.get(right.variable, _Binding('node', frozenset())).names)
    if not left_labels or not right_labels:
        return
    current = _fits(relation.kinds, right_labels, left_labels) if relation.left_arrow is not None else _fits(relation.kinds, left_labels, right_labels)
    opposite = _fits(relation.kinds, left_labels, right_labels) if relation.left_arrow is not None else _fits(relation.kinds, right_labels, left_labels)
    if current or not opposite:
        return
    metadata = {'relationship': next(iter(relation.kinds)),
                'left_labels': sorted(left_labels), 'right_labels': sorted(right_labels),
                'proof': 'only_reversed_orientation_matches_verified_endpoints'}
    if relation.left_arrow is not None:
        _edit(edits, notes, tokens[relation.left_arrow], '', 'unique_direction', **metadata)
        _edit(edits, notes, tokens[relation.right_dash], '->', 'unique_direction', **metadata)
    else:
        _edit(edits, notes, tokens[relation.left_dash], '<-', 'unique_direction', **metadata)
        _edit(edits, notes, tokens[relation.right_arrow], '', 'unique_direction', **metadata)


def _match(tokens, scope, edits, notes):
    nodes, chains, declarations = [], [], []
    at = 0
    while at < len(tokens):
        # A named path is safe: the repair does not rename or reorder nodes.
        if at + 1 < len(tokens) and tokens[at].kind in _IDENTIFIER and tokens[at + 1].value == '=':
            at += 2
        if at >= len(tokens) or tokens[at].value != '(':
            raise _Unsupported('unsupported_match_expression')
        variable, labels, end, props = _declaration(tokens, at, REGISTRY['nodes'], 'node', edits, notes)
        left = _Node(variable, labels, at, end, props)
        nodes.append(left)
        _bind(scope, variable, 'node', labels)
        at = end + 1
        while at < len(tokens) and tokens[at].value in ('-', '<'):
            left_arrow = at if tokens[at].value == '<' else None
            if left_arrow is not None:
                at += 1
            if at >= len(tokens) or tokens[at].value != '-':
                raise _Unsupported('invalid_arrow')
            left_dash, at = at, at + 1
            if at >= len(tokens) or tokens[at].value != '[':
                raise _Unsupported('untyped_relationship')
            rel_start = at
            variable, kinds, end, props = _declaration(tokens, at, REGISTRY['relations'], 'relationship', edits, notes)
            _bind(scope, variable, 'relationship', kinds)
            at = end + 1
            if at >= len(tokens) or tokens[at].value != '-':
                raise _Unsupported('invalid_arrow')
            right_dash, at = at, at + 1
            right_arrow = at if at < len(tokens) and tokens[at].value == '>' else None
            if right_arrow is not None:
                at += 1
            relation = _Relationship(variable, kinds, rel_start, end, left_arrow, right_arrow,
                                     left_dash, right_dash, props)
            declarations.append(relation)
            if at >= len(tokens) or tokens[at].value != '(':
                raise _Unsupported('missing_endpoint')
            variable, labels, end, props = _declaration(tokens, at, REGISTRY['nodes'], 'node', edits, notes)
            right = _Node(variable, labels, at, end, props)
            nodes.append(right)
            _bind(scope, variable, 'node', labels)
            chains.append((left, relation, right))
            left, at = right, end + 1
        if at < len(tokens):
            if tokens[at].value != ',':
                raise _Unsupported('unsupported_match_expression')
            at += 1
    for node in nodes:
        binding = scope.get(node.variable, _Binding('node', frozenset(node.labels)))
        _property_tokens(node.property_tokens, binding, edits, notes)
    for relation in declarations:
        binding = scope.get(relation.variable, _Binding('relationship', frozenset(relation.kinds)))
        _property_tokens(relation.property_tokens, binding, edits, notes)
    for left, relation, right in chains:
        _direction(tokens, left, relation, right, scope, edits, notes)
    _dot_properties(tokens, scope, edits, notes)


def _projection_scope(tokens, scope):
    if tokens and _word(tokens[0], 'DISTINCT'):
        tokens = tokens[1:]
    depths = _depths(tokens)
    pieces, start, result = [], 0, {}
    for i, token in enumerate(tokens):
        if depths[i] == 0 and token.value == ',':
            pieces.append(tokens[start:i])
            start = i + 1
    pieces.append(tokens[start:])
    for piece in pieces:
        if len(piece) == 1 and piece[0].value == '*':
            result.update(scope)
        elif len(piece) == 1 and piece[0].kind in _IDENTIFIER and piece[0].value in scope:
            result[piece[0].value] = scope[piece[0].value]
        elif len(piece) == 3 and piece[0].kind in _IDENTIFIER and _word(piece[1], 'AS') and piece[2].kind in _IDENTIFIER:
            if piece[0].value in scope:
                result[piece[2].value] = scope[piece[0].value]
        # A scalar alias deliberately carries no node/relationship binding.
    return result


def _repair(tokens, edits, notes):
    depths = _depths(tokens)
    for i, token in enumerate(tokens):
        if token.kind == 'SYMBOL' and token.value == '!=':
            # Neo4j requires <> for inequality. Lexer offsets exclude strings,
            # backtick identifiers and comments; this changes only equivalent
            # comparison spelling, never values or predicate ownership.
            _edit(edits, notes, token, '<>', 'operator_spelling',
                  proof='equivalent_not_equal_operator', canonical='<>')
        if token.kind == 'WORD' and token.value.upper() in _UNSUPPORTED and not (
                i and tokens[i - 1].value in ('.', ':')):
            raise _Unsupported('unsupported_scope_construct')
        if token.value == '|' and depths[i] and not any(
                t.value == ':' for t in tokens[max(0, i - 2):i]):
            # A direct alternative relationship is parsed below. Comprehension
            # scopes must never inherit an outer node's property ownership.
            raise _Unsupported('unsupported_comprehension_or_alternative')
    boundaries = [i for i, token in enumerate(tokens) if depths[i] == 0 and
                  token.kind == 'WORD' and token.value.upper() in _CLAUSES]
    if not boundaries or not _word(tokens[boundaries[0]], 'MATCH'):
        raise _Unsupported('unsupported_query_start')
    scope = {}
    for position, start in enumerate(boundaries):
        stop = boundaries[position + 1] if position + 1 < len(boundaries) else len(tokens)
        body = tokens[start + 1:stop]
        # OPTIONAL belongs to the following MATCH rather than the current body.
        if body and _word(body[-1], 'OPTIONAL'):
            body = body[:-1]
        if body and body[-1].value == ';':
            body = body[:-1]
        clause = tokens[start].value.upper()
        if clause == 'UNION':
            scope = {}
        elif clause == 'MATCH':
            _match(body, scope, edits, notes)
        else:
            _dot_properties(body, scope, edits, notes)
            if clause in ('WITH', 'RETURN'):
                scope = _projection_scope(body, scope)


def repair_candidate(query, *, graph_release):
    """Return an unapproved candidate and a protected-storage audit record.

    No network calls. ``query`` and ``original_query`` can contain user content;
    persist this record only in protected session storage, never operational logs.
    Any unsupported syntax causes an unchanged result, including earlier edits.
    """
    started = time.perf_counter()
    result = {'query': query, 'original_query': query, 'changed': False,
              'transformations': [], 'skipped': [], 'version': VERSION,
              'implementation_sha256': DIGEST, 'registry_sha256': REGISTRY_DIGEST,
              'graph_release': graph_release, 'requires_validation': True}
    if graph_release != REGISTRY['release']:
        result['skipped'] = ['graph_release_mismatch']
    elif not isinstance(query, str) or not query.strip():
        result['skipped'] = ['empty_candidate']
    else:
        # Lazy import avoids a graph -> repair -> graph initialization cycle.
        from .graph import tokenize, GraphValidationError
        edits, notes = {}, []
        try:
            tokens = tokenize(query)
            _repair(tokens, edits, notes)
            ordered = sorted(edits)
            if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
                raise _Unsupported('overlapping_repairs')
            revised = query
            for start, end in sorted(edits, reverse=True):
                revised = revised[:start] + edits[(start, end)] + revised[end:]
            result.update(query=revised, changed=revised != query, transformations=notes)
        except (GraphValidationError, _Unsupported) as exc:
            result['skipped'] = [str(exc)]
    if isinstance(query, str):
        result['original_sha256'] = hashlib.sha256(query.encode()).hexdigest()
        result['candidate_sha256'] = hashlib.sha256(result['query'].encode()).hexdigest()
    result['latency_ms'] = round((time.perf_counter() - started) * 1000, 3)
    return result


def failure_categories(errors):
    """Stable coarse classes; preserve the precise original reasons separately."""
    categories = set()
    for error in errors:
        reason = str(error).casefold()
        if any(x in reason for x in ('syntax', 'unterminated', 'unbalanced', 'invalid_arrow', 'neo.clienterror.statement.syntaxerror')):
            category = 'syntax'
        elif any(x in reason for x in ('invalid_relationship_endpoints', 'direction', 'endpoint_type')):
            category = 'direction_or_endpoints'
        elif any(x in reason for x in ('unknown_label', 'unknown_node_label', 'unrecognized_label')):
            category = 'label'
        elif any(x in reason for x in ('property', 'unknown_identifier', 'dependency_owner_mismatch')):
            category = 'property_or_binding'
        elif any(x in reason for x in ('constraint', 'filter', 'cohort', 'predicate', 'unrequested', 'overspecified')):
            category = 'filter_or_scope'
        elif any(x in reason for x in ('incomplete', 'truncat', 'projection', 'coverage', 'limit', 'pagination', 'scalar')):
            category = 'incomplete_evidence'
        elif 'unknown_relationship' in reason:
            category = 'relationship_type'
        elif any(x in reason for x in ('timeout', 'unavailable', 'connect', 'deadline', 'http')):
            category = 'infrastructure'
        elif 'release' in reason:
            category = 'release_identity'
        else:
            category = 'unsupported_or_other'
        categories.add(category)
    return sorted(categories)
