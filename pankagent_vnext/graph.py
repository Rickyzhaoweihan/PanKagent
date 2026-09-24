"""Bounded Cypher generation and read-only retrieval against one pinned graph.

The generator executes queries internally. These guards protect this service's
downstream execution; they cannot change the shared generator's own privileges.
Semantic guards enforce explicit plan constraints, not scientific correctness.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from neo4j import AsyncGraphDatabase, READ_ACCESS
from neo4j.graph import Node, Path as Neo4jPath, Relationship
from .graph_contract import DIGEST as CONTRACT_DIGEST, RELATIONS, MEASUREMENTS, generation_request
from .plan_constraints import (CELL_TYPES, build_generation_question, related_context_step,
                               repair_step_constraints, resolved_lookup, step_relation_types)
from .constraint_values import list_value


class GraphValidationError(ValueError):
    """A query or configured graph does not meet the explicit contract."""


def suppress_driver_query_logging() -> None:
    """Keep Cypher and parameter values out of automatic driver diagnostics.

    Server notifications remain enabled for EXPLAIN validation. Only automatic
    Python logging/warnings are silenced; health exposes sanitized failures.
    """
    parent = logging.getLogger("neo4j")
    parent.setLevel(logging.CRITICAL + 1)
    parent.propagate = False
    for name in list(logging.Logger.manager.loggerDict):
        if name == "neo4j" or name.startswith("neo4j."):
            logging.getLogger(name).disabled = True


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    start: int = field(default=0, compare=False)
    end: int = field(default=0, compare=False)


def tokenize(query: str) -> list[Token]:
    """Lex strings, quoted identifiers and comments before checking keywords."""
    out, i = [], 0
    while i < len(query):
        start, count = i, len(out)
        ch = query[i]
        if ch.isspace():
            i += 1
        elif query.startswith("//", i):
            end = query.find("\n", i)
            i = len(query) if end < 0 else end + 1
        elif query.startswith("/*", i):
            end = query.find("*/", i + 2)
            if end < 0:
                raise GraphValidationError("unterminated_comment")
            i = end + 2
        elif ch in "'\"`":
            quote, val = ch, []
            i += 1
            while i < len(query):
                if query[i] == quote:
                    if i + 1 < len(query) and query[i + 1] == quote:
                        val.append(quote)
                        i += 2
                        continue
                    i += 1
                    break
                if query[i] == "\\" and quote != "`":
                    i += 1
                    if i >= len(query):
                        raise GraphValidationError("unterminated_string")
                    val.append({"n": "\n", "r": "\r", "t": "\t"}.get(query[i], query[i]))
                    i += 1
                else:
                    val.append(query[i])
                    i += 1
            else:
                raise GraphValidationError("unterminated_string")
            value = "".join(val)
            if quote == "`" and "\\" in value:
                raise GraphValidationError("unsupported_identifier_escape")
            out.append(Token("IDENT" if quote == "`" else "STRING", value))
        elif ch == "$":
            match = re.match(r"\$([A-Za-z_][A-Za-z_0-9]*)", query[i:])
            if not match:
                raise GraphValidationError("invalid_parameter")
            out.append(Token("PARAM", match[1]))
            i += len(match[0])
        elif ch.isalpha() or ch == "_":
            match = re.match(r"[A-Za-z_][A-Za-z_0-9]*", query[i:])
            if not match:
                raise GraphValidationError("unsupported_identifier")
            out.append(Token("WORD", match[0]))
            i += len(match[0])
        elif ch.isdigit() or ch == "-" and i + 1 < len(query) and query[i + 1].isdigit():
            match = re.match(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", query[i:])
            out.append(Token("NUMBER", match[0]))
            i += len(match[0])
        else:
            if ch == "\\":
                raise GraphValidationError("unsupported_code_escape")
            operator = query[i:i + 2]
            if operator in {">=", "<=", "<>", "!=", "..", "=~"}:
                out.append(Token("SYMBOL", operator))
                i += 2
            else:
                out.append(Token("SYMBOL", ch))
                i += 1
        if len(out) > count:
            out[-1] = replace(out[-1], start=start, end=i)
    return out


def _word(token: Token, word: str) -> bool:
    return token.kind == "WORD" and token.value.upper() == word


def _value(tokens: list[Token], start: int, parameters: dict) -> tuple[Any, int]:
    if start >= len(tokens):
        return None, start
    token = tokens[start]
    if token.kind == "STRING":
        return token.value, start + 1
    if token.kind == "NUMBER":
        return float(token.value), start + 1
    if token.kind == "PARAM":
        return parameters.get(token.value), start + 1
    if token.kind == "WORD" and token.value.lower() in {"true", "false", "null"}:
        return {"true": True, "false": False, "null": None}[token.value.lower()], start + 1
    if token.value == "[":
        values, at = [], start + 1
        while at < len(tokens) and tokens[at].value != "]":
            before = at
            item, at = _value(tokens, at, parameters)
            if at == before:
                return None, start
            values.append(item)
            if at < len(tokens) and tokens[at].value == ",":
                at += 1
            elif at >= len(tokens) or tokens[at].value != "]":
                return None, start
        return (values, at + 1) if at < len(tokens) else (None, start)
    return None, start


def _equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str) and isinstance(expected, str):
        return actual == expected
    if isinstance(actual, list) and isinstance(expected, list):
        return sorted(json.dumps(x, sort_keys=True) for x in actual) == sorted(
            json.dumps(x, sort_keys=True) for x in expected)
    return actual == expected


def _normalized_expected(expected: Any, actual: Any, operator: str) -> Any:
    if operator == "IN":
        try:
            return list_value(expected)
        except ValueError:
            return expected  # Invalid list shapes are rejected by validate_cypher.
    # Legacy scalar constraints retain their original numeric/bool encoding.
    if isinstance(expected, str):
        if isinstance(actual, (int, float)) and not isinstance(actual, bool):
            try:
                return float(expected)
            except ValueError:
                pass
        if isinstance(actual, bool) and expected.lower() in {"true", "false"}:
            return expected.lower() == "true"
    return expected


def _pattern_bindings(tokens: list[Token], *, undirected_patterns=None, graph_release=None):
    """Read mandatory node/edge pattern bindings; never infer from prose/literals."""
    nodes, patterns, edges = {}, [], []
    inference_excluded = set()
    mandatory, i = False, 0
    while i < len(tokens):
        token = tokens[i]
        if token.kind == "WORD" and token.value.upper() in {"MATCH", "WHERE", "RETURN", "WITH", "UNWIND"}:
            mandatory = token.value.upper() == "MATCH" and not (i and _word(tokens[i - 1], "OPTIONAL"))
        if mandatory and token.value == "(" and i + 1 < len(tokens) and tokens[i + 1].kind in {"WORD", "IDENT"}:
            variable, labels, depth, end = tokens[i + 1].value, set(), 1, i + 2
            while end < len(tokens) and depth:
                current = tokens[end]
                if current.value == "(": depth += 1
                elif current.value == ")": depth -= 1
                if depth == 1 and current.value == "{":
                    break
                if depth == 1 and current.value == ":" and end + 1 < len(tokens) and tokens[end + 1].kind in {"WORD", "IDENT"}:
                    labels.add(tokens[end + 1].value)
                end += 1
            # Find the complete node pattern, including a property map.
            depth, end = 1, i + 1
            while end < len(tokens) and depth:
                if tokens[end].value == "(": depth += 1
                elif tokens[end].value == ")": depth -= 1
                end += 1
            if depth == 0:
                nodes.setdefault(variable, set()).update(labels)
                patterns.append((i, end - 1, variable))
                i = end - 1
        i += 1
    for left, right in zip(patterns, patterns[1:]):
        between = tokens[left[1] + 1:right[0]]
        if not between or between[0].value not in {"-", "<"}:
            continue
        values = [token.value for token in between]
        if "[" not in values or "]" not in values or any(token.kind == "SYMBOL" and token.value in {"*"} for token in between):
            continue
        kinds = {between[i + 1].value for i, token in enumerate(between[:-1])
                 if token.value in {":", "|"} and between[i + 1].kind in {"WORD", "IDENT"}}
        if kinds:
            source, target = (right[2], left[2]) if values[0] == "<" else (left[2], right[2])
            edges.append((source, target, kinds))
            if values[0] != "<" and values[-1] != ">":
                # Either stored orientation is possible. Explicit labels still
                # validate normally; do not guess a type for an unlabeled end.
                inference_excluded.update((source, target))
                edges.append((target, source, kinds))
                if undirected_patterns is not None:
                    undirected_patterns.extend([(source,target,kinds),(target,source,kinds)])
    # Preserve node types through simple WITH aliases; never infer a property projection as a node.
    for i, token in enumerate(tokens[1:-1], 1):
        if _word(token, "AS") and tokens[i-1].value in nodes and (i < 2 or tokens[i-2].value != '.'):
            old,new=tokens[i-1].value,tokens[i+1].value
            nodes.setdefault(new, set()).update(nodes[old])
            edges += [(new if a==old else a, new if b==old else b, kinds) for a,b,kinds in list(edges) if old in (a,b)]
            if undirected_patterns is not None:
                undirected_patterns += [(new if a==old else a, new if b==old else b, kinds) for a,b,kinds in list(undirected_patterns) if old in (a,b)]
        # Preserve identity through the bounded singleton-list UNWIND form.
        # Without this, ``UNWIND [d] AS x`` could erase d's donor type before
        # projection/privacy validation.
        elif (_word(token, "AS") and i >= 4 and tokens[i - 1].value == ']'
                and tokens[i - 3].value == '[' and tokens[i - 2].value in nodes
                and any(_word(item, 'UNWIND') for item in tokens[max(0, i - 6):i])):
            old, new = tokens[i - 2].value, tokens[i + 1].value
            nodes.setdefault(new, set()).update(nodes[old])
            edges += [(new if a == old else a, new if b == old else b, kinds)
                      for a, b, kinds in list(edges) if old in (a, b)]
    # Scalar aliases must not acquire a node type from a prior use of the
    # same variable name. Per-branch callers prevent UNION type leakage.
    for i, token in enumerate(tokens[1:-1], 1):
        if _word(token, "AS"):
            old, new = tokens[i-1].value, tokens[i+1].value
            if old not in nodes or (i >= 2 and tokens[i-2].value == '.'):
                inference_excluded.add(new)
            elif old in inference_excluded:
                inference_excluded.add(new)
    if graph_release and not any(_word(t, "UNION") for t in tokens):
        from .endpoint_types import inferred_endpoint_types
        for variable, label in inferred_endpoint_types(nodes, edges, graph_release, excluded=inference_excluded).items():
            nodes[variable].add(label)
    return nodes, edges


def _predicate_owner(tokens: list[Token], index: int) -> str | None:
    if index >= 2 and tokens[index - 1].value == ".":
        return tokens[index - 2].value
    stack = []
    for i, token in enumerate(tokens[:index]):
        if token.value in {"(", "[", "{"}: stack.append(i)
        elif token.value in {")", "]", "}"} and stack: stack.pop()
    for start in reversed(stack):
        if tokens[start].value in {"(", "["} and start + 1 < len(tokens):
            return tokens[start + 1].value
    return None


def _predicate_present(tokens: list[Token], constraint: dict, parameters: dict,
                       allowed_variables: set[str] | None = None) -> bool:
    """Recognize direct WHERE comparisons and MATCH property maps.

    Intentionally fail closed for predicates whose meaning needs a full parser.
    A planner must supply resolved graph properties/values, not prose filters.
    """
    from .numeric_predicates import unit_predicate_present
    if unit_predicate_present(tokens, constraint, parameters, allowed_variables):
        return True
    prop = str(constraint.get("property", "")).split(".")[-1]
    expected_operator = str(constraint.get("operator", "=")).upper()
    expected = constraint.get("value")
    if not prop or expected_operator not in {"=", "!=", "<>", "IN", "CONTAINS", "STARTS WITH", "ENDS WITH", ">", ">=", "<", "<="}:
        return False
    distributed_values = _normalized_expected(expected, [], expected_operator)
    distributed: dict[str, set[str]] = {}
    clause, optional_match = "", False
    for i, token in enumerate(tokens):
        if token.kind == "WORD" and token.value.upper() in {"MATCH", "WHERE", "RETURN", "WITH", "UNWIND", "ORDER"}:
            clause = token.value.upper()
            if clause == "MATCH":
                optional_match = i > 0 and _word(tokens[i - 1], "OPTIONAL")
        if token.kind not in {"WORD", "IDENT"} or token.value != prop or optional_match:
            continue
        if allowed_variables is not None and _predicate_owner(tokens, i) not in allowed_variables:
            continue
        at, transform = i + 1, None
        # toLower(n.name) and toUpper(n.name) preserve the property constraint.
        if at < len(tokens) and tokens[at].value == ")" and i >= 4:
            if tokens[i - 3].value == "(" and tokens[i - 4].value.lower() in {"tolower", "toupper"}:
                transform = tokens[i - 4].value.lower()
                at += 1
        # Numeric representation conversion is allowed only for explicitly
        # registered typed numeric fields; never for numeric-looking IDs.
        from .numeric_predicates import cast_at, float_cast_allowed
        numeric_transform = cast_at(tokens, i) == "tofloat"
        if numeric_transform:
            at += 1
        if at >= len(tokens):
            continue
        operator = tokens[at].value.upper()
        if operator == ":":
            if clause != "MATCH" or i == 0 or tokens[i - 1].value not in {"{", ","}:
                continue
            operator = "="
        elif clause != "WHERE" or i == 0 or tokens[i - 1].value != ".":
            continue
        at += 1
        if operator in {"STARTS", "ENDS"} and at < len(tokens) and _word(tokens[at], "WITH"):
            operator += " WITH"
            at += 1
        actual, end = _value(tokens, at, parameters)
        if end == at:
            continue
        if numeric_transform and not float_cast_allowed(tokens, i, constraint, actual):
            continue
        wanted = _normalized_expected(expected, actual, expected_operator)
        if transform and isinstance(wanted, str):
            wanted = wanted.lower() if transform == "tolower" else wanted.upper()
        if (operator == expected_operator or {operator, expected_operator} <= {"!=", "<>"}) and _equal(actual, wanted):
            return True
        if expected_operator == "=" and operator == "IN" and isinstance(actual, list) and len(actual) == 1 and _equal(actual[0], wanted):
            return True
        if expected_operator == "IN" and operator == "=" and isinstance(distributed_values, list):
            # A plan's entity set can appear as separately bound endpoints of
            # an interaction. Each member must bind a different variable, so
            # n.name='A' AND n.name='B' cannot satisfy this requirement.
            if i >= 2 and tokens[i - 1].value == "." and tokens[i - 2].kind in {"WORD", "IDENT"}:
                for member in distributed_values:
                    compare = member
                    if transform and isinstance(compare, str):
                        compare = compare.lower() if transform == "tolower" else compare.upper()
                    if _equal(actual, compare):
                        key = json.dumps(member, sort_keys=True)
                        distributed.setdefault(key, set()).add(tokens[i - 2].value)
    if expected_operator == "IN" and isinstance(distributed_values, list) and distributed_values:
        keys = {json.dumps(member, sort_keys=True) for member in distributed_values}
        if keys <= distributed.keys():
            matched: dict[str, str] = {}

            def assign(key, visited):
                for variable in distributed[key]:
                    if variable in visited:
                        continue
                    visited.add(variable)
                    if variable not in matched or assign(matched[variable], visited):
                        matched[variable] = key
                        return True
                return False

            if all(assign(key, set()) for key in keys):
                return True
    return False


def _unrequested_identity_filters(tokens: list[Token], constraints: list[dict], parameters: dict, extra_properties=(), *, graph_release=None) -> list[str]:
    """Catch invented identifier/entity restrictions on complete set queries."""
    errors = []
    bindings, _ = _pattern_bindings(tokens, graph_release=graph_release)
    for i, token in enumerate(tokens):
        if token.kind not in {"WORD", "IDENT"}:
            continue
        prop = token.value
        if not (prop in extra_properties or prop.lower().endswith(("name", "_id")) or prop.lower() in {"id", "condition", "gender", "sex", "t1d_stage", "diabetes_type", "derived_diabetes_status", "data_modality", "data_source"}):
            continue
        at, transform = i + 1, None
        if at < len(tokens) and tokens[at].value == ")" and i >= 4 and tokens[i - 3].value == "(":
            if tokens[i - 4].value.lower() in {"tolower", "toupper"}:
                transform = tokens[i - 4].value.lower()
                at += 1
        if at >= len(tokens):
            continue
        operator = tokens[at].value.upper()
        if operator == ":":
            operator = "="
        at += 1
        if operator in {"STARTS", "ENDS"} and at < len(tokens) and _word(tokens[at], "WITH"):
            operator += " WITH"
            at += 1
        if operator not in {"=", "!=", "<>", "IN", "CONTAINS", "STARTS WITH", "ENDS WITH"}:
            continue
        actual, end = _value(tokens, at, parameters)
        if end == at:
            continue
        observed = {"property": prop, "operator": operator, "value": actual}
        if not _predicate_present(tokens, observed, parameters):
            continue
        allowed = False
        for wanted in constraints:
            if str(wanted.get("property", "")).split(".")[-1] != prop:
                continue
            if wanted.get("_entity_type") and wanted["_entity_type"] not in bindings.get(_predicate_owner(tokens, i), set()):
                continue
            wanted_operator = str(wanted.get("operator", "=")).upper()
            expected = _normalized_expected(wanted.get("value"), actual, wanted_operator)
            if transform and isinstance(expected, str):
                expected = expected.lower() if transform == "tolower" else expected.upper()
            if (wanted_operator == operator or {operator, wanted_operator} <= {"!=", "<>"}) and _equal(actual, expected):
                allowed = True
            elif wanted_operator == "=" and operator == "IN" and isinstance(actual, list) and len(actual) == 1 and _equal(actual[0], expected):
                allowed = True
            elif wanted_operator == "IN" and operator == "=" and isinstance(expected, list) and any(_equal(actual, member) for member in expected):
                allowed = True
        if prop == "id" and operator == "IN" and any(_equal(actual, ids) for ids in parameters.values()):
            allowed = True
        if not allowed:
            errors.append("unrequested_identity_filter:" + prop)
    return errors


def _region_scope_errors(tokens: list[Token], step: dict, parameters: dict) -> list[str]:
    """A region's chromosome, build and bounds must bind one required Gene."""
    if 'genomic_scope_contract' not in step:
        return []
    from .genomic_scope import has_verified_region_scope
    if not has_verified_region_scope(step):
        return ['invalid_genomic_scope_contract']
    nodes, paths = _pattern_bindings(tokens, graph_release=step.get('graph_version'))
    predicates = step['genomic_scope_contract']['predicates']
    relations = step_relation_types(step)
    for variable, labels in nodes.items():
        if ('Gene' in labels
                and all(_predicate_present(tokens, predicate, parameters, {variable}) for predicate in predicates)
                and all(any(kind in kinds and variable in (source, target)
                            for source, target, kinds in paths) for kind in relations)):
            return []
    return ['region_scope_not_same_gene']


