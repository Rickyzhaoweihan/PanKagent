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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from neo4j import AsyncGraphDatabase, READ_ACCESS
from neo4j.graph import Node, Path as Neo4jPath, Relationship
from .graph_contract import DIGEST as CONTRACT_DIGEST, RELATIONS, MEASUREMENTS, generation_request
from .plan_constraints import (CELL_TYPES, build_generation_question, related_context_step,
                               repair_step_constraints, resolved_lookup, step_relation_types)


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


def tokenize(query: str) -> list[Token]:
    """Lex strings, quoted identifiers and comments before checking keywords."""
    out, i = [], 0
    while i < len(query):
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
    # Structured planners encode heterogeneous constraint values as strings.
    if isinstance(expected, str):
        if operator == "IN":
            try:
                decoded = json.loads(expected)
                if isinstance(decoded, list):
                    return decoded
            except (ValueError, TypeError):
                pass
        if isinstance(actual, (int, float)) and not isinstance(actual, bool):
            try:
                return float(expected)
            except ValueError:
                pass
        if isinstance(actual, bool) and expected.lower() in {"true", "false"}:
            return expected.lower() == "true"
    return expected


def _pattern_bindings(tokens: list[Token]):
    """Read mandatory node/edge pattern bindings; never infer from prose/literals."""
    nodes, patterns, edges = {}, [], []
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
        if "[" not in values or "]" not in values or any(token.kind == "SYMBOL" and token.value in {"|", "*"} for token in between):
            continue
        kinds = {between[i + 1].value for i, token in enumerate(between[:-1])
                 if token.value == ":" and between[i + 1].kind in {"WORD", "IDENT"}}
        if kinds:
            source, target = (right[2], left[2]) if values[0] == "<" else (left[2], right[2])
            edges.append((source, target, kinds))
            if values[0] != "<" and values[-1] != ">":
                edges.append((target, source, kinds))
    # Preserve node types through simple WITH aliases; never infer a property projection as a node.
    for i, token in enumerate(tokens[1:-1], 1):
        if _word(token, "AS") and tokens[i-1].value in nodes and (i < 2 or tokens[i-2].value != '.'):
            old,new=tokens[i-1].value,tokens[i+1].value
            nodes.setdefault(new, set()).update(nodes[old])
            edges += [(new if a==old else a, new if b==old else b, kinds) for a,b,kinds in list(edges) if old in (a,b)]
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
    prop = str(constraint.get("property", "")).split(".")[-1]
    expected_operator = str(constraint.get("operator", "=")).upper()
    expected = constraint.get("value")
    if not prop or expected_operator not in {"=", "IN", "CONTAINS", "STARTS WITH", "ENDS WITH", ">", ">=", "<", "<="}:
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
        wanted = _normalized_expected(expected, actual, expected_operator)
        if transform and isinstance(wanted, str):
            wanted = wanted.lower() if transform == "tolower" else wanted.upper()
        if operator == expected_operator and _equal(actual, wanted):
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


def _unrequested_identity_filters(tokens: list[Token], constraints: list[dict], parameters: dict, extra_properties=()) -> list[str]:
    """Catch invented identifier/entity restrictions on complete set queries."""
    errors = []
    bindings, _ = _pattern_bindings(tokens)
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
        if operator not in {"=", "IN", "CONTAINS", "STARTS WITH", "ENDS WITH"}:
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
            if wanted_operator == operator and _equal(actual, expected):
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


def _choice_present(tokens, choice, parameters):
    bindings, _ = _pattern_bindings(tokens)
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


