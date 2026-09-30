"""Official Neo4j driver with an independent, procedure-free read boundary."""
import json
import math
import re

from cakg.identity import ID_RE
from fastapi import HTTPException
from neo4j import GraphDatabase, Query, READ_ACCESS
from neo4j.graph import Node, Relationship, Path


def validate_cypher(cypher, parameters):
    if not cypher.strip() or len(cypher) > 20_000 or "\\u" in cypher or "\\U" in cypher:
        raise HTTPException(422, "Unsupported or oversized Cypher")
    # Mask strings, escaped identifiers and comments before inspecting clauses.
    token = re.compile(r"'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"|`(?:``|[^`])*`|//[^\n]*|/\*[\s\S]*?\*/")
    code = token.sub(" ", cypher)
    if any(c in code for c in "'\"`;") or "/*" in code or re.search(
            r"\b(CALL|LOAD|CREATE|INSERT|MERGE|DELETE|DETACH|SET|REMOVE|DROP|GRANT|DENY|REVOKE|ALTER|SHOW|USE|FOREACH|PROFILE|EXPLAIN)\b", code, re.I):
        raise HTTPException(422, "Only procedure-free read Cypher is supported")
    # Extension functions can perform I/O even when EXPLAIN reports a read.
    # Backtick identifiers are disallowed in function positions as well.
    if re.search(r"\.\s*(?:[A-Za-z_]\w*\s*)?\(", code) or re.search(r"`(?:``|[^`])*`\s*\(", cypher):
        raise HTTPException(422, "Extension functions are not supported")
    if not re.match(r"\s*(MATCH|OPTIONAL\s+MATCH|WITH|RETURN|UNWIND)\b", code, re.I):
        raise HTTPException(422, "Unsupported read query form")
    if any(k.startswith("__pank0919_") for k in parameters):
        raise HTTPException(422, "Reserved parameter name")
    try:
        size = len(json.dumps(parameters, allow_nan=False).encode())
    except (TypeError, ValueError, RecursionError):
        raise HTTPException(422, "Invalid query parameters") from None
    if size > 32_000:
        raise HTTPException(413, "Query parameters exceed the size limit")


def serialize(value, ids, snapshots):
    """Only actual driver objects confer identity; projected maps never do."""
    if isinstance(value, (Node, Relationship)):
        properties = dict(value)
        identifier = properties.get("id")
        prefix = "cakg:e:" if isinstance(value, Relationship) else "cakg:n:"
        if not isinstance(identifier, str) or not ID_RE.fullmatch(identifier) or not identifier.startswith(prefix):
            raise HTTPException(409, "Graph object lacks canonical identity")
        snapshot = properties.get("snapshot_id")
        if not isinstance(snapshot, str) or not snapshot:
            raise HTTPException(409, "Graph object lacks snapshot identity")
        ids.add(identifier)
        snapshots.add(snapshot)
        if isinstance(value, Relationship):
            if properties.get("assertion_status") == "quarantined":
                raise HTTPException(409, "Quarantined evidence cannot form graph assertions")
            for key, endpoint in (("start_id", value.start_node), ("end_id", value.end_node)):
                mirrored = properties.get(key)
                if not isinstance(mirrored, str) or not ID_RE.fullmatch(mirrored) or not mirrored.startswith("cakg:n:"):
                    raise HTTPException(409, "Graph relationship endpoint identity is invalid")
                # RETURN r does not hydrate its endpoint properties. If nodes
                # were also returned, their hydrated identities must agree.
                actual = endpoint.get("id")
                if actual is not None and mirrored != actual:
                    raise HTTPException(409, "Graph relationship endpoint mirror mismatch")
                if endpoint.get("snapshot_id") not in (None, snapshot):
                    raise HTTPException(409, "Graph relationship endpoint snapshot mismatch")
            return {"kind": "edge", "type": value.type, "properties": safe(properties), "id": identifier}
        return {"kind": "node", "labels": sorted(value.labels), "properties": safe(properties), "id": identifier}
    if isinstance(value, Path):
        return {"nodes": [serialize(v, ids, snapshots) for v in value.nodes],
                "edges": [serialize(v, ids, snapshots) for v in value.relationships]}
    if isinstance(value, dict):
        return {str(k): serialize(v, ids, snapshots) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(v, ids, snapshots) for v in value]
    return safe(value)


def safe(value):
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    return str(value)


class GraphStore:
    def __init__(self, settings, driver=None):
        self.settings = settings
        self.driver = driver or GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_pool_size=4, connection_timeout=5,
            connection_acquisition_timeout=settings.query_timeout)

    def session(self):
        return self.driver.session(database=self.settings.neo4j_database, default_access_mode=READ_ACCESS)

    def probe(self, expected_counts=None):
        with self.session() as session:
            summary = session.run(Query("RETURN 1 AS probe", timeout=self.settings.query_timeout)).consume()
            if summary.database != self.settings.neo4j_database:
                raise HTTPException(503, "Graph database identity mismatch")
            rows = session.run(Query(
                "MATCH (n) RETURN n.snapshot_id AS snapshot_id, count(n) AS count LIMIT 2",
                timeout=self.settings.query_timeout)).data()
            edges = session.run(Query(
                "MATCH ()-[r]->() RETURN r.snapshot_id AS snapshot_id, count(r) AS count LIMIT 2",
                timeout=self.settings.query_timeout)).data()
            if {r["snapshot_id"] for r in rows} != {self.settings.snapshot_id} or (
                    edges and {r["snapshot_id"] for r in edges} != {self.settings.snapshot_id}):
                raise HTTPException(409, "Graph snapshot identity mismatch")
            counts = {"node": sum(r["count"] for r in rows), "edge": sum(r["count"] for r in edges)}
            if expected_counts is not None and counts != expected_counts:
                raise HTTPException(409, "Graph snapshot counts mismatch")
        return {"database": self.settings.neo4j_database, "snapshot_id": self.settings.snapshot_id,
                "identity_verified": True, "object_counts": counts}

    def query(self, cypher, parameters, limit):
        validate_cypher(cypher, parameters)
        cap = min(limit, self.settings.max_rows)
        ids, snapshots, rows = set(), set(), []
        size, truncated = 2, False
        with self.session() as session:
            summary = session.run(Query("EXPLAIN " + cypher, timeout=self.settings.query_timeout), parameters).consume()
            if summary.query_type != "r":
                raise HTTPException(422, "Only read queries are supported")
            result = session.run(Query("CALL {\n" + cypher + "\n} RETURN * LIMIT $__pank0919_cap",
                                      timeout=self.settings.query_timeout),
                                 {**parameters, "__pank0919_cap": cap + 1})
            for record in result:
                if len(rows) >= cap:
                    truncated = True
                    break
                row = {k: serialize(v, ids, snapshots) for k, v in record.items()}
                size += len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode()) + 1
                if size > self.settings.max_bytes:
                    raise HTTPException(413, "Graph response exceeds the byte limit; narrow the projection")
                rows.append(row)
            result.consume()
        if snapshots and snapshots != {self.settings.snapshot_id}:
            raise HTTPException(409, "Graph snapshot mismatch")
        return {"graph_rows": rows, "object_ids": sorted(ids), "graph_snapshot_ids": sorted(snapshots),
                "graph_truncated": truncated}

    def close(self):
        self.driver.close()
