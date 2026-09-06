"""Bounded Cypher generation and read-only retrieval against one pinned graph.

The generator executes queries internally. These guards protect this service's
downstream execution; they cannot change the shared generator's own privileges.
Semantic guards enforce explicit plan constraints, not scientific correctness.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from neo4j import AsyncGraphDatabase, READ_ACCESS
from neo4j.graph import Node, Path as Neo4jPath, Relationship


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


def _predicate_present(tokens: list[Token], constraint: dict, parameters: dict) -> bool:
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


def _unrequested_identity_filters(tokens: list[Token], constraints: list[dict], parameters: dict) -> list[str]:
    """Catch invented identifier/entity restrictions on complete set queries."""
    errors = []
    for i, token in enumerate(tokens):
        if token.kind not in {"WORD", "IDENT"}:
            continue
        prop = token.value
        if not (prop.lower().endswith(("name", "_id")) or prop.lower() in {"id", "condition", "gender", "sex", "t1d_stage"}):
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
    for constraint in constraints:
        if not all(_predicate_present(part, constraint, parameters) for part in branches):
            errors.append("missing_required_filter:" + str(constraint.get("property", "unknown")))
    for name in dependencies:
        # Require an actual bounded id predicate in every UNION arm. Mentioning
        # a dependency parameter in a comment or RETURN does not preserve it.
        wanted = {"property": "id", "operator": "IN", "value": parameters[name]}
        if not all(_predicate_present(part, wanted, parameters) for part in branches):
            errors.append("missing_dependency:" + name)
    if step.get("complete", True):
        errors.extend(_unrequested_identity_filters(tokens, constraints, parameters))
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
            return queries
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
            if budget_key not in seen and len(seen) >= maximum or size + width > byte_limit:
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
                        rows.append(row)
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
        base = {"step_id": step.get("id"), "question": step.get("question"), "graph_version": self.settings.graph_version,
                "nodes": [], "edges": [], "rows": [], "queries": [], "validation": [],
                "truncated": False, "status": "failed", "provenance": []}
        limits = {
            "known_node_ids": {str(node["id"]) for item in previous.values() for node in item.get("nodes", [])},
            "known_edge_keys": {json.dumps(edge, sort_keys=True, separators=(",", ":")) for item in previous.values() for edge in item.get("edges", [])},
            "used_bytes": sum(item.get("materialized_bytes", 0) for item in previous.values()),
            "used_rows": sum(len(item.get("rows", [])) for item in previous.values()),
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
        question = str(step.get("question", ""))
        if dependency_notes:
            question += "\n" + "\n".join(dependency_notes)
        if len(question) > 4000:
            base["validation"].append({"valid": False, "reasons": ["generation_question_too_long"]})
            return base
        for n in (1, 8):
            await emit("progress", {"stage": "generating_cypher", "step_id": step.get("id"), "candidates_requested": n})
            try:
                attempt_question = question
                if n == 8:
                    failures = sorted({reason for check in base["validation"] for reason in check["reasons"]})
                    correction = "\nCorrect the previous validation failures: " + ", ".join(reason.split(":", 2)[0] for reason in failures) + ". Preserve every required filter and dependency."
                    if step.get("complete", True):
                        correction += " Return all matches without LIMIT or list slices."
                    if len(question) + len(correction) <= 4000:
                        attempt_question += correction
                candidates = await self._generate(attempt_question, n)
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
                base["queries"].append({"cypher": query, "parameters": parameters})
                try:
                    result = await asyncio.wait_for(self._retrieve(query, parameters, limits), timeout=self.settings.graph_timeout + 1)
                except Exception as exc:
                    base["validation"].append({"valid": False, "reasons": ["graph_execution_failed:" + type(exc).__name__]})
                    return base
                base.update(result)
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