def _unrequested_measurement_filters(tokens, constraints, parameters, *, graph_release=None):
    """A generated threshold must not silently narrow the requested evidence.

    Check each predicate's actual owner, including simple relationship aliases.
    Only direct recorded comparisons can establish equivalence; calculations,
    casts and null checks need an explicit supported contract instead of being
    treated as a complete search. Projections and ordering are unaffected.
    """
    from .release_schema import REGISTRY, relationship_bindings
    from .scientific_projection import MEASUREMENT_FIELDS
    structural = [t for t in tokens if not _word(t, "OPTIONAL")]
    nodes, _ = _pattern_bindings(structural, graph_release=graph_release)
    relationships = relationship_bindings(tokens)
    measured = {kind: set(fields) - {"expression_call"} for kind, fields in MEASUREMENT_FIELDS.items()}
    measured["PART_OF_QTL_SIGNAL"] = {"pip", "nominal_p", "purity", "lbf", "slope", "n_snp"}
    measured["PART_OF_GWAS_SIGNAL"] = {"pip", "rank", "p", "p_value", "odds_ratio", "beta"}
    # Scalar aliases retain their source property for this rejection check.
    # They cannot authorize a predicate on their own; the required-constraint
    # validator must independently verify any supported requested comparison.
    scalar_aliases = {}
    for index, token in enumerate(tokens):
        if _word(token, "AS") and index >= 3 and index + 1 < len(tokens):
            if tokens[index - 2].value == "." and tokens[index - 3].value in (nodes.keys() | relationships.keys()):
                scalar_aliases[tokens[index + 1].value] = (tokens[index - 3].value, tokens[index - 1].value)
            elif tokens[index - 1].value in scalar_aliases:
                scalar_aliases[tokens[index + 1].value] = scalar_aliases[tokens[index - 1].value]
    errors, clause = [], ""
    comparisons = {"=", "<", "<=", ">", ">=", "<>", "!=", "IN"}
    for index, token in enumerate(tokens):
        if token.kind == "WORD" and token.value.upper() in {"MATCH", "WHERE", "RETURN", "WITH", "UNWIND", "ORDER"}:
            clause = token.value.upper()
        access = index >= 2 and tokens[index - 1].value == "."
        mapped = (index > 0 and index + 1 < len(tokens) and tokens[index - 1].value in {"{", ","}
                  and tokens[index + 1].value == ":")
        aliased = not access and not mapped and token.value in scalar_aliases
        if token.kind not in {"WORD", "IDENT"} or not (access or mapped or aliased):
            continue
        if not (clause == "WHERE" and (access or aliased) or clause == "MATCH" and mapped):
            continue
        owner, prop = scalar_aliases[token.value] if aliased else (_predicate_owner(tokens, index), token.value)
        kinds = relationships.get(owner, set())
        if owner not in nodes and not kinds:
            continue
        known_measurement = any(prop in measured.get(kind, set())
                                and prop in REGISTRY["relations"].get(kind, {}).get("properties", []) for kind in kinds)
        at = index + 1
        operator = tokens[at].value.upper() if at < len(tokens) else ""
        operator = "=" if mapped and operator == ":" else operator
        actual, end = _value(tokens, at + 1, parameters) if operator in comparisons else (None, at + 1)
        numeric = (isinstance(actual, (int, float)) and not isinstance(actual, bool)
                   or isinstance(actual, list) and bool(actual)
                   and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in actual))
        if not known_measurement and not numeric:
            continue
        if operator not in comparisons or end == at + 1:
            errors.append("unsupported_measurement_filter:" + prop)
            continue
        # A parsed literal must be the whole right side, not a prefix such as
        # r.pip > 0.1 + 0.8. Unknown expressions are rejected, never rewritten.
        if end < len(tokens) and tokens[end].value.upper() not in {"AND", "OR", "XOR", ")", "]", "}", ",", "RETURN", "WITH", "ORDER", "MATCH", "OPTIONAL", "LIMIT", "SKIP", ";"}:
            errors.append("unsupported_measurement_filter:" + prop)
            continue
        allowed = False
        for wanted in constraints:
            if str(wanted.get("property", "")).split(".")[-1] != prop:
                continue
            if wanted.get("relationship_type") and wanted["relationship_type"] not in kinds:
                continue
            label = wanted.get("_entity_type") or wanted.get("entity_type")
            if label and label not in nodes.get(owner, set()):
                continue
            wanted_operator = str(wanted.get("operator", "=")).upper()
            expected = _normalized_expected(wanted.get("value"), actual, wanted_operator)
            if (wanted_operator == operator or {wanted_operator, operator} <= {"!=", "<>"}) and _equal(actual, expected):
                allowed = True
            elif wanted_operator == "=" and operator == "IN" and isinstance(actual, list) and len(actual) == 1 and _equal(actual[0], expected):
                allowed = True
        if not allowed:
            errors.append("unrequested_measurement_filter:" + prop)
    return errors


def _unrequested_property_filters(tokens, constraints, parameters, *, graph_release=None):
    """Reject generated property predicates absent from the prepared request.

    Template/request proofs govern the prepared constraints, but the GPU route
    can still invent a WHERE predicate after that boundary.  Every comparison
    against a recorded node/relationship property must therefore match one of
    the already prepared constraint choices (including verified entity and
    modality alternatives) or a dependency ID set.  Projections and ORDER BY
    remain unaffected.
    """
    from .release_schema import REGISTRY, relationship_bindings
    known = {prop for values in REGISTRY.get('nodes', {}).values() for prop in values}
    known.update(prop for spec in REGISTRY.get('relations', {}).values()
                 for prop in spec.get('properties', []))
    structural = [token for token in tokens if not _word(token, 'OPTIONAL')]
    nodes, _ = _pattern_bindings(structural, graph_release=graph_release)
    relationships = relationship_bindings(tokens)
    scalar_alias_sources = {}
    def expression_start(alias_index):
        depth = 0
        pairs = {')': '(', ']': '[', '}': '{'}
        opens = set(pairs.values())
        for position in range(alias_index - 1, -1, -1):
            value = tokens[position].value
            if value in pairs:
                depth += 1
            elif value in opens:
                if depth:
                    depth -= 1
            elif depth == 0 and value == ',':
                return position + 1
            elif (depth == 0 and tokens[position].kind == 'WORD'
                  and value.upper() in {'WITH', 'RETURN'}):
                return position + 1
        return 0
    for index, token in enumerate(tokens):
        if not (_word(token, 'AS') and index >= 3 and index + 1 < len(tokens)):
            continue
        start = expression_start(index)
        property_inputs = {(tokens[position - 2].value, tokens[position].value)
                           for position in range(max(2, start), index)
                           if tokens[position - 1].value == '.'
                           and tokens[position - 2].value in (nodes.keys() | relationships.keys())
                           and tokens[position].value in known}
        for part in tokens[start:index]:
            property_inputs.update(scalar_alias_sources.get(part.value, set()))
        if property_inputs:
            # Track the complete transitive provenance set through wrappers
            # and chained WITH clauses. A predicate on a multi-property alias
            # cannot be equated to only its nearest input.
            scalar_alias_sources[tokens[index + 1].value] = property_inputs
    errors, clause = [], ''
    comparisons = {'=', '<', '<=', '>', '>=', '<>', '!=', 'IN', 'CONTAINS',
                   'STARTS WITH', 'ENDS WITH'}
    boundaries = {'AND', 'OR', 'XOR', ')', ']', '}', ',', 'RETURN', 'WITH',
                  'ORDER', 'MATCH', 'OPTIONAL', 'LIMIT', 'SKIP', ';'}
    for index, token in enumerate(tokens):
        if token.kind == 'WORD' and token.value.upper() in {
                'MATCH', 'WHERE', 'RETURN', 'WITH', 'UNWIND', 'ORDER'}:
            clause = token.value.upper()
        previous = tokens[index - 1] if index else None
        dynamic_lookup = bool(previous and (
            (previous.kind in {'WORD', 'IDENT'}
             and previous.value.upper() not in {
                 'WHERE', 'RETURN', 'WITH', 'UNWIND', 'IN', 'AS', 'AND', 'OR'})
            or previous.value in {')', ']'}))
        numeric_index = (token.value == '[' and index + 2 < len(tokens)
                         and tokens[index + 1].kind == 'NUMBER'
                         and tokens[index + 2].value == ']')
        if (clause == 'WHERE' and token.value == '[' and dynamic_lookup
                and not numeric_index):
            errors.append('unrequested_dynamic_property_filter')
            continue
        access = index >= 2 and tokens[index - 1].value == '.'
        mapped = (index > 0 and index + 1 < len(tokens)
                  and tokens[index - 1].value in {'{', ','}
                  and tokens[index + 1].value == ':')
        alias_sources = (scalar_alias_sources.get(token.value, set())
                         if not access and not mapped else set())
        if clause == 'WHERE' and len(alias_sources) > 1:
            errors.append('unrequested_ambiguous_property_alias_filter')
            continue
        aliased = len(alias_sources) == 1
        alias_source = next(iter(alias_sources)) if aliased else None
        property_name = alias_source[1] if aliased else token.value
        if (token.kind not in {'WORD', 'IDENT'} or property_name not in known
                or not (access or mapped or aliased)):
            continue
        if not (clause == 'WHERE' and (access or aliased)
                or clause == 'MATCH' and mapped):
            continue
        owner, prop = (alias_source if aliased
                       else (_predicate_owner(tokens, index), token.value))
        # Mixed-unit donor age has a dedicated expression parser and exact
        # request-equivalence validator; a token-local generic check would
        # misclassify its reviewed CASE expression.
        if (prop == 'age' and any(
                str(wanted.get('property', '')).split('.')[-1] == 'age'
                for wanted in constraints)):
            continue
        at, transform = index + 1, None
        if access and at < len(tokens) and tokens[at].value == ')' and index >= 4:
            if tokens[index - 3].value == '(' and tokens[index - 4].value.casefold() in {
                    'tolower', 'toupper', 'tofloat', 'tointeger'}:
                transform = tokens[index - 4].value.casefold()
                at += 1
            elif tokens[index - 4].value.casefold() == 'exists':
                errors.append('unrequested_property_filter:' + prop)
                continue
        operator = tokens[at].value.upper() if at < len(tokens) else ''
        operator = '=' if mapped and operator == ':' else operator
        value_at = at + 1
        if operator in {'STARTS', 'ENDS'} and value_at < len(tokens) and _word(tokens[value_at], 'WITH'):
            operator += ' WITH'
            value_at += 1
        if operator == 'IS':
            errors.append('unrequested_property_filter:' + prop)
            continue
        actual, end = _value(tokens, value_at, parameters) if operator in comparisons else (None, value_at)
        if operator not in comparisons or end == value_at:
            # Recorded properties used in a WHERE predicate through an
            # unsupported expression cannot be treated as harmless.
            errors.append('unsupported_property_filter:' + prop)
            continue
        if end < len(tokens) and tokens[end].value.upper() not in boundaries:
            errors.append('unsupported_property_filter:' + prop)
            continue
        allowed = False
        for wanted in constraints:
            if str(wanted.get('property', '')).split('.')[-1] != prop:
                continue
            kinds = relationships.get(owner, set())
            if wanted.get('relationship_type') and wanted['relationship_type'] not in kinds:
                continue
            label = wanted.get('_entity_type') or wanted.get('entity_type')
            if label and label not in nodes.get(owner, set()):
                continue
            wanted_operator = str(wanted.get('operator', '=')).upper()
            expected = _normalized_expected(wanted.get('value'), actual, wanted_operator)
            if transform in {'tolower', 'toupper'} and isinstance(expected, str):
                expected = expected.lower() if transform == 'tolower' else expected.upper()
            if ((wanted_operator == operator or {wanted_operator, operator} <= {'!=', '<>'})
                    and _equal(actual, expected)):
                allowed = True
            elif (wanted_operator == '=' and operator == 'IN'
                  and isinstance(actual, list) and len(actual) == 1
                  and _equal(actual[0], expected)):
                allowed = True
            elif (wanted_operator == 'IN' and operator == '='
                  and isinstance(expected, list)
                  and any(_equal(actual, member) for member in expected)):
                allowed = True
        if (prop == 'id' and operator == 'IN'
                and any(name.startswith('dep_') and _equal(actual, value)
                        for name, value in parameters.items())):
            allowed = True
        if not allowed:
            errors.append('unrequested_property_filter:' + prop)
    return errors


def _constraint_choices(step: dict, index: int, constraint: dict) -> list[dict]:
    """Equivalence is local to a graph-verified entity, never an ID allowlist."""
    for entity in step.get("resolved_entities") or []:
        if (entity.get("state") == "resolved" and entity.get("constraint_index") == index
                and entity.get("graph_version") == step.get("graph_version")
                and entity.get("requested") == constraint and entity.get("entity_type") in entity.get("labels", [])
                and str(constraint.get("property", "")).split(".")[-1] in {"id", "name"}
                and constraint.get("operator", "=") == "="):
            return [{"property": prop, "operator": "=", "value": entity[prop], "_entity_type": entity["entity_type"]}
                    for prop in ("id", "name") if isinstance(entity.get(prop), str) and entity[prop]]
    if constraint.get('property')=='data_modality' and step.get('semantic_registry',{}).get('modality_links_verified'):
        return [{**constraint,'_entity_type':'Sample_node'},
                {**constraint,'property':'id','_entity_type':'data_modality'}]
    if constraint.get("property") == "go_domain" and constraint.get("entity_type") == "GO_term":
        return [{**constraint, "_entity_type": "GO_term"}]
    return [{**constraint, **({"_entity_type": constraint["entity_type"]} if constraint.get("entity_type") else {})}]


def _choice_present(tokens, choice, parameters, *, graph_release=None):
    bindings, _ = _pattern_bindings(tokens, graph_release=graph_release)
    if choice.get("relationship_type"):
        from .release_schema import relationship_bindings
        variables = {v for v, kinds in relationship_bindings(tokens).items() if choice["relationship_type"] in kinds}
        return bool(variables) and _predicate_present(tokens, choice, parameters, variables)
    variables = {variable for variable, labels in bindings.items() if choice["_entity_type"] in labels} if choice.get("_entity_type") else None
    return _predicate_present(tokens, choice, parameters, variables)