def validate_cypher(query: str, step: dict, parameters: dict | None = None) -> list[str]:
    parameters = parameters or {}
    if not isinstance(query, str) or not query.strip() or len(query) > 24000:
        return ["missing_or_oversized_cypher"]
    try:
        tokens = tokenize(query)
    except GraphValidationError as exc:
        return [str(exc)]
    if tokens and tokens[-1].value == ";" and tokens[-1].kind == "SYMBOL":
        tokens = tokens[:-1]
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
    if step.get("complete", True) and (slices or any(
        t.kind == "WORD" and t.value.upper() in {"LIMIT", "SKIP", "RAND"} for t in tokens
    )):
        errors.append("incomplete_limit_or_slice")
    constraints = list(step.get("constraints") or [])
    dependencies = [name for name in parameters if name.startswith("dep_")]
    if (constraints or dependencies) and any(_word(t, "OR") or _word(t, "XOR") or _word(t, "NOT") for t in tokens):
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
    choices = [_constraint_choices(step, index, constraint) for index, constraint in enumerate(constraints)]
    for constraint, alternatives in zip(constraints, choices):
        if not all(any(_choice_present(part, choice, parameters) for choice in alternatives) for part in branches):
            errors.append("missing_required_filter:" + str(constraint.get("property", "unknown")))
    relations = step_relation_types(step)
    for part in branches:
        if step.get("evidence_combination", "independent") == "independent":
            _, bindings = _pattern_bindings(part)
            measurement_paths = [path for path in bindings if path[2] & MEASUREMENTS]
            if len({kind for path in measurement_paths for kind in path[2] & MEASUREMENTS}) > 1:
                errors.append("independent_measurements_require_separate_steps")
    for part in branches:
        _, paths = _pattern_bindings(part)
        errors.extend(_enrichment_property_errors(part, step, parameters))
        bindings, _ = _pattern_bindings(part)
        from .semantic_registry import validation_errors
        errors.extend(validation_errors(part, step, parameters, bindings, paths, _predicate_present, choices))
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
                bindings, _ = _pattern_bindings(part)
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
    if step.get("complete", True):
        from .semantic_registry import PROPERTIES
        extra={p for fields in PROPERTIES.values() for p in fields} if step.get('semantic_registry') else set()
        errors.extend(_unrequested_identity_filters(tokens, [choice for group in choices for choice in group], parameters, extra))
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
        path = Path(getattr(self.settings, "graph_identity_file", ""))
        manifest_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        return {"graph_version": self.settings.graph_version, "identity_manifest_sha256": manifest_hash,
                "identity_verified": self.identity_verified,
                **{key: getattr(self.settings, key, None) for key in (
                    "neo4j_uri", "neo4j_database", "cypher_url", "max_nodes", "max_edges", "max_rows",
                    "max_bytes", "graph_timeout", "cypher_timeout")}}

    def _resolution_signature(self, step: dict) -> str:
        if not hasattr(self, "_resolution_secret"):
            self._resolution_secret = secrets.token_bytes(32)
        payload = {key: value for key, value in step.items() if key != "resolution_key"}
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
            return constraint["entity_type"]
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

    async def _resolve_constraint(self, constraint: dict, index: int, step: dict) -> dict:
        entry = {"constraint_index": index, "requested": dict(constraint), "state": "unsupported",
                 "graph_version": self.settings.graph_version, "labels": []}
        prop, value = str(constraint.get("property", "")).split(".")[-1], constraint.get("value")
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
        cached = cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            return {**deepcopy(cached[1]), "constraint_index": index, "requested": dict(constraint)}
        node = f"(n:`{label}`)" if label else "(n)"
        query = f"MATCH {node} WHERE n.`{prop}` = $value RETURN n.id AS id, n.name AS name, labels(n)[..16] AS labels LIMIT 3"
        timeout = min(self.settings.graph_timeout, 3)
        rows = await asyncio.wait_for(self._small_query(query, {"value": value}), timeout)
        if not rows and prop == "name":
            query = f"MATCH {node} WHERE toLower(n.name) = toLower($value) RETURN n.id AS id, n.name AS name, labels(n)[..16] AS labels LIMIT 3"
            rows = await asyncio.wait_for(self._small_query(query, {"value": value}), timeout)
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
            canonical_type = label or next((kind for kind in ("Gene", "anatomical_structure", "disease", "donor", "variants", "GO_term", "reactome", "kegg", "Sample_node", "data_modality") if kind in candidate["labels"]), None)
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

    async def semantic_vocabulary(self):
        from .semantic_registry import DIGEST
        key=(self.settings.graph_version,DIGEST)
        if not hasattr(self, '_semantic_lock'): self._semantic_lock=asyncio.Lock()
        async with self._semantic_lock:
            cached=getattr(self,'_semantic_cache',None)
            if cached and cached[0]==key and time.monotonic()-cached[1]<300:return cached[2]
            rows=await self._small_query("MATCH (d:donor) RETURN collect(DISTINCT d.t1d_stage) AS stages, collect(DISTINCT d.data_source) AS sources")
            modalities=await self._small_query("MATCH (s:Sample_node) RETURN collect(DISTINCT s.data_modality) AS modalities")
            check=await self._small_query("MATCH (m:data_modality)-[:HAS_SAMPLE]->(s:Sample_node) RETURN count(CASE WHEN m.id <> s.data_modality OR s.data_modality IS NULL THEN 1 END) AS mismatches, count(*) AS links")
            tissues=await self._small_query("MATCH (a:anatomical_structure)-[:HAS_SAMPLE]->(:Sample_node) RETURN DISTINCT a.id AS id, a.name AS name LIMIT 2000")
            value={'tissues':tissues,**(rows[0] if rows else {}),**(modalities[0] if modalities else {}), 'modality_links_verified':bool(check and check[0]['links'] and check[0]['mismatches']==0)}
            self._semantic_cache=(key,time.monotonic(),value)
            return value

    async def _prepare_step(self, source: dict, emit) -> dict:
        step = repair_step_constraints({key: value for key, value in source.items()
                                        if key not in {"resolution_key", "resolved_entities", "entity_resolution"}})
        from .semantic_registry import donor_intent, resolve
        if donor_intent(step):
            step=resolve(step,await self.semantic_vocabulary(),self.settings.graph_version)
        step["graph_version"] = self.settings.graph_version
        step["relation_types"] = step_relation_types(step)
        identities = [(index, constraint) for index, constraint in enumerate(step.get("constraints") or [])
                      if str(constraint.get("property", "")).split(".")[-1] in {"name", "id"}]
        if len(identities) > 8:
            raise GraphValidationError("too_many_entity_constraints")
        entities = []
        for index, constraint in identities:
            await emit("progress", {"stage": "resolving_entities", "step_id": step.get("id")})
            entities.append(await self._resolve_constraint(constraint, index, step))
        step["resolved_entities"] = entities
        unresolved = [item for item in entities if item["state"] not in {"resolved", "literal_predicate"}]
        unknown_relations = [kind for kind in step["relation_types"] if getattr(self, "release_relations", set()) and kind not in self.release_relations]
        step["entity_resolution"] = {"state": "needs_clarification" if unresolved or unknown_relations or step.get("semantic_issues") else "resolved" if entities else "not_required",
                                     "graph_version": self.settings.graph_version, "unknown_relations": unknown_relations}
        step["resolution_key"] = self._resolution_signature(step)
        return step

    async def prepare_plan(self, plan: dict, emit) -> dict:
        """Resolve bounded entity identities and expose one context step for review."""
        await self._ensure_identity()
        if len(plan.get("steps") or []) > 3:
            raise GraphValidationError("plan_too_large")
        prepared = {**plan, "steps": []}
        for source in plan.get("steps") or []:
            prepared["steps"].append(await self._prepare_step(source, emit))
        if any('scRNA-seq' in group and 'snMultiomics' in group for step in prepared['steps'] for group in step.get('sample_requirements',{}).get('modality_groups',[])):
            interpretation=prepared.get('interpreted_question') or prepared['steps'][0]['question']
            note=' Include documented RNA components of HPAP multiome assays, retaining their original assay labels.'
            if note.strip() not in interpretation:prepared['interpreted_question']=interpretation+note
        context = related_context_step(prepared)
        if context:
            prepared["steps"].append(await self._prepare_step(context, emit))
        issues = [{"step_id": step["id"], "entities": [item for item in step["resolved_entities"] if item["state"] not in {"resolved", "literal_predicate"}],
                   "unknown_relations": step["entity_resolution"]["unknown_relations"], "terminology":step.get("semantic_issues",[])}
                  for step in prepared["steps"] if step["entity_resolution"]["state"] == "needs_clarification"]
        prepared["entity_resolution"] = {"state": "needs_clarification" if issues else "resolved",
                                          "graph_version": self.settings.graph_version, "issues": issues}
        if issues:
            prepared["clarification"] = "Some requested entities or relationship types could not be uniquely verified in this graph release. Review the indicated names or IDs and revise the plan."
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
        limits = limits or {}
        seen_nodes = set(limits.get("known_node_ids", []))
        seen_edges = set(limits.get("known_edge_keys", []))
        byte_limit = max(0, self.settings.max_bytes - limits.get("used_bytes", 0))
        row_limit = max(0, getattr(self.settings, "max_rows", 1000) - limits.get("used_rows", 0))

        def put(target: dict, key, value, maximum, seen, budget_key=None):
            nonlocal size, truncated
            if key in target:
                return
            budget_key = key if budget_key is None else budget_key
            width = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
            if (budget_key not in seen and len(seen) >= maximum or size + width > byte_limit
                    or target is nodes and len(nodes) >= limits.get("max_step_nodes", self.settings.max_nodes)):
                truncated = True
                return
            target[key] = value
            seen.add(budget_key)
            size += width

        def walk(value):
            if isinstance(value, Node):
                nid = _node_id(value)
                put(nodes, nid, {"id": nid, "labels": sorted(value.labels), "properties": _safe_value(dict(value))}, self.settings.max_nodes, seen_nodes)
                return {"node_id": nid}
            if isinstance(value, Relationship):
                walk(value.start_node)
                walk(value.end_node)
                edge = {"start_id": _node_id(value.start_node), "end_id": _node_id(value.end_node),
                        "type": value.type, "properties": _safe_value(dict(value))}
                if edge["start_id"] in nodes and edge["end_id"] in nodes:
                    # An internal ID deduplicates repeated result paths but is
                    # intentionally absent from the stable public edge shape.
                    put(edges, value.element_id, edge, self.settings.max_edges, seen_edges,
                        json.dumps(edge, sort_keys=True, separators=(",", ":")))
                return {"edge": [edge["start_id"], edge["type"], edge["end_id"]]}
            if isinstance(value, Neo4jPath):
                for node in value.nodes:
                    walk(node)
                for edge in value.relationships:
                    walk(edge)
                return {"path": [_node_id(node) for node in value.nodes]}
            if isinstance(value, Mapping):
                return {str(k): walk(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [walk(item) for item in value]
            return _safe_value(value)

        async with self._session() as session:
            async with await session.begin_transaction(timeout=self.settings.graph_timeout) as tx:
                result = await tx.run(query.rstrip().rstrip(";"), parameters)
                async for record in result:
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
                "status": "partial" if truncated else "complete" if nodes or rows else "empty"}

    async def execute(self, step: dict, previous: dict, emit) -> dict:
        # Older confirmed plans may name a cell in prose but omit its predicate.
        # Recover only the narrow, verified entity constraint; guards stay strict.
        step = repair_step_constraints(step)
        base = {"step_id": step.get("id"), "question": step.get("question"), "graph_version": self.settings.graph_version,
                "nodes": [], "edges": [], "rows": [], "queries": [], "validation": [],
                "truncated": False, "status": "failed", "provenance": [], "contract_sha256": CONTRACT_DIGEST, "generator_attempts": [], "retry_eligible": False,
                "requested_scope": {"constraints": step.get("constraints", []), "relation_types": step.get("relation_types", []), "complete": step.get("complete", True)},
                **{key: step[key] for key in ("title", "purpose", "context_for", "rationale") if key in step}}
        limits = {
            "known_node_ids": {str(node["id"]) for item in previous.values() for node in item.get("nodes", [])},
            "known_edge_keys": {json.dumps(edge, sort_keys=True, separators=(",", ":")) for item in previous.values() for edge in item.get("edges", [])},
            "used_bytes": sum(item.get("materialized_bytes", 0) for item in previous.values()),
            "used_rows": sum(len(item.get("rows", [])) for item in previous.values()),
            "max_step_nodes": 20 if step.get("purpose") == "context" else self.settings.max_nodes,
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
        if "resolved_entities" in step:
            if not self._resolution_verified(step):
                step = await self._prepare_step(step, emit)
            base["resolved_entities"] = step["resolved_entities"]
            if step["entity_resolution"]["state"] == "needs_clarification":
                base["validation"].append({"valid": False, "reasons": ["unresolved_plan_entities"]})
                return base
        parameters, dependency_notes, inherited_partial = {}, [], False
        for index, dependency in enumerate(step.get("depends_on") or []):
            evidence = previous.get(dependency)
            if not evidence or evidence.get("status") not in {"complete", "partial", "empty"}:
                base["validation"].append({"valid": False, "reasons": ["dependency_unavailable:" + dependency]})
                return base
            ids = sorted({str(node["id"]) for node in evidence.get("nodes", []) if node.get("id") is not None})
            inherited_partial |= evidence.get("status") == "partial"
            if not ids:
                base["status"] = "partial" if inherited_partial else "empty"
                base["validation"].append({"valid": True, "reasons": ["empty_dependency:" + dependency]})
                return base
            name = "dep_" + str(index)
            parameters[name] = ids
            dependency_notes.append(f"Preserve the entities from step {dependency}: constrain the appropriate node's id IN ${name}; this parameter contains {len(ids)} existing graph IDs.")
        try:
            question = generation_request(step, build_generation_question(step))
        except ValueError as exc:
            base["validation"].append({"valid": False, "reasons": [str(exc)]})
            return base
        if dependency_notes:
            question += "\n" + "\n".join(dependency_notes)
        if len(question) > 4000:
            base["validation"].append({"valid": False, "reasons": ["generation_question_too_long"]})
            return base
        for n in (1, 8):
            base["generator_attempts"].append({"n": n})
            await emit("progress", {"stage": "generating_cypher", "step_id": step.get("id"), "candidates_requested": n})
            try:
                attempt_question = question
                if n == 8:
                    failures = sorted({reason for check in base["validation"] for reason in check["reasons"]})
                    correction = "\nCorrect the previous validation failures: " + ", ".join(reason for reason in failures) + ". Preserve every required filter and dependency."
                    if step.get("complete", True):
                        correction += " Return all matches without LIMIT or list slices."
                    if any(reason.startswith(("invalid_relation_property:", "unrequested_measurement_filter:")) for reason in failures):
                        correction += " GENE_ENRICHED_IN uses padj for adjusted p-value and rank_in_cell_type for rank; enrichment_score is not a supported field. Do not invent measurement thresholds."
                    if len(question) + len(correction) <= 4000:
                        attempt_question += correction
                if not hasattr(self, "_generation_slots"):
                    self._generation_slots = asyncio.Semaphore(2)
                async with self._generation_slots:
                    candidates = await self._generate(attempt_question, n)
                base["generator_attempts"][-1].update(
                    request_sha256=hashlib.sha256(attempt_question.encode()).hexdigest(),
                    candidate_count=len(candidates),
                    reported_identity=getattr(candidates, "metadata", {}))
            except GraphValidationError as exc:
                base["validation"].append({"valid": False, "n": n, "reasons": [str(exc)]})
                continue
            except Exception as exc:
                base["validation"].append({"valid": False, "n": n, "reasons": ["generation_unavailable:" + type(exc).__name__]})
                return base
            for query in candidates:
                await emit("progress", {"stage": "validating", "step_id": step.get("id")})
                reasons = validate_cypher(query, step, parameters)
                if not reasons:
                    reasons = await self._explain(query, parameters)
                base["validation"].append({"valid": not reasons, "n": n, "candidate_cypher": query, "reasons": reasons})
                if reasons:
                    continue
                await emit("progress", {"stage": "querying_graph", "step_id": step.get("id")})
                limits = {
                    "known_node_ids": {str(node["id"]) for item in previous.values() for node in item.get("nodes", [])},
                    "known_edge_keys": {json.dumps(edge, sort_keys=True, separators=(",", ":")) for item in previous.values() for edge in item.get("edges", [])},
                    "used_bytes": sum(item.get("materialized_bytes", 0) for item in previous.values()),
                    "used_rows": sum(len(item.get("rows", [])) for item in previous.values()),
                    "max_step_nodes": 20 if step.get("purpose") == "context" else self.settings.max_nodes,
                }
                base["queries"].append({"cypher": query, "parameters": parameters})
                try:
                    result = await asyncio.wait_for(self._retrieve(query, parameters, limits), timeout=self.settings.graph_timeout + 1)
                except Exception as exc:
                    base["validation"].append({"valid": False, "reasons": ["graph_execution_failed:" + type(exc).__name__]})
                    return base
                base.update(result)
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
                return base
        return base