def _enrichment_property_errors(tokens: list[Token], step: dict, parameters: dict) -> list[str]:
    """Reject known wrong enrichment fields and unrequested direct cutoffs.

    Neo4j warns about property keys globally: a property on a different edge
    type can otherwise appear valid here. No query predicate is ever rewritten.
    """
    variables = set()
    for i in range(len(tokens) - 4):
        if (tokens[i].value == "[" and tokens[i + 1].kind in {"WORD", "IDENT"}
                and tokens[i + 2].value == ":" and tokens[i + 3].kind in {"WORD", "IDENT"}
                and tokens[i + 3].value == "GENE_ENRICHED_IN" and tokens[i + 4].value in {"]", "{"}):
            variables.add(tokens[i + 1].value)
    if not variables:
        return []
    wrong = {"adjusted_p_value": "padj", "enrichment_rank_in_cell_type": "rank_in_cell_type",
             "enrichment_score": None}
    measurements = {"padj", "pvalue", "log2_fold_change", "rank_in_cell_type", *wrong}
    simple_lookup = resolved_lookup(step)
    errors = []
    for i, token in enumerate(tokens):
        if token.kind not in {"WORD", "IDENT"} or token.value not in measurements:
            continue
        property_access = i >= 2 and tokens[i - 1].value == "."
        map_property = i > 0 and i + 1 < len(tokens) and tokens[i - 1].value in {"{", ","} and tokens[i + 1].value == ":"
        if not (property_access or map_property) or _predicate_owner(tokens, i) not in variables:
            continue
        if token.value in wrong:
            reason = "invalid_relation_property:GENE_ENRICHED_IN." + token.value
            if wrong[token.value]:
                reason += ":use_" + wrong[token.value]
            errors.append(reason)
        if not simple_lookup or token.value not in measurements or i + 2 >= len(tokens):
            continue
        operator = tokens[i + 1].value.upper()
        operator = "=" if operator == ":" else operator
        if operator not in {"=", "<", "<=", ">", ">="}:
            continue
        actual, end = _value(tokens, i + 2, parameters)
        if end == i + 2 or not isinstance(actual, (float, int)) or isinstance(actual, bool):
            continue
        observed = {"property": token.value, "operator": operator, "value": actual}
        if _predicate_present(tokens, observed, parameters, variables):
            errors.append("unrequested_measurement_filter:" + token.value)
    return errors


def _dependency_owner_errors(tokens, name, values, metadata, graph_release):
    """Reject an ID dependency on a provably incompatible node type.

    Metadata is derived from the actual parent nodes, never from ID prefixes or
    the model's intended answer. Overlapping types remain legitimate even when
    their IDs have no intersection: that is a real zero, not a query defect.
    """
    if (not graph_release or not metadata or metadata.get('graph_version') != graph_release
            or not isinstance(values, list) or not all(isinstance(value, str) for value in values)):
        return []
    by_id = metadata.get('id_labels') or {}
    if set(by_id) != set(values) or any(not labels for labels in by_id.values()):
        return []
    labels = set().union(*(set(kinds) for kinds in by_id.values()))
    bindings, _ = _pattern_bindings(tokens, graph_release=graph_release)
    wanted = {'property': 'id', 'operator': 'IN', 'value': values}
    return ['dependency_owner_mismatch:' + name + ':' + variable + ':expected_any_of:' + ','.join(sorted(labels))
            for variable, kinds in bindings.items() if kinds and not kinds.intersection(labels)
            and _predicate_present(tokens, wanted, {name: values}, {variable})]


def _unrequested_classification_projections(tokens, step):
    """Reject protected donor fields in projections regardless of aliases."""
    from .answer_facts import (DONOR_CLASSIFICATION_FIELDS,
                               requested_classification_fields)
    allowed = requested_classification_fields(step)
    protected = set(DONOR_CLASSIFICATION_FIELDS) - set(allowed)
    if not protected:
        return []
    bindings, _ = _pattern_bindings(tokens, graph_release=step.get('graph_version'))
    donor_variables = {variable for variable, labels in bindings.items()
                       if 'donor' in labels}
    donor_in_scope = bool(donor_variables)

    # Follow donor-derived aliases introduced by WITH without treating ordinary
    # final collection transport as a disclosure.  For example, ``collect(d) AS
    # y`` is harmless by itself, but ``properties(head(y))`` reconstructs a full
    # donor property map under an otherwise untyped alias.
    donor_aliases = set(donor_variables)
    alias_clause = None
    for index, token in enumerate(tokens):
        if token.kind == 'WORD' and token.value.upper() in {
                'MATCH', 'OPTIONAL', 'WHERE', 'WITH', 'RETURN', 'UNWIND',
                'ORDER', 'LIMIT', 'SKIP'}:
            alias_clause = token.value.upper()
            continue
        if (alias_clause != 'WITH' or not _word(token, 'AS')
                or index + 1 >= len(tokens)
                or tokens[index + 1].kind not in {'WORD', 'IDENT'}):
            continue
        depth = 0
        start = index
        for cursor in range(index - 1, -1, -1):
            value = tokens[cursor].value
            if value in {')', ']', '}'}:
                depth += 1
            elif value in {'(', '[', '{'}:
                depth = max(0, depth - 1)
            elif depth == 0 and (value == ',' or _word(tokens[cursor], 'WITH')):
                start = cursor + 1
                break
            start = cursor
        alias = tokens[index + 1].value
        if any(item.kind in {'WORD', 'IDENT'} and item.value in donor_aliases
               for item in tokens[start:index]):
            donor_aliases.add(alias)

    def donor_reference_between(start, end):
        return any(item.kind in {'WORD', 'IDENT'} and item.value in donor_aliases
                   for item in tokens[start:end])

    def matching_close(start, opening='(', closing=')'):
        depth = 0
        for cursor in range(start, len(tokens)):
            if tokens[cursor].value == opening:
                depth += 1
            elif tokens[cursor].value == closing:
                depth -= 1
                if depth == 0:
                    return cursor
        return None

    def projected_expression_start(end):
        depth = 0
        start = end
        for cursor in range(end - 1, -1, -1):
            value = tokens[cursor].value
            if value in {')', ']', '}'}:
                depth += 1
            elif value in {'(', '[', '{'}:
                depth = max(0, depth - 1)
            elif depth == 0 and (value == ',' or _word(tokens[cursor], 'WITH')
                                 or _word(tokens[cursor], 'RETURN')):
                return cursor + 1
            start = cursor
        return start

    errors, clause = [], None
    for index, token in enumerate(tokens):
        if token.kind == 'WORD' and token.value.upper() in {
                'MATCH', 'OPTIONAL', 'WHERE', 'WITH', 'RETURN', 'UNWIND',
                'ORDER', 'LIMIT', 'SKIP'}:
            clause = token.value.upper()
            continue
        if (clause in {'WITH', 'RETURN'} and index >= 2
                and tokens[index - 1].value == '.'
                and token.kind in {'WORD', 'IDENT'}
                and token.value in protected):
            errors.append('unrequested_donor_classification_projection:' + token.value)
        if clause not in {'WITH', 'RETURN'}:
            continue
        # Cypher map lookup can conceal a protected key behind an arbitrary
        # alias (``d['t1d_stage'] AS foo``), defeating key-based answer
        # sanitization.  Reject every dynamic donor lookup while any donor
        # classification is unrequested; requested fields remain available via
        # the ordinary, auditable ``d.field`` form above.
        previous = tokens[index - 1] if index else None
        dynamic_lookup = bool(previous and (
            (previous.kind in {'WORD', 'IDENT'}
             and previous.value.upper() not in {'RETURN', 'WITH', 'UNWIND', 'IN', 'AS'})
            or previous.value in {')', ']'}))
        if donor_in_scope and token.value == '[' and dynamic_lookup:
            errors.append('unrequested_donor_dynamic_property_projection')
        # ``properties(d)`` exposes the complete donor property map, including
        # all protected fields, and can then be indexed under a harmless alias.
        if (donor_in_scope and token.kind == 'WORD' and token.value.casefold() == 'properties'
                and index + 3 < len(tokens) and tokens[index + 1].value == '('
                and tokens[index + 2].kind in {'WORD', 'IDENT'}
                and tokens[index + 3].value == ')'):
            errors.append('unrequested_donor_property_map_projection')
        if (donor_in_scope and token.kind == 'WORD' and token.value.casefold() == 'properties'
                and index + 1 < len(tokens) and tokens[index + 1].value == '('):
            close = matching_close(index + 1)
            if close is not None and donor_reference_between(index + 2, close):
                errors.append('unrequested_donor_property_map_projection')
        # Node map projections such as ``d{.*}`` are another spelling of the
        # same full-property disclosure.
        if (donor_in_scope and token.value == '{' and index >= 1
                and tokens[index - 1].kind in {'WORD', 'IDENT'}):
            errors.append('unrequested_donor_property_map_projection')
        if (donor_in_scope and token.value == '{' and index >= 1
                and tokens[index - 1].value in {')', ']'}
                and donor_reference_between(projected_expression_start(index), index)):
            errors.append('unrequested_donor_property_map_projection')
    return sorted(set(errors))


def validate_cypher(query: str, step: dict, parameters: dict | None = None, *, dependency_bindings=None) -> list[str]:
    parameters = parameters or {}
    from .metadata_guard import recovery as metadata_recovery
    unsupported_metadata = metadata_recovery(step, step.get("graph_version", "PanKgraph_08_04"))
    if unsupported_metadata:
        return [unsupported_metadata['category']]
    if not isinstance(query, str) or not query.strip() or len(query) > 24000:
        return ["missing_or_oversized_cypher"]
    try:
        tokens = tokenize(query)
    except GraphValidationError as exc:
        return [str(exc)]
    if tokens and tokens[-1].value == ";" and tokens[-1].kind == "SYMBOL":
        tokens = tokens[:-1]
    if step.get("path_spec") is not None:
        from .bounded_paths import topology_errors as bounded_path_topology_errors
        topology = bounded_path_topology_errors(tokens, step, parameters, tokenize)
        if topology:
            return list(dict.fromkeys(topology))
    forbidden = {"CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "LOAD", "CALL", "FOREACH", "ALTER", "RENAME", "GRANT", "DENY", "REVOKE", "SHOW", "USE", "INSERT", "FINISH"}
    errors = []
    if any(t.kind == "SYMBOL" and t.value == ";" for t in tokens):
        errors.append("multiple_statements")
    if any(t.kind == "WORD" and t.value.upper() in forbidden for t in tokens):
        errors.append("non_readonly_clause")
    if any(t.value == "(" and i >= 3 and tokens[i - 1].kind in {"WORD", "IDENT"}
           and tokens[i - 2].value == "." and tokens[i - 3].kind in {"WORD", "IDENT"}
           for i, t in enumerate(tokens)):
        errors.append("external_function_not_allowed")
    if not tokens or tokens[0].kind != "WORD" or tokens[0].value.upper() not in {"MATCH", "OPTIONAL", "WITH", "UNWIND", "RETURN"}:
        errors.append("unsupported_query_start")
    if not any(_word(t, "RETURN") for t in tokens):
        errors.append("missing_return")
    unknown = {t.value for t in tokens if t.kind == "PARAM"} - set(parameters)
    if unknown:
        errors.append("unknown_parameters")
    slices = any(t.value == "[" and i > 0 and i + 1 < len(tokens)
                 and (tokens[i - 1].kind in {"WORD", "IDENT"} or tokens[i - 1].value in {"]", ")"})
                 and (tokens[i + 1].kind == "NUMBER" and i + 2 < len(tokens) and tokens[i + 2].value == ".."
                      or tokens[i + 1].value == "..") for i, t in enumerate(tokens))
    if step.get("complete", True) and not step.get("path_spec") and (slices or any(
        t.kind == "WORD" and t.value.upper() in {"LIMIT", "SKIP", "RAND"} for t in tokens
    )):
        errors.append("incomplete_limit_or_slice")
    from .annotation_selection import validation_errors as annotation_errors
    errors.extend(annotation_errors(query, step, parameters))
    constraints = list(step.get("constraints") or [])
    for constraint in constraints:
        if str(constraint.get("operator", "=")).upper() == "IN":
            try:
                list_value(constraint.get("value"))
            except ValueError:
                errors.append("invalid_constraint_list:" + str(constraint.get("property", "unknown")))
    dependencies = [name for name in parameters if name.startswith("dep_")]
    if (constraints or dependencies) and not step.get("path_spec") and any(
            _word(t, "OR") or _word(t, "XOR") or _word(t, "NOT") for t in tokens):
        errors.append("ambiguous_constraint_boolean_logic")
    branches, branch = [], []
    for token in tokens:
        if _word(token, "UNION"):
            branches.append(branch)
            branch = []
        elif not branch and _word(token, "ALL"):
            continue
        else:
            branch.append(token)
    branches.append(branch)
    from .ranking_contract import validation_errors as ranking_validation_errors
    ranking_errors = ranking_validation_errors(tokens, step, parameters)
    errors.extend(ranking_errors)
    from .coloc_query_guard import validation_errors as coloc_validation_errors
    errors.extend(coloc_validation_errors(tokens, step, parameters))
    from .scientific_projection import validation_errors as projection_validation_errors
    errors.extend(projection_validation_errors(tokens, step, parameters))
    errors.extend(_unrequested_classification_projections(tokens, step))
    choices = [_constraint_choices(step, index, constraint) for index, constraint in enumerate(constraints)]
    measurement_choices = [choice for group in choices for choice in group]
    ranking = step.get("ranking_contract") or {}
    # Direction-only selection is a verified requested predicate even when it
    # was derived from "upregulated" rather than a literal planner constraint.
    # Consume the existing complete ranking validator; never add a global
    # allowance for positive effects or for arbitrary ranking thresholds.
    if not ranking_errors and ranking.get("effect_direction") in {"positive", "negative"}:
        from .ranking_contract import FIELDS
        effect = FIELDS.get(ranking.get("relation_type"), {}).get("effect")
        if effect:
            measurement_choices.append({"property": effect, "operator": ">" if ranking["effect_direction"] == "positive" else "<",
                                        "value": 0, "relationship_type": ranking["relation_type"]})
    for constraint, alternatives in zip(constraints, choices):
        if not all(any(_choice_present(part, choice, parameters, graph_release=step.get("graph_version")) for choice in alternatives) for part in branches):
            errors.append("missing_required_filter:" + str(constraint.get("property", "unknown")))
    relations = step_relation_types(step)
    for part in branches:
        if step.get("evidence_combination", "independent") == "independent":
            _, bindings = _pattern_bindings(part, graph_release=step.get("graph_version"))
            measurement_paths = [path for path in bindings if path[2] & MEASUREMENTS]
            if len(measurement_paths) > 1 and len({kind for path in measurement_paths for kind in path[2] & MEASUREMENTS}) > 1:
                errors.append("independent_measurements_require_separate_steps")
    for part in branches:
        _, paths = _pattern_bindings(part, graph_release=step.get("graph_version"))
        # Every production step is normalized with an explicit relationship
        # contract (including an explicit empty list).  Preserve compatibility
        # for the old low-level validator API that omitted the key entirely,
        # while fail-closing every generated mandatory join once the contract
        # exists. OPTIONAL context is intentionally absent from ``paths``.
        if 'relation_types' in step:
            mandatory_relations = {kind for _, _, kinds in paths for kind in kinds}
            allowed_relations = set(relations)
            constraint_owners = {constraint.get('entity_type')
                                 for constraint in step.get('constraints') or []}
            # A donor/sample cohort with an explicit disease predicate needs
            # the reviewed disease-HAS_DONOR->donor witness even when the
            # primary requested relation is HAS_SAMPLE.  This narrow derived
            # join is already enforced by cohort_scope; no donor-only request
            # receives the same allowance.
            if (allowed_relations <= {'HAS_DONOR', 'HAS_SAMPLE'}
                    and 'disease' in constraint_owners):
                allowed_relations.add('HAS_DONOR')
            for relation in sorted(mandatory_relations - allowed_relations):
                errors.append('unrequested_mandatory_relation:' + relation)
        errors.extend(_region_scope_errors(part, step, parameters))
        errors.extend(_enrichment_property_errors(part, step, parameters))
        errors.extend(_unrequested_measurement_filters(part, measurement_choices, parameters,
                                                       graph_release=step.get("graph_version")))
        errors.extend(_unrequested_property_filters(part, measurement_choices, parameters,
                                                    graph_release=step.get("graph_version")))
        from .numeric_predicates import validation_errors as numeric_validation_errors
        errors.extend(numeric_validation_errors(part, step, parameters))
        from .measurement_properties import validation_errors as detection_validation_errors
        errors.extend(detection_validation_errors(part, step, parameters))
        from .release_schema import structural_errors
        errors.extend(structural_errors(part, step, parameters))
        from .anatomy_paths import endpoint_role_errors
        errors.extend(endpoint_role_errors(part, step, parameters))
        bindings, _ = _pattern_bindings(part, graph_release=step.get("graph_version"))
        from .semantic_registry import validation_errors
        errors.extend(validation_errors(part, step, parameters, bindings, paths, _predicate_present, choices))
        from .donor_query_guard import sample_path_errors
        errors.extend(sample_path_errors(bindings, paths))
        from .cohort_scope import validation_errors as cohort_scope_errors
        errors.extend(cohort_scope_errors(part, step, parameters, choices))
        for source, target, kinds in paths:
            correct = lambda a, b: 'Gene' in bindings.get(a, set()) and 'anatomical_structure' in bindings.get(b, set())
            undirected = (target, source, kinds) in paths
            if kinds & MEASUREMENTS and bindings.get(source) and bindings.get(target) and not (correct(source, target) or undirected and correct(target, source)):
                errors.append('measurement_endpoint_schema_mismatch')
        for relation in relations:
            if not any(relation in kinds for _, _, kinds in paths):
                errors.append("missing_required_relation:" + str(relation))
        lookup = resolved_lookup(step)
        if lookup:
            gene, cell, relation = lookup
            gene_choices, cell_choices = choices[gene["constraint_index"]], choices[cell["constraint_index"]]
            def at(variable, alternatives):
                bindings, _ = _pattern_bindings(part, graph_release=step.get("graph_version"))
                return any(choice.get("_entity_type") in bindings.get(variable, set())
                           and _predicate_present(part, choice, parameters, {variable}) for choice in alternatives)
            if not any(relation in kinds and at(source, gene_choices) and at(target, cell_choices)
                       for source, target, kinds in paths):
                errors.append("missing_required_entity_relation_path")
    for name in dependencies:
        # Require an actual bounded id predicate in every UNION arm. Mentioning
        # a dependency parameter in a comment or RETURN does not preserve it.
        wanted = {"property": "id", "operator": "IN", "value": parameters[name]}
        if not all(_predicate_present(part, wanted, parameters) for part in branches):
            errors.append("missing_dependency:" + name)
        index = int(name[4:]) if name[4:].isdigit() else -1
        source_id = (step.get('depends_on') or [])[index] if 0 <= index < len(step.get('depends_on') or []) else None
        explicit = next((b for b in step.get('input_bindings', []) if b['step_id'] == source_id), None)
        if explicit and not step.get('path_spec'):
            for part in branches:
                owners, directed_paths = _pattern_bindings(part, graph_release=step.get('graph_version'))
                variables = {a if explicit['target_role'] == 'source' else b for a, b, kinds in directed_paths
                             if kinds & set(step.get('relation_types', []))}
                variables = {v for v in variables if explicit['entity_type'] in owners.get(v, set())}
                if len(variables) != 1 or not _predicate_present(part, wanted, parameters, variables):
                    errors.append('dependency_role_mismatch:' + name)
        for part in branches:
            errors.extend(_dependency_owner_errors(part, name, parameters[name],
                (dependency_bindings or {}).get(name), step.get('graph_version')))
    if step.get("complete", True):
        from .semantic_registry import PROPERTIES
        extra={p for fields in PROPERTIES.values() for p in fields} if step.get('semantic_registry') else set()
        for part in branches:
            errors.extend(_unrequested_identity_filters(part, [choice for group in choices for choice in group], parameters, extra, graph_release=step.get("graph_version")))
    return list(dict.fromkeys(errors))


def schema_fingerprint(labels: list[str], relationship_types: list[str]) -> str:
    payload = {"labels": sorted(set(labels)), "relationship_types": sorted(set(relationship_types))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(k): _safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(v) for v in value]
    if hasattr(value, "iso_format"):
        return value.iso_format()
    return str(value)


def _node_id(node: Node) -> str:
    return str(node.get("id") if node.get("id") is not None else node.element_id)


def _public_edge_fingerprint(edge: Mapping) -> str:
    """Hash the durable public relationship shape, never a Neo4j element ID."""
    encoded = json.dumps(edge, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class GraphAdapter:
    def __init__(self, settings):
        self.settings = settings
        suppress_driver_query_logging()
        self.http = httpx.AsyncClient(timeout=settings.cypher_timeout)
        self.driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=min(settings.graph_timeout, 5),
            max_connection_pool_size=8, connection_acquisition_timeout=settings.graph_timeout,
            warn_notification_severity="OFF",
        )
        self.identity_verified = False
        self.identity_check_time = 0.0
        self._identity_lock = asyncio.Lock()
        self.last_generation_success = None
        self.last_query_success = None
        self.last_generation_error = None
        self.identity_details = {}
        self._resolution_secret = secrets.token_bytes(32)
        self._entity_cache = {}
        self.release_labels, self.release_relations = set(), set()
        self._semantic_cache = None
        self._semantic_lock = asyncio.Lock()
        from .planning_contract import VerifiedCache
        self._query_cache = VerifiedCache(ttl=300)
        self.query_repair = None

    async def ground_question(self, question):
        from .preplanning_grounding import ground_question
        # Strict deterministic grammars must not fall through to a model plan
        # merely because a fresh service is still constructing its public
        # entity index.  The longer bound changes latency only: exact entity
        # resolution and every normal admission/validation guard remain
        # mandatory.  General questions retain the established 3 s deadline.
        from .pattern_planning import is_no_variant_coloc_role_frame
        from .bounded_paths import is_hla_path_request
        timeout = 12.0 if (
            is_no_variant_coloc_role_frame(question)
            or is_hla_path_request(question)
        ) else 3.0
        grounded = await ground_question(self, question, timeout_seconds=timeout)
        if 'tissues' not in (grounded.get('sample_terminology') or {}) and re.search(
                r'donor|sample|tissue|\bHPAP\b|\b(?:in|from|within)\s+(?:the\s+)?[A-Z]{3,8}\b', question, re.I):
            # A missing large entity index must not hide the smaller verified
            # categorical inventory used for term clarification.
            async def terminology():
                await self._ensure_identity()
                return await self.semantic_vocabulary()
            try:
                vocabulary = await asyncio.wait_for(terminology(), timeout=3.0)
                grounded['term_vocabulary'] = {key: vocabulary.get(key, [] if key != 'inventory_complete' else False)
                    for key in ('donor_sources', 'sample_sources', 'tissues', 'inventory_complete')}
                grounded['term_inventory_status'] = 'verified_fallback'
            except asyncio.TimeoutError:
                grounded['term_inventory_status'] = 'unavailable'
        from .entity_lookup import retain_grounding_proofs
        return retain_grounding_proofs(self, grounded)

    async def close(self):
        await self.http.aclose()
        await self.driver.close()

    def _session(self):
        return self.driver.session(database=self.settings.neo4j_database, default_access_mode=READ_ACCESS)

    async def _small_query(self, query: str, params: dict | None = None) -> list[dict]:
        async with self._session() as session:
            async with await session.begin_transaction(timeout=self.settings.graph_timeout) as tx:
                result = await tx.run(query, params or {})
                return [dict(record) async for record in result]

    def preview_identity(self) -> dict:
        """Stable release/config identity for durable preview reuse, without keys."""
        from .anatomy_resolution import VERSION as anatomy_version
        path = Path(getattr(self.settings, "graph_identity_file", ""))
        manifest_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        from .semantic_registry import DIGEST as semantic_digest
        from .anatomy_scope import DIGEST as anatomy_scope_digest
        from .bounded_paths import DIGEST as bounded_path_digest, VERSION as bounded_path_version
        from .pattern_planning import DIGEST as pattern_plan_digest, VERSION as pattern_plan_version
        from .coloc_records import DIGEST as coloc_record_digest, VERSION as coloc_record_version
        from .agent_schemas import active_pack
        return {"agent_schema": active_pack().identity(), "record_comparison_contract": "recorded-signal-comparison-v1", "semantic_registry": semantic_digest, "anatomy_resolver": anatomy_version, "anatomy_scope": anatomy_scope_digest, "qtl_tissue_binding": "qtl-tissue-owner-v2", "measurement_filter_contract": "requested-measurement-predicates-v1", "graph_version": self.settings.graph_version, "identity_manifest_sha256": manifest_hash,
                "bounded_path_version": bounded_path_version,
                "bounded_path_sha256": bounded_path_digest,
                "signal_pattern_version": pattern_plan_version,
                "signal_pattern_sha256": pattern_plan_digest,
                "colocalization_record_version": coloc_record_version,
                "colocalization_record_sha256": coloc_record_digest,
                "identity_verified": self.identity_verified,
                **{key: getattr(self.settings, key, None) for key in (
                    "neo4j_uri", "neo4j_database", "cypher_url", "max_nodes", "max_edges", "max_rows",
                    "max_bytes", "graph_timeout", "cypher_timeout")}}

    def _resolution_signature(self, step: dict) -> str:
        if not hasattr(self, "_resolution_secret"):
            self._resolution_secret = secrets.token_bytes(32)
        payload = {key: value for key, value in step.items() if key not in {"resolution_key", "gpu_participation_required"}}
        body = json.dumps([self.preview_identity(), payload], sort_keys=True, separators=(",", ":"), default=str).encode()
        return hmac.new(self._resolution_secret, body, hashlib.sha256).hexdigest()

    def _resolution_verified(self, step: dict) -> bool:
        key = step.get("resolution_key")
        return (isinstance(key, str) and step.get("graph_version") == self.settings.graph_version
                and hmac.compare_digest(key, self._resolution_signature(step)))

    async def _ensure_identity(self):
        if not self.identity_verified or time.monotonic() - self.identity_check_time > 60:
            health = await self.probe()
            if health.get("state") != "healthy":
                raise GraphValidationError("graph_identity_unavailable")

    def _entity_type(self, constraint: dict, step: dict) -> str | None:
        if constraint.get("entity_type"):
            return {"cell_type":"anatomical_structure", "tissue":"anatomical_structure", "cell":"anatomical_structure"}.get(constraint["entity_type"], constraint["entity_type"])
        prop, value = str(constraint.get("property", "")), str(constraint.get("value", ""))
        prefix = prop.split(".")[0].lower() if "." in prop else ""
        if prefix in {"gene", "cell", "disease", "donor"}:
            return {"gene": "Gene", "cell": "anatomical_structure", "disease": "disease", "donor": "donor"}[prefix]
        if value in {name for name, _ in CELL_TYPES.values()} or re.fullmatch(r"CL_\d+", value):
            return "anatomical_structure"
        if re.fullmatch(r"ENSG\d+|NCBIGene[:_]\d+", value):
            return "Gene"
        if re.fullmatch(r"MONDO_\d+", value):
            return "disease"
        return None

    async def resolve_entities(self, requests):
        from .entity_lookup import resolve_entities
        return await resolve_entities(self, requests)

    async def _resolve_constraint(self, constraint: dict, index: int, step: dict) -> dict:
        entry = {"constraint_index": index, "requested": dict(constraint), "state": "unsupported",
                 "graph_version": self.settings.graph_version, "labels": []}
        prop, value = str(constraint.get("property", "")).split(".")[-1], constraint.get("value")
        # Reuse the reviewed, release-scoped signal identity aliases even when
        # preplanning grounding timed out. Verify the canonical node live;
        # retaining the original request literal lets authorization stay strict.
        from .coloc_scope import VERIFIED_IDENTITY_ALIASES
        signal_relations = {'SIGNAL_COLOC_WITH', 'PART_OF_GWAS_SIGNAL'}
        if (constraint.get('entity_type') == 'disease' and prop == 'name'
                and constraint.get('operator', '=') == '='
                and set(step.get('relation_types') or []) & signal_relations):
            aliases = [identifier for (release, kind, identifier), names in VERIFIED_IDENTITY_ALIASES.items()
                       if release == self.settings.graph_version and kind == 'disease'
                       and isinstance(value, str) and value.casefold() in {n.casefold() for n in names}]
            if len(aliases) == 1:
                canonical = {**constraint, 'property': 'id', 'value': aliases[0]}
                resolved = await self._resolve_constraint(canonical, index, step)
                if resolved.get('state') == 'resolved':
                    return {**resolved, 'requested': dict(constraint),
                            'verified_signal_alias': True}
        from .genomic_scope import is_verified_region_constraint
        if (step.get('graph_version') == self.settings.graph_version
                and is_verified_region_constraint(constraint, step)):
            return {**entry, "state": "literal_predicate"}
        if self._entity_type(constraint, step) == 'anatomical_structure' and prop in {'name','id'} and isinstance(value,str) and 0 < len(value) <= 512:
            explicit_pattern = constraint.get('operator','=') == 'CONTAINS' and re.search(r'\bcontains?\b|\bcontaining\b|substring|names? matching',step.get('question',''),re.I)
            if constraint.get('operator','=') in {'=','CONTAINS'} and not explicit_pattern:
                from .anatomy_resolution import resolve_anatomy, VERSION
                if not hasattr(self, '_anatomy_lock'): self._anatomy_lock=asyncio.Lock()
                async with self._anatomy_lock:
                    key=(self.settings.graph_version, VERSION, json.dumps(self.preview_identity(),sort_keys=True))
                    cached=getattr(self, '_anatomy_inventory', None)
                    if not cached or cached[0]!=key or time.monotonic()-cached[1]>300:
                        rows=await self._small_query("MATCH (n:anatomical_structure) RETURN n.id AS id,n.name AS name,labels(n) AS labels")
                        self._anatomy_inventory=(key,time.monotonic(),rows)
                    records=self._anatomy_inventory[2]
                return {**entry, **resolve_anatomy(value,records,self.settings.graph_version,prop)}
        if constraint.get("operator", "=") == "CONTAINS" and constraint.get("entity_type") in {"kegg", "reactome", "anatomical_structure"} and prop == "name":
            selector = "n:anatomical_structure" if constraint.get("entity_type") == "anatomical_structure" else "n:kegg OR n:reactome"
            rows = await self._small_query("MATCH (n) WHERE ("+selector+") AND toLower(n.name) CONTAINS toLower($value) RETURN n.id AS id, n.name AS name, labels(n) AS labels LIMIT 3", {"value":value})
            if not re.search(r'\bcontains?\b|\bcontaining\b|substring|names? matching', step.get('question',''), re.I):
                exact_rows = await self._small_query("MATCH (n) WHERE ("+selector+") AND toLower(n.name) = toLower($value) RETURN n.id AS id, n.name AS name, labels(n) AS labels LIMIT 3", {"value":value})
                if len(exact_rows)==1: rows=exact_rows
            if len(rows) == 1:
                candidate = rows[0]
                kind = next(k for k in ("kegg", "reactome", "anatomical_structure") if k in candidate['labels'])
                return {**entry, **candidate, "entity_type":kind, "state":"resolved", "unique_pattern_match":True}
            return {**entry, "state":"ambiguous" if rows else "not_found", "candidates":rows}
        if constraint.get("operator", "=") != "=":
            return {**entry, "state": "literal_predicate"}
        if prop not in {"id", "name"} or constraint.get("operator", "=") != "=" or not isinstance(value, str) or not value or len(value) > 512:
            return entry
        label = self._entity_type(constraint, step)
        labels = getattr(self, "release_labels", set())
        if label and (not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", label) or labels and label not in labels):
            return {**entry, "reason": "unknown_entity_type"}
        cache = getattr(self, "_entity_cache", {})
        self._entity_cache = cache
        cache_key = json.dumps([self.preview_identity(), label, prop, value], sort_keys=True)
        from .entity_lookup import valid_selection
        request_text = (step.get('semantic_request') or {}).get('question') or step.get('question', '')
        selected = [proof for proof in step.get('entity_selection_proofs', [])
                    if proof.get('entity_type') == label and value in {proof.get('id'), proof.get('name'), proof.get('mention')}
                    and valid_selection(self, proof, request_text)]
        if len(selected) == 1:
            proof = selected[0]
            rows = await self._small_query(f"MATCH (n:`{label}`) WHERE n.id = $id RETURN n.id AS id, n.name AS name, labels(n) AS labels", {'id': proof['id']})
            if len(rows) == 1:
                return {**entry, **rows[0], 'entity_type': label, 'state': 'resolved',
                        'original_requested': {**constraint, 'property': 'name', 'value': proof['mention']},
                        'model_identity_interpretation': proof}
        cached = cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            return {**deepcopy(cached[1]), "constraint_index": index, "requested": dict(constraint)}
        pathway = label in {"kegg", "reactome"} and prop == "name"
        node = "(n)" if pathway else f"(n:`{label}`)" if label else "(n)"
        collection_filter = "(n:kegg OR n:reactome) AND " if pathway else ""
        query = f"MATCH {node} WHERE {collection_filter}n.`{prop}` = $value RETURN n.id AS id, n.name AS name, labels(n)[..16] AS labels LIMIT 3"
        timeout = min(self.settings.graph_timeout, 3)
        rows = await asyncio.wait_for(self._small_query(query, {"value": value}), timeout)
        if not rows and prop == "name":
            query = f"MATCH {node} WHERE {collection_filter}toLower(n.name) = toLower($value) RETURN n.id AS id, n.name AS name, labels(n)[..16] AS labels LIMIT 3"
            rows = await asyncio.wait_for(self._small_query(query, {"value": value}), timeout)
        if not rows and prop == 'name':
            from .entity_lookup import LABELS, lookup
            if label in LABELS:
                found = await lookup(self, value, label, fuzzy=False)
                rows = found.get('candidates', [])
        candidates = [{"id": row.get("id"), "name": row.get("name"), "labels": row.get("labels", [])}
                      for row in rows[:3]]
        invalid_metadata = any(not isinstance(row["id"], str) or not row["id"] or len(row["id"]) > 512
                               or row["name"] is not None and (not isinstance(row["name"], str) or len(row["name"]) > 512)
                               or not isinstance(row["labels"], list) or len(row["labels"]) > 16
                               or any(not isinstance(label, str) or len(label) > 128 for label in row["labels"])
                               for row in candidates)
        if invalid_metadata:
            result = {**entry, "reason": "unsupported_canonical_entity"}
        elif not candidates:
            suggestions=[]
            if label and prop=='name':
                from difflib import get_close_matches
                # Bounded metadata suggestions only; never substitute a fuzzy entity.
                names=await asyncio.wait_for(self._small_query(f"MATCH {node} WHERE n.name IS NOT NULL RETURN n.name AS name ORDER BY n.name LIMIT 2000"), timeout)
                pool=[r['name'] for r in names if isinstance(r.get('name'),str)]
                suggestions=get_close_matches(value,pool,n=3,cutoff=.65)
            result = {**entry, "state": "not_found", "candidates": [], "suggestions":suggestions}
        elif len(candidates) != 1:
            result = {**entry, "state": "ambiguous", "candidates": candidates, "candidate_limit": 3,
                      "candidates_complete": len(candidates) < 3}
        else:
            candidate = candidates[0]
            canonical_type = (next((k for k in ("kegg", "reactome") if k in candidate["labels"]), None) if pathway else label) or next((kind for kind in ("Gene", "anatomical_structure", "disease", "donor", "variants", "GO_term", "reactome", "kegg", "Sample_node", "data_modality") if kind in candidate["labels"]), None)
            if (not canonical_type or canonical_type not in candidate["labels"] or
                    not isinstance(candidate["id"], str) or not candidate["id"] or len(candidate["id"]) > 512 or
                    candidate["name"] is not None and (not isinstance(candidate["name"], str) or len(candidate["name"]) > 512)):
                result = {**entry, "reason": "unsupported_canonical_entity"}
            else:
                result = {**entry, **candidate, "entity_type": canonical_type, "state": "resolved"}
        cache[cache_key] = time.monotonic(), deepcopy(result)
        while len(cache) > 256:
            cache.pop(next(iter(cache)))
        return {**result, "requested": dict(constraint)}

    async def semantic_vocabulary(self, *, force=False):
        from .semantic_registry import DIGEST
        key=(self.settings.graph_version,DIGEST)
        if not hasattr(self, '_semantic_lock'): self._semantic_lock=asyncio.Lock()
        async with self._semantic_lock:
            cached=getattr(self,'_semantic_cache',None)
            if not force and cached and cached[0]==key and time.monotonic()-cached[1]<300:return cached[2]
            from .donor_categories import CATEGORICAL_FIELDS
            # Fixed schema fields, not user-supplied query text. This is the same
            # complete metadata scan as the stage/source inventory.
            categorical_columns = ''.join(', collect(DISTINCT d.' + field + ') AS category_' + field
                                          for field in CATEGORICAL_FIELDS)
            rows=await self._small_query("MATCH (d:donor) RETURN collect(DISTINCT d.t1d_stage) AS stages, collect(DISTINCT d.data_source) AS donor_sources" + categorical_columns)
            modalities=await self._small_query("MATCH (s:Sample_node) RETURN collect(DISTINCT s.data_modality) AS modalities, collect(DISTINCT s.data_source) AS sample_sources")
            check=await self._small_query("MATCH (m:data_modality)-[:HAS_SAMPLE]->(s:Sample_node) RETURN count(CASE WHEN m.id <> s.data_modality OR s.data_modality IS NULL THEN 1 END) AS mismatches, count(*) AS links")
            tissues=await self._small_query("MATCH (a:anatomical_structure)-[:HAS_SAMPLE]->(:Sample_node) RETURN DISTINCT a.id AS id, a.name AS name LIMIT 2000")
            diseases=await self._small_query("MATCH (x:disease)-[:HAS_DONOR]->(:donor) RETURN DISTINCT x.id AS id, x.name AS name, x.synonyms AS synonyms")
            value={'inventory_complete':True, 'tissues':tissues,**(rows[0] if rows else {}),**(modalities[0] if modalities else {}), 'modality_links_verified':bool(check and check[0]['links'] and check[0]['mismatches']==0)}
            for field in ('stages', 'donor_sources', 'sample_sources', 'modalities'):
                raw = value.get(field)
                value[field] = sorted(raw) if isinstance(raw, list) and all(isinstance(item, str) for item in raw) else []
            value['tissues'] = sorted((row for row in tissues
                if isinstance(row, dict) and isinstance(row.get('id'), str)),
                key=lambda row: (row.get('id', ''), str(row.get('name') or '')))
            value['sources'] = sorted(set(value['donor_sources']) | set(value['sample_sources']))
            value['donor_diseases'] = sorted(({
                'id': row.get('id'), 'name': row.get('name'), 'synonyms': row.get('synonyms')}
                for row in diseases if isinstance(row.get('id'), str) and row.get('id')),
                key=lambda row: row['id'])
            value['donor_categories_complete'] = bool(rows) and all(
                isinstance(rows[0].get('category_' + field), list)
                and all(isinstance(item, str) for item in rows[0]['category_' + field])
                for field in CATEGORICAL_FIELDS)
            value['donor_categorical_values'] = {field: sorted(rows[0]['category_' + field])
                for field in CATEGORICAL_FIELDS if rows
                and isinstance(rows[0].get('category_' + field), list)
                and all(isinstance(item, str) for item in rows[0]['category_' + field])}
            assay_sources=await self._small_query("MATCH (d:donor)-[:HAS_SAMPLE]->(s:Sample_node) RETURN s.data_modality AS modality, collect(DISTINCT coalesce(d.data_source, '<unknown>')) AS sources")
            value['assay_donor_sources']={r['modality']:sorted(r['sources']) for r in assay_sources
                if isinstance(r.get('modality'), str) and isinstance(r.get('sources'), list)
                and all(isinstance(item, str) for item in r['sources'])}
            value['inventory_sha256'] = hashlib.sha256(json.dumps({
                key: value.get(key) for key in ('stages', 'donor_sources', 'sample_sources',
                    'modalities', 'donor_categorical_values', 'donor_diseases', 'tissues',
                    'assay_donor_sources', 'inventory_complete', 'donor_categories_complete')},
                sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
            self._semantic_cache=(key,time.monotonic(),value)
            return value

    async def _prepare_step(self, source: dict, emit) -> dict:
        if self._resolution_verified(source):
            return deepcopy(source)
        step = repair_step_constraints({key: value for key, value in source.items()
                                        if key not in {"resolution_key", "resolved_entities", "entity_resolution",
                                            "recovery", "semantic_issues", "resolved_constraints", "semantic_registry",
                                            "sample_requirements", "semantic_summary", "request_filter_bindings",
                                            "runtime_binding_issues"}})
        from .release_schema import normalize_constraints
        step = normalize_constraints(step)
        from .measurement_scope import measurement_scope_recovery
        from .metadata_guard import recovery as metadata_recovery
        unsupported_scope = (metadata_recovery(step, self.settings.graph_version)
                             or step.get("ranking_issue")
                             or measurement_scope_recovery(step, self.settings.graph_version))
        if unsupported_scope:
            step.update(graph_version=self.settings.graph_version, relation_types=step_relation_types(step),
                        resolved_entities=[], recovery=unsupported_scope,
                        semantic_issues=[unsupported_scope['message']],
                        entity_resolution={'state': 'needs_clarification',
                                           'graph_version': self.settings.graph_version,
                                           'unknown_relations': []})
            step['resolution_key'] = self._resolution_signature(step)
            return step
        from .semantic_registry import semantic_intent, resolve
        if semantic_intent(step):
            step=resolve(step,await self.semantic_vocabulary(),self.settings.graph_version)
        step["graph_version"] = self.settings.graph_version
        step["relation_types"] = step_relation_types(step)
        if (self.settings.graph_version == "PanKgraph_08_04"
                and step["relation_types"] == ["PART_OF_QTL_SIGNAL"]):
            from .release_schema import REGISTRY
            for constraint in step.get("constraints", []):
                if (constraint.get("property") != "tissue" or constraint.get("entity_type")
                        or constraint.get("operator", "=") != "=" or not isinstance(constraint.get("value"), str)):
                    continue
                # The generic word "tissue" is not a stored property. Select
                # its owner/value only from the complete release categories.
                matches = [(prop, value) for prop in ("tissue_name", "tissue_id")
                           for value in REGISTRY["categories"].get("PART_OF_QTL_SIGNAL." + prop, [])
                           if isinstance(value, str) and value.casefold() == constraint["value"].casefold()]
                if len(matches) != 1:
                    continue
                original = deepcopy(constraint)
                prop, value = matches[0]
                constraint.update(entity_type=None, property=prop, value=value,
                                  owner_kind="relationship", relationship_type="PART_OF_QTL_SIGNAL")
                step.setdefault("schema_bindings", []).append({
                    "kind": "verified_qtl_tissue_category", "version": "qtl-tissue-owner-v2",
                    "graph_version": self.settings.graph_version, "requested": original,
                    "canonical_binding": deepcopy(constraint), "registry_version": REGISTRY["version"],
                    "source": "full-release QTL tissue property/category inventory"})
        identities = [(index, constraint) for index, constraint in enumerate(step.get("constraints") or [])
                      if str(constraint.get("property", "")).split(".")[-1] in {"name", "id"}]
        if len(identities) > 8:
            raise GraphValidationError("too_many_entity_constraints")
        entities = []
        for index, constraint in identities:
            await emit("progress", {"stage": "resolving_entities", "step_id": step.get("id")})
            entities.append(await self._resolve_constraint(constraint, index, step))
        for entry in entities:
            if entry.get('verified_signal_alias') and entry.get('state') == 'resolved':
                entry['original_requested'] = deepcopy(entry['requested'])
                step['constraints'][entry['constraint_index']].update(property='id', value=entry['id'])
                entry['requested'] = deepcopy(step['constraints'][entry['constraint_index']])
            if entry.get("state") == "resolved" and entry.get("entity_type") in {"kegg", "reactome", "anatomical_structure"}:
                entry["original_requested"] = deepcopy(entry["requested"])
                if entry.get("unique_pattern_match"):
                    step["constraints"][entry["constraint_index"]].update(property="id", operator="=", value=entry["id"])
                step["constraints"][entry["constraint_index"]]["entity_type"] = entry["entity_type"]
                entry["requested"] = deepcopy(step["constraints"][entry["constraint_index"]])
        # QTL tissue is a relationship measurement context, not an extra
        # anatomical endpoint. Resolve the requested tissue first, then bind
        # that exact verified ID to the recorded QTL relationship property.
        # Other evidence categories keep their original anatomy-node filters.
        if (self.settings.graph_version == "PanKgraph_08_04"
                and step["relation_types"] == ["PART_OF_QTL_SIGNAL"]):
            for entry in entities:
                index = entry.get("constraint_index")
                constraint = step["constraints"][index]
                if (entry.get("state") != "resolved"
                        or entry.get("graph_version") != self.settings.graph_version
                        or entry.get("entity_type") != "anatomical_structure"
                        or "anatomical_structure" not in entry.get("labels", [])
                        or entry.get("requested") != constraint
                        or constraint.get("property") not in {"id", "name"}
                        or constraint.get("operator", "=") != "="
                        or not isinstance(entry.get("id"), str) or not entry["id"]):
                    continue
                original = deepcopy(entry.get("original_requested", constraint))
                canonical = {**constraint, "entity_type": None, "property": "tissue_id",
                             "operator": "=", "value": entry["id"], "owner_kind": "relationship",
                             "relationship_type": "PART_OF_QTL_SIGNAL"}
                step["constraints"][index] = canonical
                binding = {"kind": "verified_qtl_tissue_property", "version": "qtl-tissue-owner-v2",
                           "graph_version": self.settings.graph_version, "requested": original,
                           "canonical_binding": deepcopy(canonical),
                           "resolved_tissue": {key: deepcopy(entry.get(key)) for key in ("id", "name", "labels")},
                           "source": "release-verified anatomical identity and QTL tissue_id property ownership"}
                step.setdefault("schema_bindings", []).append(binding)
                entry.update(state="literal_predicate", original_requested=original,
                             requested=deepcopy(canonical), property_binding=deepcopy(binding))
            # A QTL record has one Gene endpoint. Name and ID constraints
            # independently verified as that same entity are redundant; retain
            # one canonical ID while keeping both original requests auditable.
            by_id = {}
            for entry in entities:
                index = entry["constraint_index"]
                constraint = step["constraints"][index]
                if (entry.get("state") == "resolved" and entry.get("entity_type") == "Gene"
                        and "Gene" in entry.get("labels", []) and entry.get("graph_version") == self.settings.graph_version
                        and entry.get("requested") == constraint and constraint.get("property") in {"id", "name"}
                        and constraint.get("operator", "=") == "=" and isinstance(entry.get("id"), str) and entry["id"]):
                    by_id.setdefault(entry["id"], []).append(entry)
            removed = set()
            for identifier, duplicates in by_id.items():
                if len(duplicates) < 2:
                    continue
                first = duplicates[0]
                index = first["constraint_index"]
                original = [deepcopy(entry["requested"]) for entry in duplicates]
                canonical = {**step["constraints"][index], "entity_type": "Gene", "property": "id", "value": identifier}
                step["constraints"][index] = canonical
                first.update(requested=deepcopy(canonical), original_requests=original)
                removed.update(entry["constraint_index"] for entry in duplicates[1:])
                step.setdefault("schema_bindings", []).append({
                    "kind": "verified_same_qtl_gene_identity", "version": "qtl-tissue-owner-v2",
                    "graph_version": self.settings.graph_version, "requested": original,
                    "canonical_binding": deepcopy(canonical), "source": "independently resolved identical QTL Gene endpoint"})
            if removed:
                indexes = {old: new for new, old in enumerate(i for i in range(len(step["constraints"])) if i not in removed)}
                step["constraints"] = [c for i, c in enumerate(step["constraints"]) if i not in removed]
                entities = [entry for entry in entities if entry["constraint_index"] not in removed]
                for entry in entities:
                    entry["constraint_index"] = indexes[entry["constraint_index"]]
        step["resolved_entities"] = entities
        from .semantic_registry import attach_request_authorizations
        step = attach_request_authorizations(step)
        from .query_templates import runtime_binding_errors
        binding_issues = runtime_binding_errors(step)
        if binding_issues:
            # Planning and execution share one proof gate.  Surface a generic
            # revision request before confirmation while retaining only the
            # value-free reason codes needed for audit/debugging.
            step['runtime_binding_issues'] = binding_issues
            message = ('One or more requested filters need current graph '
                       'resolution before this plan can run.')
            if message not in (step.get('semantic_issues') or []):
                step.setdefault('semantic_issues', []).append(message)
        entities = step["resolved_entities"]
        unresolved = [item for item in entities if item["state"] not in {"resolved", "literal_predicate"}]
        unknown_relations = [kind for kind in step["relation_types"] if getattr(self, "release_relations", set()) and kind not in self.release_relations]
        step["entity_resolution"] = {"state": "needs_clarification" if unresolved or unknown_relations or step.get("semantic_issues") else "resolved" if entities else "not_required",
                                     "graph_version": self.settings.graph_version, "unknown_relations": unknown_relations}
        step["resolution_key"] = self._resolution_signature(step)
        return step

    async def prepare_plan(self, plan: dict, emit) -> dict:
        """Resolve bounded entity identities and expose one context step for review."""
        await self._ensure_identity()
        if len(plan.get("steps") or []) > 12:
            raise GraphValidationError("plan_too_large")
        from .ranking_contract import attach_to_plan
        plan = attach_to_plan(plan, self.settings.graph_version)
        from .coloc_scope import normalize_plan as normalize_coloc_scope
        plan = normalize_coloc_scope(plan, self.settings.graph_version, max_steps=12)
        if plan.get('coloc_scope_issue'):
            return plan
        from .anatomy_scope import normalize_plan as normalize_anatomy_scope
        plan = await normalize_anatomy_scope(plan, self.settings.graph_version, self._resolve_constraint, max_steps=3)
        if plan.get('anatomy_scope_issue'):
            return plan
        prepared = {**plan, "steps": []}
        old_recovery = prepared.get('recovery') or {}
        if old_recovery.get('category') in {'scope_needs_clarification', 'stage_needs_clarification',
                                            'recorded_stage_unavailable', 'stage_inventory_unavailable',
                                            'unsupported_expression_stratification', 'ranking_needs_clarification'}:
            prepared.pop('recovery', None)
            if prepared.get('clarification') == old_recovery.get('message'):
                prepared['clarification'] = None
        for source in plan.get("steps") or []:
            # A donor-only parent supplies IDs; sample conditions are enforced
            # by its explicit downstream HAS_SAMPLE task, not by both tasks.
            if ((source.get('relation_types') == ['HAS_DONOR']
                     or not source.get('relation_types') and any(c.get('entity_type') == 'donor' for c in source.get('constraints', [])))
                    and any(source['id'] in child.get('depends_on', [])
                            and child.get('relation_types') == ['HAS_SAMPLE']
                            for child in plan.get('steps', []))
                    and not any(c.get('entity_type') in {'Sample_node', 'data_modality', 'anatomical_structure'}
                                for c in source.get('constraints', []))):
                source = {**source, 'deferred_sample_scope': True}
            if source.get('operation') or source.get('session_input'):
                prepared['steps'].append({**source, 'graph_version': self.settings.graph_version,
                    'resolved_entities': [], 'entity_resolution': {'state': 'resolved', 'unknown_relations': []}})
                continue
            if plan.get('original_question'):
                source = {**source, 'semantic_request': {
                    'source': 'user_request', 'question': plan.get('effective_question') or plan['original_question'],
                    'original_question': plan['original_question'],
                    'revision_instruction': (plan.get('revision_trace') or {}).get('instruction', '')}}
            from .annotation_selection import apply_default
            source = apply_default(source, plan.get('original_question', ''))
            resolved = await self._prepare_step(source, emit)
            from .term_clarification import repair_generated_scope
            repaired = repair_generated_scope(source, resolved)
            if repaired is not None:
                resolved = await self._prepare_step(repaired, emit)
                resolved['planning_repair'] = {'kind': 'remove_unrequested_tissue', 'attempts': 1,
                    'original_step_question': source.get('question'),
                    'verified': not resolved.get('semantic_issues') and resolved.get('entity_resolution', {}).get('state') != 'needs_clarification'}
            prepared["steps"].append(resolved)
        from .dependency_scope import normalize as preserve_dependency_scope
        prepared = preserve_dependency_scope(prepared)
        from .coloc_scope import compile_comparisons
        prepared = compile_comparisons(prepared, self.settings.graph_version)
        from .signal_comparison import compile_comparisons as compile_record_comparisons
        prepared = compile_record_comparisons(prepared, self.settings.graph_version)
        rewritten = set((prepared.get('coloc_comparison_normalization') or {}).get('rewritten_step_ids') or [])
        if rewritten:
            prepared['steps'] = [await self._prepare_step(step, emit) if step['id'] in rewritten else step
                                 for step in prepared['steps']]
        if any(step.get('sample_requirements',{}).get('capability_scope_verified') and 'scRNA-seq' in group and 'snMultiomics' in group for step in prepared['steps'] for group in step.get('sample_requirements',{}).get('modality_groups',[])):
            interpretation=prepared.get('interpreted_question') or prepared['steps'][0]['question']
            note=' Include documented RNA components of HPAP multiome assays, retaining their original assay labels.'
            if note.strip() not in interpretation:prepared['interpreted_question']=interpretation+note
        context = related_context_step(prepared)
        if context:
            if (plan.get('original_question')
                    and not isinstance(context.get('semantic_request'), dict)):
                context = {**context, 'semantic_request': {
                    'source': 'user_request', 'question': plan.get('effective_question') or plan['original_question'],
                    'original_question': plan['original_question'],
                    'revision_instruction': (plan.get('revision_trace') or {}).get(
                        'instruction', '')}}
            prepared["steps"].append(await self._prepare_step(context, emit))
        issues = [{"step_id": step["id"], "entities": [item for item in step["resolved_entities"] if item["state"] not in {"resolved", "literal_predicate"}],
                   "unknown_relations": step["entity_resolution"]["unknown_relations"], "terminology":step.get("semantic_issues",[])}
                  for step in prepared["steps"] if step["entity_resolution"]["state"] == "needs_clarification"]
        prepared["entity_resolution"] = {"state": "needs_clarification" if issues else "resolved",
                                          "graph_version": self.settings.graph_version, "issues": issues}
        if issues:
            from .query_recovery import plan_recovery
            prepared["recovery"] = plan_recovery(prepared, self.settings.graph_version)
            prepared["clarification"] = prepared["recovery"]["message"]
        from .filter_recovery import recover_filter_failures
        prepared = recover_filter_failures(prepared)
        from .annotation_selection import allocate_independent_budgets
        prepared = allocate_independent_budgets(prepared, self.settings)
        for step in prepared["steps"]:
            step["resolution_key"] = self._resolution_signature(step)
        return prepared

    async def _verify_identity(self):
        manifest_path = Path(self.settings.graph_identity_file)
        if not manifest_path.is_file():
            raise GraphValidationError("graph_identity_manifest_missing")
        manifest = json.loads(manifest_path.read_text())
        for key, expected in {
            "graph_version": self.settings.graph_version,
            "neo4j_uri": self.settings.neo4j_uri,
            "database": self.settings.neo4j_database,
        }.items():
            if not expected or manifest.get(key) != expected:
                raise GraphValidationError("graph_identity_mismatch:" + key)
        if not manifest.get("anchors") or not manifest.get("schema_sha256"):
            raise GraphValidationError("graph_identity_manifest_incomplete")
        labels = [row["label"] for row in await self._small_query("CALL db.labels() YIELD label RETURN label")]
        relationships = [row["relationshipType"] for row in await self._small_query(
            "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")]
        if schema_fingerprint(labels, relationships) != manifest["schema_sha256"]:
            raise GraphValidationError("graph_schema_identity_mismatch")
        self.release_labels, self.release_relations = set(labels), set(relationships)
        if self.settings.graph_version == "PanKgraph_08_04" and not set(RELATIONS) <= self.release_relations:
            raise GraphValidationError("graph_contract_relationship_mismatch")
        for anchor in manifest["anchors"]:
            label, prop = anchor.get("label", ""), anchor.get("property", "")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", label) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", prop):
                raise GraphValidationError("invalid_identity_anchor")
            expected_count = anchor.get("count", 1)
            if not isinstance(expected_count, int) or expected_count < 1:
                raise GraphValidationError("invalid_identity_anchor_count")
            rows = await self._small_query(
                f"MATCH (n:`{label}`) WHERE n.`{prop}` = $value RETURN count(n) AS count",
                {"value": anchor["value"]},
            )
            if not rows or rows[0]["count"] != expected_count:
                raise GraphValidationError("graph_anchor_identity_mismatch")
        self.identity_verified = True
        self.identity_check_time = time.monotonic()
        self.identity_details = {
            "read_only_enforcement": "application_guard_and_read_transactions",
            "database_role_enforced": manifest.get("database_role_enforced", False),
            "database_auth_enabled": manifest.get("database_auth_enabled"),
        }

    async def probe(self) -> dict:
        start = time.monotonic()
        try:
            async with self._identity_lock:
                await asyncio.wait_for(self._verify_identity(), timeout=self.settings.graph_timeout)
            return {"state": "healthy", "graph_version": self.settings.graph_version,
                    "identity_verified": True, "identity_strength": "schema_and_anchors",
                    "details": self.identity_details,
                    "recent_query_success": self.last_query_success,
                    "latency_ms": round((time.monotonic() - start) * 1000, 2)}
        except Exception as exc:
            self.identity_verified = False
            return {"state": "unavailable", "identity_verified": False,
                    "graph_version": self.settings.graph_version,
                    "error_category": str(exc) if isinstance(exc, GraphValidationError) else type(exc).__name__,
                    "latency_ms": round((time.monotonic() - start) * 1000, 2)}

    async def probe_cypher(self) -> dict:
        """Authenticated reachability is reported separately from inference."""
        headers = {"Authorization": "Bearer " + self.settings.cypher_token}
        try:
            health, info = await asyncio.gather(
                self.http.get(self.settings.cypher_url.rstrip("/") + "/health"),
                self.http.get(self.settings.cypher_url.rstrip("/") + "/v1/info", headers=headers),
                return_exceptions=True,
            )

            def body(response):
                if not isinstance(response, httpx.Response):
                    return None
                try:
                    value = response.json()
                    return value if isinstance(value, dict) else None
                except (ValueError, TypeError):
                    return None

            def count(value):
                # Booleans are integers in Python, but never replica counts.
                if type(value) is int and 0 <= value <= 1024:
                    return value
                if isinstance(value, str) and re.fullmatch(r"\s*[0-9]{1,4}\s*", value):
                    parsed = int(value)
                    return parsed if parsed <= 1024 else None
                return None

            h, metadata = body(health), body(info)
            health_ok = isinstance(health, httpx.Response) and health.is_success and h is not None
            authenticated = isinstance(info, httpx.Response) and info.is_success and metadata is not None
            h, metadata = h or {}, metadata or {}
            raw = h.get("backends_up")
            available, total = None, None
            fraction = re.fullmatch(r"\s*([0-9]{1,4})\s*/\s*([0-9]{1,4})\s*", raw) if isinstance(raw, str) else None
            if fraction:
                available, total = count(fraction[1]), count(fraction[2])
                if available is None or total is None or total == 0 or available > total:
                    available, total = None, None
            else:
                available = count(raw)
                total = count(h.get("backends_total"))
                if total == 0 or available is not None and total is not None and available > total:
                    available, total = None, None

            state = "degraded"
            if available == 0 or not health_ok or not authenticated or h.get("status") in {"down", "unavailable"}:
                state = "unavailable"
            elif available is not None and total is not None and available == total and h.get("status") in {"ok", "healthy"}:
                state = "healthy"

            category = None
            if isinstance(info, httpx.Response) and info.status_code in {401, 403}:
                category = "authentication" if info.status_code == 401 else "authorization"
            elif any(isinstance(value, httpx.TimeoutException) for value in (health, info)):
                category = "timeout"
            elif any(isinstance(value, BaseException) for value in (health, info)):
                category = "connection"
            elif not health_ok or not authenticated or available == 0:
                category = "dependency_unavailable"
            elif available is None:
                category = "invalid_response"
            return {"state": state, "authenticated": authenticated, "backends_up": available,
                    "healthy_replicas": available, "total_replicas": total,
                    "error_category": category,
                    "model": metadata.get("model"), "prompt_version": metadata.get("prompt_version"),
                    "recent_generation_success": self.last_generation_success,
                    "generation_error_category": self.last_generation_error}
        except Exception as exc:
            return {"state": "unavailable", "error_category": type(exc).__name__,
                    "recent_generation_success": self.last_generation_success}

    async def _generate(self, question: str, n: int) -> list[str]:
        try:
            response = await self.http.post(
                self.settings.cypher_url.rstrip("/") + "/v1/cypher",
                headers={"Authorization": "Bearer " + self.settings.cypher_token},
                json={"question": question, "n": n},
                timeout=min(30, self.settings.cypher_timeout * (2 if n == 8 else 1)),
            )
            response.raise_for_status()
            body = response.json()
            # Inspect emitted order, not the server's size-ranked primary. The
            # first candidate that satisfies every guard wins deterministically.
            candidates = body.get("candidates", [])
            queries = []
            if isinstance(candidates, list):
                for item in candidates[:max(1, n)]:
                    query = item if isinstance(item, str) else item.get("cypher") if isinstance(item, dict) else None
                    if isinstance(query, str) and query.strip() and query not in queries:
                        queries.append(query)
            primary = body.get("cypher")
            if isinstance(primary, str) and primary.strip() and primary not in queries:
                queries.append(primary)
            if not queries:
                raise GraphValidationError("no_usable_cypher")
            self.last_generation_success = datetime.now(timezone.utc).isoformat()
            self.last_generation_error = None
            class Candidates(list):
                pass
            result = Candidates(queries)
            result.metadata = {key: body.get(key) for key in ('ok', 'model', 'prompt_version', 'backend_port')}
            return result
        except Exception as exc:
            self.last_generation_error = type(exc).__name__
            raise

    async def _explain(self, query: str, parameters: dict) -> list[str]:
        try:
            async with self._session() as session:
                async with await session.begin_transaction(timeout=self.settings.graph_timeout) as tx:
                    result = await tx.run("EXPLAIN " + query.rstrip().rstrip(";"), parameters)
                    summary = await result.consume()
            errors = []
            for note in getattr(summary, "notifications", None) or []:
                code = note.get("code", "")
                if any(fragment in code for fragment in ("UnknownLabel", "UnknownRelationshipType", "UnknownPropertyKey", "CartesianProduct", "UnboundedVariableLengthPattern")):
                    errors.append("schema_or_plan_warning:" + code.rsplit(".", 1)[-1])
            return errors
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            # Statement diagnostics describe only the submitted Cypher. Never
            # include connection/authentication exception messages or reprs.
            message = str(getattr(exc, "message", ""))[:500] if code.startswith("Neo.ClientError.Statement.") else ""
            return ["cypher_explain_failed:" + code + (":" + message if message else "")]

    async def _retrieve(self, query: str, parameters: dict, limits: dict | None = None) -> dict:
        nodes, edges, rows, size, truncated = {}, {}, [], 0, False
        active_insertions = None
        limits = limits or {}
        seen_nodes = set(limits.get("known_node_ids", []))
        seen_edges = set(limits.get("known_edge_keys", []))
        byte_limit = max(0, self.settings.max_bytes - limits.get("used_bytes", 0))
        byte_limit = min(byte_limit, limits.get("max_step_bytes", byte_limit))
        row_limit = max(0, getattr(self.settings, "max_rows", 1000) - limits.get("used_rows", 0))
        row_limit = min(row_limit, limits.get("max_step_rows", row_limit))

        def put(target: dict, key, value, maximum, seen, budget_key=None):
            nonlocal size, truncated, active_insertions
            if key in target:
                return
            budget_key = key if budget_key is None else budget_key
            width = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
            if (budget_key not in seen and len(seen) >= maximum or size + width > byte_limit
                    or target is nodes and len(nodes) >= limits.get("max_step_nodes", self.settings.max_nodes)
                    or target is edges and len(edges) >= limits.get("max_step_edges", self.settings.max_edges)):
                truncated = True
                return
            seen_before = budget_key in seen
            target[key] = value
            seen.add(budget_key)
            size += width
            if active_insertions is not None:
                active_insertions.append(
                    (target, key, seen, budget_key, width, seen_before))

        def rollback_insertions(insertions):
            nonlocal size
            for target, key, seen, budget_key, width, seen_before in reversed(insertions):
                if key in target:
                    target.pop(key)
                    size -= width
                if not seen_before:
                    seen.discard(budget_key)

        def walk(value):
            if truncated:
                return None
            if isinstance(value, Node):
                nid = _node_id(value)
                put(nodes, nid, {"id": nid, "labels": sorted(value.labels), "properties": _safe_value(dict(value))}, self.settings.max_nodes, seen_nodes)
                return {"node_id": nid}
            if isinstance(value, Relationship):
                walk(value.start_node)
                walk(value.end_node)
                edge = {"start_id": _node_id(value.start_node), "end_id": _node_id(value.end_node),
                        "type": value.type, "properties": _safe_value(dict(value))}
                fingerprint = _public_edge_fingerprint(edge)
                if edge["start_id"] in nodes and edge["end_id"] in nodes:
                    # An internal ID deduplicates repeated result paths but is
                    # intentionally absent from the stable public edge shape.
                    put(edges, value.element_id, edge, self.settings.max_edges, seen_edges,
                        json.dumps(edge, sort_keys=True, separators=(",", ":")))
                return {"edge": [edge["start_id"], edge["type"], edge["end_id"]],
                        "fingerprint": fingerprint}
            if isinstance(value, Neo4jPath):
                for node in value.nodes:
                    walk(node)
                for edge in value.relationships:
                    walk(edge)
                return {"path": [_node_id(node) for node in value.nodes]}
            if isinstance(value, Mapping):
                result = {}
                for key, item in value.items():
                    if truncated:
                        break
                    result[str(key)] = walk(item)
                return result
            if isinstance(value, (list, tuple)):
                result = []
                for item in value:
                    if truncated:
                        break
                    result.append(walk(item))
                return result
            return _safe_value(value)

        def walk_path_records(values):
            """Materialize a bounded-path aggregate as an atomic prefix.

            A deterministic path query returns one aggregate row.  The normal
            graph walker may hit a byte/node/edge cap in the middle of that
            row; discarding the row would leave display nodes and edges without
            their verified role ordering.  Reserve the serialized row bytes as
            each complete path is added and roll back the first path that does
            not fit, so any exposed prefix remains internally consistent.
            """
            nonlocal active_insertions, truncated
            materialized = []
            row_width = len(b'{"path_records":[]}')
            for item in values:
                if truncated:
                    break
                insertions = []
                prior_capture = active_insertions
                active_insertions = insertions
                try:
                    converted = walk(item)
                finally:
                    active_insertions = prior_capture
                if truncated:
                    rollback_insertions(insertions)
                    break
                item_width = len(json.dumps(
                    converted, ensure_ascii=False, separators=(",", ":")).encode())
                # Replacing [] with [item] adds item_width bytes; every later
                # element adds one comma plus item_width.  This is exactly the
                # compact JSON width without repeatedly serializing the entire
                # growing aggregate (important for 1,000+ verified paths).
                candidate_width = row_width + item_width + bool(materialized)
                if size + candidate_width > byte_limit:
                    rollback_insertions(insertions)
                    truncated = True
                    break
                materialized.append(converted)
                row_width = candidate_width
            return {"path_records": materialized}, row_width

        async with self._session() as session:
            async with await session.begin_transaction(timeout=self.settings.graph_timeout) as tx:
                result = await tx.run(query.rstrip().rstrip(";"), parameters)
                async for record in result:
                    items = list(record.items())
                    if (limits.get("bounded_path_records") is True
                            and len(items) == 1 and items[0][0] == "path_records"
                            and isinstance(items[0][1], (list, tuple))):
                        if len(rows) >= row_limit:
                            truncated = True
                            await tx.rollback()
                            break
                        row, width = walk_path_records(items[0][1])
                        if size + width > byte_limit:
                            truncated = True
                        else:
                            from .semantic_registry import meaningful_row
                            if meaningful_row(row):
                                rows.append(row)
                                size += width
                        if truncated:
                            # The retained row is an atomic, role-preserving
                            # prefix.  Discard the unread cursor tail.
                            await tx.rollback()
                            break
                        continue
                    row = {key: walk(value) for key, value in record.items()}
                    width = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
                    if truncated or len(rows) >= row_limit or size + width > byte_limit:
                        truncated = True
                    else:
                        from .semantic_registry import meaningful_row
                        if meaningful_row(row): rows.append(row)
                        size += width
                    if truncated:
                        # Exiting without a commit rolls back the read tx and
                        # discards the cursor instead of materializing the tail.
                        await tx.rollback()
                        break
        self.last_query_success = datetime.now(timezone.utc).isoformat()
        return {"nodes": list(nodes.values()), "edges": list(edges.values()), "rows": rows,
                "truncated": truncated, "materialized_bytes": size,
                "retrieval_execution": {"completed": True, "cursor_exhausted": not truncated,
                                        "mode": "read_only"},
                "status": "partial" if truncated else "complete" if nodes or rows else "empty"}

    async def hydrate_viewer_ids(self, graph_version, typed_ids):
        """Read annotations for explicit backend membership; never expand it."""
        if graph_version != self.settings.graph_version:
            raise ValueError("graph_release_mismatch")
        await self._ensure_identity()
        if len(typed_ids) > self.settings.max_nodes:
            raise ValueError("viewer_identity_limit")
        # Types and IDs are both parameters, not interpolated query text.
        return await self._retrieve(
            "UNWIND $identities AS identity MATCH (n) "
            "WHERE n.id = identity.id AND identity.entity_type IN labels(n) "
            "RETURN n",
            {"identities": [{"id": identifier, "entity_type": label}
                            for identifier, label in typed_ids]})

    async def execute(self, step: dict, previous: dict, emit) -> dict:
        from .annotation_selection import inherited_budget
        step = deepcopy(step)
        step['retrieval_budget'] = inherited_budget(step, previous)
        result = await self._execute(step, previous, emit)
        result['resource_budget'] = {
            'allocated': deepcopy(step['retrieval_budget']),
            'consumed': {'max_nodes': len(result.get('nodes', [])),
                         'max_edges': len(result.get('edges', [])),
                         'max_rows': len(result.get('rows', [])),
                         'max_bytes': result.get('materialized_bytes', 0)}}
        return result

    async def _execute(self, step: dict, previous: dict, emit) -> dict:
        # Older confirmed plans may name a cell in prose but omit its predicate.
        # Recover only the narrow, verified entity constraint; guards stay strict.
        step = repair_step_constraints(step)
        def requested_scope(current):
            scope = {"constraints": deepcopy(current.get("constraints", [])),
                     "relation_types": deepcopy(current.get("relation_types", [])),
                     "complete": current.get("complete", True),
                     "retrieval_selection": deepcopy(current.get("retrieval_selection"))}
            if current.get("path_spec") is not None:
                scope["path_spec"] = deepcopy(current["path_spec"])
            request = current.get("semantic_request") or {}
            if (request.get("source") == "user_request"
                    and isinstance(request.get("question"), str)):
                scope["original_question"] = request["question"]
            clinical = (current.get("semantic_registry") or {}).get("clinical_intent")
            if isinstance(clinical, dict):
                scope["clinical_intent"] = deepcopy(clinical)
            if current.get('interpretation_warnings'):
                scope['interpretation_warnings'] = list(current['interpretation_warnings'])
            return scope
        base = {"step_id": step.get("id"), "question": step.get("question"), "graph_version": self.settings.graph_version,
                "nodes": [], "edges": [], "rows": [], "queries": [], "validation": [],
                "truncated": False, "status": "failed", "provenance": [], "contract_sha256": CONTRACT_DIGEST, "generator_attempts": [], "retry_eligible": False,
                "requested_scope": requested_scope(step),
                **{key: step[key] for key in ("title", "purpose", "context_for", "rationale") if key in step}}
        if step.get('filter_warning'):
            warning = deepcopy(step['filter_warning'])
            base['requested_scope']['filter_warning'] = warning
            # Existing scope rendering displays the title for failed checks.
            # Preserve this explanation through the unchanged synthesis adapter.
            base['title'] = (step.get('title') or step.get('question') or 'Requested check') + '. ' + warning['message']
            base['error'] = {'category': warning['category'], 'message': warning['message']}
        if step.get("path_spec") is not None:
            from .bounded_paths import plan_issue as bounded_path_plan_issue
            issue = bounded_path_plan_issue(step)
            if issue:
                reason = "unsupported_bounded_path_spec:" + issue
                base["validation"].append({"valid": False, "reasons": [reason]})
                base["error"] = {"category": "unsupported_bounded_path_spec",
                                 "message": "The requested bounded path is outside the verified fixed-path contract."}
                return base
        if step.get('semantic_issues') or step.get('recovery'):
            base['validation'].append({'valid': False,
                'reasons': ['semantic_scope_unresolved']})
            if step.get('recovery'):
                base['recovery'] = deepcopy(step['recovery'])
            return base
        if step.get("gwas_scope_unavailable"):
            base["validation"].append({"valid": False, "reasons": ["gene_gwas_variant_scope_unresolved"]})
            base["error"] = {"category": "scope_unavailable", "message": "The requested gene has no verified variant or locus binding for this GWAS check. A disease-wide search was not substituted."}
            return base
        from .metadata_guard import recovery as metadata_recovery
        unsupported_metadata = metadata_recovery(step, self.settings.graph_version)
        if unsupported_metadata:
            base["validation"].append({"valid": False, "reasons": [unsupported_metadata["category"]]})
            base["recovery"] = unsupported_metadata
            return base
        limits = {
            "known_node_ids": {str(node["id"]) for item in previous.values() for node in item.get("nodes", [])},
            "known_edge_keys": {json.dumps(edge, sort_keys=True, separators=(",", ":")) for item in previous.values() for edge in item.get("edges", [])},
            "used_bytes": sum(item.get("materialized_bytes", 0) for item in previous.values()),
            "used_rows": sum(len(item.get("rows", [])) for item in previous.values()),
            "max_step_nodes": min(20 if step.get("purpose") == "context" else self.settings.max_nodes, (step.get("retrieval_budget") or {}).get("max_nodes", self.settings.max_nodes)),
            "max_step_bytes": (step.get("retrieval_budget") or {}).get("max_bytes", getattr(self.settings, "max_bytes", 2_000_000)),
            "max_step_edges": (step.get("retrieval_budget") or {}).get("max_edges", getattr(self.settings, "max_edges", 5000)),
            "max_step_rows": (step.get("retrieval_budget") or {}).get("max_rows", getattr(self.settings, "max_rows", 1000)),
        }
        if limits["used_bytes"] >= getattr(self.settings, "max_bytes", 2_000_000):
            base["status"], base["truncated"] = "partial", True
            base["validation"].append({"valid": False, "reasons": ["run_graph_materialization_limit"]})
            return base
        # A stale verification is refreshed; a mismatched or absent manifest
        # never silently switches the graph endpoint.
        if not self.identity_verified or time.monotonic() - self.identity_check_time > 60:
            health = await self.probe()
            if health["state"] != "healthy":
                base["validation"].append({"valid": False, "reasons": [health.get("error_category", "graph_unavailable")]})
                return base
        from .semantic_registry import semantic_intent
        preparation_required = bool(
            step.get('relation_types') or step.get('semantic_registry')
            or step.get('semantic_issues') or step.get('recovery')
            or semantic_intent(step)
            or bool(step.get('constraints'))
            or any(isinstance(constraint, dict) and (
                constraint.get('entity_type') or constraint.get('owner_kind')
                or constraint.get('relationship_type'))
                for constraint in step.get('constraints') or []))
        if preparation_required and not self._resolution_verified(step):
            step = await self._prepare_step(step, emit)
        if "resolved_entities" in step:
            base["requested_scope"] = requested_scope(step)
            base["resolved_entities"] = step["resolved_entities"]
            if step["entity_resolution"]["state"] == "needs_clarification":
                base["validation"].append({"valid": False, "reasons": ["unresolved_plan_entities"]})
                return base
        if step.get('semantic_issues') or step.get('recovery'):
            base['validation'].append({'valid': False,
                'reasons': ['semantic_scope_unresolved']})
            if step.get('recovery'):
                base['recovery'] = deepcopy(step['recovery'])
            return base
        proof_required = preparation_required or self._resolution_verified(step)
        if proof_required and step.get('constraints'):
            from .query_templates import runtime_binding_errors
            binding_errors = runtime_binding_errors(step)
            if binding_errors:
                base["validation"].append({"valid": False,
                    "reasons": ["query_binding_unverified", *binding_errors]})
                return base
        parameters, dependency_notes, inherited_partial = {}, [], False
        dependency_bindings = {}
        bounded_dependencies = []
        for index, dependency in enumerate(step.get("depends_on") or []):
            evidence = previous.get(dependency)
            if not evidence or evidence.get("status") not in {"complete", "partial", "empty"}:
                base["validation"].append({"valid": False, "reasons": ["dependency_unavailable:" + dependency]})
                return base
            ids = sorted({str(node["id"]) for node in evidence.get("nodes", []) if node.get("id") is not None})
            dependency_nodes = evidence.get('nodes', [])
            typed_binding = next((b for b in step.get('input_bindings', []) if b['step_id'] == dependency), None)
            if typed_binding:
                from .composable_planning import selected_nodes
                dependency_nodes = selected_nodes(evidence, typed_binding['entity_type'], typed_binding['source_role'])
                ids = sorted({str(n['id']) for n in dependency_nodes})
                if evidence.get('graph_version') != self.settings.graph_version:
                    raise GraphValidationError('dependency_graph_release_mismatch')
            if not typed_binding and step.get('relation_types') == ['PART_OF_GWAS_SIGNAL']:
                # A disease or gene node from a preceding check is not a GWAS
                # variant input. Never satisfy this dependency on disease alone.
                dependency_nodes = [n for n in dependency_nodes if 'variants' in (n.get('labels') or [])]
                if not dependency_nodes and evidence.get('graph_version') == self.settings.graph_version:
                    from .coloc_scope import _leads
                    leads = set().union(*[_leads((e.get('properties') or {}).get('gwas_lead_vars'))
                        for e in evidence.get('edges', []) if e.get('type') == 'SIGNAL_COLOC_WITH'])
                    if len(leads) > 8:
                        base['validation'].append({'valid':False,'reasons':['dependency_variant_resolution_limit']})
                        return base
                    for lead in sorted(leads):
                        binding = {'entity_type':'variants','property':'id','operator':'=','value':lead}
                        resolved = await self._resolve_constraint(binding, 0, step)
                        if resolved.get('state') != 'resolved' or 'variants' not in resolved.get('labels', []):
                            base['validation'].append({'valid':False,'reasons':['dependency_variant_identity_unresolved']})
                            return base
                        dependency_nodes.append({'id':resolved['id'],'labels':resolved['labels']})
                    if leads:
                        base.setdefault('dependency_inputs', []).append({'step_id':dependency,
                            'source_property':'SIGNAL_COLOC_WITH.gwas_lead_vars', 'identity_verified':True,
                            'scope':'recorded colocalization lead variants only', 'variant_count':len(leads)})
                ids = sorted({str(n['id']) for n in dependency_nodes if n.get('id') is not None})
            inherited_partial |= evidence.get("status") == "partial"
            if (evidence.get("status") == "partial" and evidence.get("truncated") is False
                    and ((evidence.get("requested_scope") or {}).get("complete") is False
                         or evidence.get("bounded_dependency_step_ids"))):
                bounded_dependencies.append(dependency)
            if not ids:
                if evidence.get('status') != 'empty' or any(evidence.get(key) for key in ('nodes', 'edges', 'rows')):
                    base['validation'].append({'valid': False, 'reasons': ['dependency_missing_entity_ids:' + dependency]})
                    return base
                base["status"] = "partial" if inherited_partial else "empty"
                base['execution_status'] = 'skipped_empty_dependency'
                base['retrieval_execution'] = {'completed': False, 'cursor_exhausted': False,
                                               'mode': 'dependency_inference'}
                base["validation"].append({"valid": True, "reasons": ["empty_dependency:" + dependency]})
                return base
            name = "dep_" + str(index)
            parameters[name] = ids
            if typed_binding and step.get('path_spec'):
                step.setdefault('_path_dependency_parameters', {})[name] = {
                    'ids': ids, 'graph_version': evidence['graph_version']}
            labels_by_id = {}
            for node in dependency_nodes:
                identifier, labels = node.get('id'), node.get('labels')
                if identifier is not None and isinstance(labels, list) and all(isinstance(label, str) and label for label in labels):
                    labels_by_id.setdefault(str(identifier), set()).update(labels)
            if evidence.get('graph_version') and evidence['graph_version'] == step.get('graph_version'):
                dependency_bindings[name] = {'graph_version': evidence['graph_version'],
                    'id_labels': {identifier: sorted(labels) for identifier, labels in labels_by_id.items()}}
            dependency_notes.append(f"Preserve the entities from step {dependency}: constrain the appropriate node's id IN ${name}; this parameter contains {len(ids)} existing graph IDs.")
        if bounded_dependencies:
            base['bounded_dependency_step_ids'] = bounded_dependencies
        # Verified local query routes do not need a model prompt. Construct it
        # only if generation is actually required; a model's input limit must
        # not prevent a fully typed template or cached query from being checked.
        question = None
        from .candidate_policy import CandidateBatch, retryable_generation_error, initial_request_count, grounded_prompt_variants
        grounded = getattr(self.settings, 'grounded_query_policy', False)
        from .planning_contract import VerifiedCache
        from .query_templates import compile_query, compile_variant_dependencies, compile_typed_dependencies, DIGEST as TEMPLATE_DIGEST
        from .bounded_paths import (BoundedPathError, DIGEST as BOUNDED_PATH_DIGEST,
                                    compile_query as compile_bounded_path)
        from .pattern_planning import (DIGEST as PATTERN_PLAN_DIGEST,
                                       is_verified_local_coloc_step)
        path_requested = step.get("path_spec") is not None
        local_coloc_required = is_verified_local_coloc_step(step)
        key = VerifiedCache.key({k:v for k,v in step.items() if k != 'resolution_key'}, parameters,
                                self.preview_identity(), TEMPLATE_DIGEST,
                                BOUNDED_PATH_DIGEST, PATTERN_PLAN_DIGEST,
                                CONTRACT_DIGEST)
        if not hasattr(self, '_query_cache'):
            self._query_cache = VerifiedCache()
        cached = self._query_cache.get(key) if grounded or path_requested or local_coloc_required else None
        try:
            if path_requested:
                template = compile_bounded_path(step)
            else:
                template = (((compile_typed_dependencies(step, dependency_bindings) or compile_variant_dependencies(step, dependency_bindings))
                             if dependency_notes else compile_query(step))
                            if grounded or local_coloc_required else None)
        except BoundedPathError as exc:
            reason = "unsupported_bounded_path_spec:" + (str(exc) or "compile_failed")
            base["validation"].append({"valid": False, "reasons": [reason]})
            base["error"] = {"category": "unsupported_bounded_path_spec",
                             "message": "The requested bounded path could not be compiled by the verified local route."}
            return base
        if local_coloc_required and not template and not cached:
            base["validation"].append({
                "valid": False,
                "reasons": ["verified_local_coloc_template_unavailable"],
            })
            base["error"] = {
                "category": "verified_local_coloc_template_unavailable",
                "message": "The deterministic colocalization check could not be compiled by its verified local template.",
            }
            return base
        routes = (['cache'] if cached else []) + (['template'] if template else [])
        if not path_requested and not local_coloc_required:
            routes += ['gpu_initial', 'gpu_repair', 'claude_repair'] if grounded else ['gpu_initial', 'gpu_sampling']
        if step.get('gpu_participation_required'):
            routes = ['gpu_initial'] + [r for r in routes if r != 'gpu_initial']
        for route in routes:
            local_route = route in {'template', 'cache'}
            template_audit = None
            if not local_route and question is None:
                try:
                    question = generation_request(step, build_generation_question(step))
                except ValueError as exc:
                    base["validation"].append({"valid": False, "route": route, "reasons": [str(exc)]})
                    if template or cached:
                        continue
                    return base
                if dependency_notes:
                    question += "\n" + "\n".join(dependency_notes)
                if len(question) > 4000:
                    base["validation"].append({"valid": False, "route": route,
                                               "reasons": ["generation_question_too_long"]})
                    if template or cached:
                        continue
                    return base
            n = 8 if route == 'gpu_sampling' else 1
            count = initial_request_count(self.settings, step) if route == 'gpu_initial' else 1
            await emit("progress", {"stage": "generating_cypher", "step_id": step.get("id"),
                                    "candidates_requested": n, "parallel_requests": count})
            attempt_question = ("verified-local-query:" + key) if local_route else question
            correction = ""
            if route in {'gpu_sampling', 'gpu_repair', 'claude_repair'}:
                failures = sorted({reason for check in base["validation"] for reason in check["reasons"]})
                correction = "\nCorrect the previous validation failures: " + ", ".join(reason for reason in failures) + ". Preserve every required filter and dependency."
                if step.get("complete", True):
                    correction += " Return all matches without LIMIT or list slices."
                if any(reason.startswith(("invalid_relation_property:", "unrequested_measurement_filter:")) for reason in failures):
                    correction += " GENE_ENRICHED_IN uses padj for adjusted p-value and rank_in_cell_type for rank; enrichment_score is not a supported field. Do not invent measurement thresholds."
                if step.get("semantic_registry") and not any(c.get("entity_type")=="disease" for c in step.get("constraints", [])):
                    clinical = [c for c in step.get('constraints', []) if c.get('entity_type') == 'donor'
                                and c.get('property') in {'diabetes_type', 'derived_diabetes_status'}]
                    correction += " No disease-node identity filter was requested. Do not constrain disease.id or disease.name."
                    correction += (" Apply the verified donor clinical predicates exactly as supplied."
                                   if clinical else
                                   " No diagnosed-diabetes donor predicate was requested; do not add donor.diabetes_type or donor.derived_diabetes_status.")
                    correction += " Use only the resolved donor stage/cohort and sample/tissue constraints."
                if len(question) + len(correction) <= 4000:
                    attempt_question += correction
            selected_parameters = {**parameters, **(template["parameters"] if path_requested and template else {})}
            generate = self._generate
            if route in {'template', 'cache'}:
                prepared_query = template if route == 'template' else cached
                selected_parameters = {**parameters, **prepared_query['parameters']}
                template_audit = {key: deepcopy(prepared_query[key]) for key in
                    ('template_id', 'version', 'sha256', 'schema_sha256',
                     'endpoint_coverage', 'parameter_bindings') if key in prepared_query}
                async def generate(_question, _n):
                    return [prepared_query['cypher']]
            elif route == 'gpu_repair':
                previous_query = next((v.get('original_candidate_cypher') for v in reversed(base['validation']) if v.get('original_candidate_cypher')), '')
                note = '\nPrevious candidate to correct: ' + previous_query
                if len(question) + len(correction) + len(note) > 4000:
                    base['validation'].append({'valid': False, 'route': route,
                        'reasons': ['gpu_repair_context_too_large'], 'skipped': True})
                    continue
                attempt_question = question + correction + note
            elif route == 'claude_repair':
                if not callable(getattr(self, 'query_repair', None)):
                    continue
                previous_query = next((v.get('original_candidate_cypher') for v in reversed(base['validation']) if v.get('original_candidate_cypher')), '')
                async def generate(_question, _n):
                    return await self.query_repair(step, question, failures, previous_query)
            deadline = min(30, getattr(self.settings, "cypher_timeout", 15) * (2 if n == 8 else 1)) + 1
            prompts = grounded_prompt_variants(attempt_question, count) if grounded and route == 'gpu_initial' else None
            if prompts is not None:
                count = len(prompts)
            async with CandidateBatch(generate, attempt_question, n, count=count,
                                      timeout=deadline, attempts=base["generator_attempts"],
                                      prompts=prompts, completion_order=bool(prompts and count > 1),
                                      capacity=getattr(self.settings, 'cypher_generation_concurrency', 4),
                                      slots=None if route.startswith('gpu_') else asyncio.BoundedSemaphore(1),
                                      route=route) as batch:
                async for outcome in batch:
                    outcome.attempt["route"] = route
                    if outcome.error is not None:
                        exc = outcome.error
                        reasons = [str(exc)] if isinstance(exc, GraphValidationError) else ["generation_unavailable:" + type(exc).__name__]
                        base["validation"].append({"valid": False, "n": n,
                                                   "attempt_index": outcome.attempt["attempt_index"], "reasons": reasons})
                        if not isinstance(exc, GraphValidationError) and not retryable_generation_error(exc) and not (template or cached):
                            return base
                        continue
                    for query in outcome.candidates:
                        original_query = query
                        validation_started = time.monotonic()
                        from .release_schema import canonicalize_symbols
                        query, normalizations = canonicalize_symbols(query) if getattr(self.settings, "graph_version", None) == "PanKgraph_08_04" else (query, [])
                        await emit("progress", {"stage": "validating", "step_id": step.get("id")})
                        from .categorical_bindings import bind_verified_categories
                        query, candidate_parameters, normalization = bind_verified_categories(query, step, selected_parameters)
                        repairs, repaired = [], None
                        if grounded:
                            from .cypher_repair import repair_candidate
                            repaired = repair_candidate(query, graph_release=step.get("graph_version"))
                            query, repairs = repaired["query"], repaired["transformations"]
                        reasons = validate_cypher(query, step, candidate_parameters, dependency_bindings=dependency_bindings)
                        if not reasons:
                            reasons = await self._explain(query, candidate_parameters)
                        from .cypher_repair import failure_categories
                        categories = failure_categories(reasons)
                        validation_ms = round((time.monotonic() - validation_started) * 1000, 3)
                        validation_record = {"valid": not reasons, "n": n,
                            "attempt_index": outcome.attempt["attempt_index"],
                            "candidate_cypher": query, "original_candidate_cypher": original_query,
                            "schema_normalizations": normalizations, "categorical_normalizations": normalization,
                            "deterministic_repairs": repairs, "deterministic_repair_record": repaired,
                            "route": route, "validation_ms": validation_ms,
                            "failure_categories": categories, "reasons": reasons}
                        if template_audit:
                            validation_record["template_audit"] = deepcopy(template_audit)
                        base["validation"].append(validation_record)
                        # Full candidates and repair provenance remain in this
                        # protected evidence record. Operational events contain
                        # only coarse failure classes and duration, never Cypher.
                        from .audit import provider_event
                        try:
                            provider_event('cypher_validation', {'cause_categories': categories,
                                'route': route, 'latency_ms': validation_ms, 'valid': not reasons})
                        except Exception as exc:
                            base.setdefault('telemetry_failures', []).append({
                                'event': 'cypher_validation', 'category': type(exc).__name__})
                        if local_coloc_required and route.startswith('gpu_'):
                            validation_record['candidate_only'] = True
                            continue
                        if reasons:
                            continue
                        await emit("progress", {"stage": "querying_graph", "step_id": step.get("id")})
                        limits = {
                            "known_node_ids": {str(node["id"]) for item in previous.values() for node in item.get("nodes", [])},
                            "known_edge_keys": {json.dumps(edge, sort_keys=True, separators=(",", ":")) for item in previous.values() for edge in item.get("edges", [])},
                            "used_bytes": sum(item.get("materialized_bytes", 0) for item in previous.values()),
                            "used_rows": sum(len(item.get("rows", [])) for item in previous.values()),
                            "max_step_nodes": min(20 if step.get("purpose") == "context" else self.settings.max_nodes, (step.get("retrieval_budget") or {}).get("max_nodes", self.settings.max_nodes)),
                            "max_step_bytes": (step.get("retrieval_budget") or {}).get("max_bytes", getattr(self.settings, "max_bytes", 2_000_000)),
                            "max_step_edges": (step.get("retrieval_budget") or {}).get("max_edges", getattr(self.settings, "max_edges", 5000)),
                            "max_step_rows": (step.get("retrieval_budget") or {}).get("max_rows", getattr(self.settings, "max_rows", 1000)),
                            # Only an already-validated deterministic bounded
                            # path may use atomic aggregate-prefix handling.
                            "bounded_path_records": path_requested,
                        }
                        retrieval_started = time.monotonic()
                        query_record = {"cypher": query, "parameters": candidate_parameters,
                                        "normalization": normalization}
                        if template_audit:
                            query_record["template_audit"] = deepcopy(template_audit)
                        base["queries"].append(query_record)
                        try:
                            result = await asyncio.wait_for(self._retrieve(query, candidate_parameters, limits), timeout=self.settings.graph_timeout + 1)
                        except Exception as exc:
                            base["validation"].append({"valid": False, "reasons": ["graph_execution_failed:" + type(exc).__name__]})
                            return base
                        outcome.attempt["selected"] = True
                        outcome.attempt["selected_query_sha256"] = hashlib.sha256(query.encode()).hexdigest()
                        base.update(result)
                        base["retrieval_ms"] = round((time.monotonic()-retrieval_started)*1000,3)
                        base["query_route"] = route
                        if template_audit:
                            base["query_template"] = deepcopy(template_audit)
                        if path_requested:
                            from .bounded_paths import (MAX_PATH_RECORDS,
                                derive_chain_facts, extract_path_records)
                            raw_rows = base.get("rows") or []
                            raw_records = (raw_rows[0].get("path_records")
                                           if len(raw_rows) == 1 and isinstance(raw_rows[0], dict)
                                           else None)
                            overfetch = (isinstance(raw_records, list)
                                         and len(raw_records) > MAX_PATH_RECORDS)
                            if overfetch:
                                retained = raw_records[:MAX_PATH_RECORDS]
                                base["rows"] = [{"path_records": retained}]
                                retained_node_ids = {
                                    wrapper.get("node_id")
                                    for record in retained if isinstance(record, dict)
                                    for wrapper in (record.get("nodes") or [])
                                    if isinstance(wrapper, dict) and isinstance(wrapper.get("node_id"), str)}
                                retained_edge_fingerprints = {
                                    wrapper.get("fingerprint")
                                    for record in retained if isinstance(record, dict)
                                    for wrapper in (record.get("edges") or [])
                                    if isinstance(wrapper, dict) and isinstance(wrapper.get("fingerprint"), str)}
                                base["nodes"] = [node for node in base.get("nodes") or []
                                                 if str(node.get("id")) in retained_node_ids]
                                filtered_edges, seen_fingerprints = [], set()
                                for edge in base.get("edges") or []:
                                    fingerprint = _public_edge_fingerprint(edge)
                                    if (fingerprint in retained_edge_fingerprints
                                            and fingerprint not in seen_fingerprints):
                                        seen_fingerprints.add(fingerprint)
                                        filtered_edges.append(edge)
                                base["edges"] = filtered_edges
                                base["status"], base["truncated"] = "partial", True
                                base.setdefault("retrieval_execution", {}).update(
                                    cursor_exhausted=False,
                                    path_record_limit_reached=True,
                                    retained_path_records=MAX_PATH_RECORDS)
                                base["validation"].append({
                                    "valid": True, "route": route,
                                    "status": "partial",
                                    "reasons": ["bounded_path_record_limit"],
                                })
                            try:
                                if ((base.get("nodes") or base.get("edges"))
                                        and not raw_records):
                                    raise BoundedPathError(
                                        "missing_path_records_for_materialized_graph")
                                path_records = extract_path_records(
                                    step, base.get("rows") or [], base.get("nodes") or [])
                            except BoundedPathError as exc:
                                reason = "invalid_bounded_path_evidence:" + (str(exc) or "materialization_failed")
                                base["status"] = "failed"
                                base["validation"].append({"valid": False,
                                                           "route": route,
                                                           "reasons": [reason]})
                                base["error"] = {"category": "invalid_bounded_path_evidence",
                                                 "message": "The bounded path result did not preserve its verified node and relationship roles."}
                                return base
                            base["path_records"] = path_records
                            if overfetch:
                                base["retrieval_execution"]["exposed_path_records"] = len(path_records)
                            base["chain_facts"] = derive_chain_facts(
                                step, path_records, base.get("nodes") or [],
                                status=base.get("status"),
                                truncated=base.get("truncated", False))
                            # The aggregate wrapper is an internal transport
                            # record.  Preserve its materialization accounting,
                            # but expose only role-preserving path evidence.
                            base["rows"] = []
                        if (grounded or path_requested or local_coloc_required) and base.get("status") in {"complete", "empty"} and not base.get("truncated"):
                            cached_query = {"cypher":query,"parameters":candidate_parameters}
                            if template_audit:
                                cached_query.update(deepcopy(template_audit))
                            self._query_cache.put(key, cached_query)
                        from .semantic_registry import donor_summary
                        base['resolved_constraints']=step.get('resolved_constraints',[])
                        base['semantic_registry']=step.get('semantic_registry')
                        summary=donor_summary(base)
                        if summary:base['donor_summary']=summary
                        if inherited_partial or not step.get("complete", True):
                            base["status"] = "partial"
                        sources = set()
                        for item in base["nodes"] + base["edges"]:
                            for key in ("data_source", "data_source_url", "data_version", "source", "provenance", "publication_source"):
                                val = item["properties"].get(key)
                                if val is not None:
                                    sources.add((key, json.dumps(val, sort_keys=True)))
                        base["provenance"] = [{"property": key, "value": json.loads(val)} for key, val in sorted(sources)]
                        # Record retrieval scope before either synthesis-context
                        # reduction or graph display selection. Source-analysis
                        # comparisons remain distinct from this database query.
                        from .evidence_coverage import build_evidence_coverage
                        base["evidence_coverage"] = build_evidence_coverage(
                            step, base, graph_version=self.settings.graph_version,
                            query=query, parameters=candidate_parameters, validation_verified=True,
                        )
                        from .coloc_records import derive_colocalization_records
                        coloc = derive_colocalization_records(base)
                        if coloc is not None:
                            base["colocalization_record_version"] = coloc["version"]
                            base["colocalization_records"] = coloc["records"]
                            base["colocalization_record_links"] = coloc["record_links"]
                            base["colocalization_signal_counts"] = coloc["counts"]
                            base["colocalization_record_derivation"] = coloc["derivation"]
                            base["colocalization_record_completeness"] = coloc["completeness"]
                            base["colocalization_record_interpretation"] = coloc["interpretation"]
                        return base
        return base
