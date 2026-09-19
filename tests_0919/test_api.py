"""No network or database access: exercise drivers, gates, and request boundaries."""
from dataclasses import replace
from types import SimpleNamespace

from cakg.identity import edge_id, node_id
from fastapi import HTTPException
from fastapi.testclient import TestClient
from neo4j import READ_ACCESS
from neo4j.graph import Graph, Node
import pytest

from pankgraph0919_backend.app import create_app
from pankgraph0919_backend.config import Settings
from pankgraph0919_backend.graph import GraphStore, serialize, validate_cypher
from pankgraph0919_backend.models import EntitySearch, Filters, GeneSearch, SEARCHABLE_ENTITY_TYPES
from pankgraph0919_backend.postgres import PostgresStore, page

SNAPSHOT = "synthetic-test-snapshot"
TOKEN = "unit-test-token-that-is-never-a-real-secret"
NODE_ID = node_id("Gene", "synthetic-test", "example")[0]
CELL_ID = node_id("Cell_type", "synthetic-test", "example")[0]
REGION_ID = node_id("Regulatory_region", "synthetic-test", "example-interval")[0]
EDGE_ID = edge_id(NODE_ID, "HAS_EXPRESSION_RESULT_IN", CELL_ID)[0]


def settings(**changes):
    base = Settings(neo4j_uri="bolt://127.0.0.1:17919", neo4j_user="reader",
                    neo4j_password="test-password", snapshot_id=SNAPSHOT, api_token=TOKEN,
                    postgres_read_dsn="host=127.0.0.1 port=15919 dbname=pankgraph0919 user=pankgraph0919_reader password=test")
    return replace(base, **changes)


def node(identifier=NODE_ID, snapshot=SNAPSHOT):
    return Node(Graph(), "element-node", 1, ["BioEntity", "Gene"], {"id": identifier, "snapshot_id": snapshot})


def edge(hydrated=True, **properties):
    graph = Graph()
    start = Node(graph, "a", 1, ["Gene"], {"id": NODE_ID, "snapshot_id": SNAPSHOT} if hydrated else {})
    end = Node(graph, "b", 2, ["Cell_type"], {"id": CELL_ID, "snapshot_id": SNAPSHOT} if hydrated else {})
    rel = graph.relationship_type("HAS_EXPRESSION_RESULT_IN")(
        graph, "r", 3, {"id": EDGE_ID, "start_id": NODE_ID, "end_id": CELL_ID,
                        "snapshot_id": SNAPSHOT, **properties})
    rel._start_node, rel._end_node = start, end
    return rel


class Result:
    def __init__(self, rows=(), query_type="r"):
        self.rows = rows
        self.query_type = query_type

    def __iter__(self):
        return iter(self.rows)

    def data(self):
        return self.rows

    def consume(self):
        return SimpleNamespace(query_type=self.query_type, database="pankgraph0919")


class Driver:
    def __init__(self, rows=(), query_type="r"):
        self.rows, self.query_type = rows, query_type
        self.calls, self.sessions = [], []
        self.closed = False

    def session(self, **kwargs):
        self.sessions.append(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def run(self, query, parameters=None):
        self.calls.append((query, parameters))
        if query.text.startswith("EXPLAIN"):
            return Result(query_type=self.query_type)
        if query.text.startswith("RETURN 1"):
            return Result()
        if "count(n)" in query.text:
            return Result([{"snapshot_id": SNAPSHOT, "count": 2}])
        if "count(r)" in query.text:
            return Result([{"snapshot_id": SNAPSHOT, "count": 1}])
        return Result(self.rows)

    def close(self):
        self.closed = True


class FakePostgres:
    def __init__(self, accepted=True):
        self.calls = []
        self.is_accepted = accepted

    def probe(self):
        self.calls.append(("probe",))
        return {"snapshot_id": SNAPSHOT, "status": "accepted" if self.is_accepted else "loaded",
                "object_counts": {"node": 2, "edge": 1}, "database": "pankgraph0919"}

    def records(self, ids, filters, limit, offset):
        self.calls.append(("records", ids, filters, limit, offset))
        return {**page([{"record_id": "synthetic-record", "assertion_status": filters.record_status or "source_reported",
                         "source_status": "released"}], 12, limit, offset), "object_ids": ids or [], "unfiltered_total": 15}

    def search_genes(self, query):
        self.calls.append(("search", query))
        return page([{"object_id": NODE_ID}], 2, query.limit, query.offset)

    def search_entities(self, query):
        self.calls.append(("entity_search", query))
        return {**page([{"object_id": REGION_ID, "entity_type": query.entity_type,
                         "properties": {"name": "synthetic-source-region"},
                         "intervals": [{"genome_assembly": "synthetic-assembly", "chr": "1", "start_loc": 10, "end_loc": 20}]}],
                       2, query.limit, query.offset), "interval_coverage": {"available": True}}

    def contexts(self, filters, limit, offset):
        self.calls.append(("contexts", filters, limit, offset))
        return page([{"context_id": "synthetic-context"}], 5, limit, offset)

    def sources(self, limit, offset):
        self.calls.append(("sources", limit, offset))
        return page([{"status": "unverified", "sha256": None,
                      "record_counts": {"quarantined": 3},
                      "rejections": [{"reason_code": "unresolved", "count": 7}]}], 1, limit, offset)

    def close(self):
        pass


def client(rows=(), pg=None, config=None):
    config = config or settings()
    pg = pg or FakePostgres()
    driver = Driver(rows)
    app = create_app(config, GraphStore(config, driver), pg)
    return TestClient(app, raise_server_exceptions=False), pg, driver


AUTH = {"Authorization": "Bearer " + TOKEN}


def test_authentication_and_sanitized_health():
    web, _, _ = client()
    with web:
        assert web.get("/health").json() == {"status": "ready"}
        assert web.get("/health?detail=true").status_code == 401
        detail = web.get("/health?detail=true", headers=AUTH)
        assert detail.status_code == 200
        assert detail.json()["components"]["neo4j"]["database"] == "pankgraph0919"
        for path in ("/sources", "/contexts", f"/objects/{NODE_ID}/records"):
            assert web.get(path).status_code == 401
        assert web.post("/query", json={"cypher": "RETURN 1"}).status_code == 401
        assert web.post("/query", headers={"Authorization": "Bearer invalid"}, json={"cypher": "RETURN 1"}).status_code == 401


@pytest.mark.parametrize("query", ["CREATE (n)", "MATCH (n) DELETE n", "MATCH (n) SET n.x=1 RETURN n",
                                  "CALL db.labels()", "LOAD CSV FROM 'x' AS r RETURN r", "RETURN 1; RETURN 2",
                                  "RETURN apoc.cypher.runFirstColumn('CREATE (x)',{},false)",
                                  "RETURN apoc.`load`('x')", "SHOW DATABASES", "RETURN 1 \\u0043ALL db.labels()"])
def test_write_and_extension_rejection(query):
    with pytest.raises(HTTPException) as err:
        validate_cypher(query, {})
    assert err.value.status_code == 422


def test_read_masking_and_reserved_parameters():
    validate_cypher("MATCH (n:Gene) // DELETE is a comment\nWHERE n.name=$name RETURN n, 'CREATE' AS text", {"name": "test"})
    with pytest.raises(HTTPException):
        validate_cypher("RETURN $x", {"__pank0919_cap": 1})


def test_explain_read_type_driver_read_access_and_timeout():
    cfg = settings()
    driver = Driver([{"n": node()}])
    graph = GraphStore(cfg, driver)
    assert graph.query("MATCH (n) RETURN n", {}, 2)["object_ids"] == [NODE_ID]
    assert driver.sessions == [{"database": "pankgraph0919", "default_access_mode": READ_ACCESS}]
    assert driver.calls[0][0].text.startswith("EXPLAIN ")
    assert all(q.timeout == 10 for q, _ in driver.calls)
    write_driver = Driver(query_type="rw")
    with pytest.raises(HTTPException):
        GraphStore(cfg, write_driver).query("RETURN 1", {}, 10)
    assert len(write_driver.calls) == 1


def test_graph_brief_performs_no_postgres_request_after_startup():
    web, pg, driver = client([{"n": node()}])
    with web:
        assert pg.calls == [("probe",)]
        response = web.post("/query", headers=AUTH, json={"cypher": "MATCH (n) RETURN n"})
        assert response.status_code == 200
        assert pg.calls == [("probe",)]
        assert response.json()["object_ids"] == [NODE_ID]
    assert driver.closed


def test_detail_prefers_genuine_returned_edge_and_preserves_filters():
    web, pg, _ = client([{"n": node(), "r": edge(hydrated=False)}])
    with web:
        response = web.post("/query", headers=AUTH, json={"cypher": "MATCH (a)-[r]->(b) RETURN a,r", "mode": "detail",
                                                        "filters": {"condition": "synthetic", "record_status": "source_reported"},
                                                        "limit": 2, "offset": 3})
        assert response.status_code == 200, response.text
        assert response.json()["expansion_policy"] == "returned_edges"
        call = pg.calls[-1]
        assert call[1] == [EDGE_ID]
        assert call[2].condition == "synthetic" and call[3:] == (2, 3)
        assert response.json()["evidence"]["items"][0]["assertion_status"] == "source_reported"


@pytest.mark.parametrize("projection", [NODE_ID, {"id": NODE_ID, "snapshot_id": SNAPSHOT},
                                        {"kind": "edge", "id": EDGE_ID, "properties": {"id": EDGE_ID}}])
def test_scalar_and_map_ids_never_trigger_evidence_expansion(projection):
    web, pg, _ = client([{"projected": projection}])
    with web:
        response = web.post("/query", headers=AUTH, json={"cypher": "RETURN $value", "parameters": {"value": projection}, "mode": "detail"})
        assert response.status_code == 200
        assert response.json()["evidence"]["match_status"] == "no_graph_objects"
        assert pg.calls == [("probe",)]


def test_snapshot_gate_and_requested_snapshot_mismatch():
    web, _, _ = client(pg=FakePostgres(accepted=False))
    with web:
        assert web.get("/health").status_code == 503
        assert web.post("/query", headers=AUTH, json={"cypher": "RETURN 1"}).status_code == 503
    web, _, _ = client([{"n": node(snapshot="other")}])
    with web:
        assert web.post("/query", headers=AUTH, json={"cypher": "RETURN 1", "snapshot_id": "other"}).status_code == 409
        assert web.post("/query", headers=AUTH, json={"cypher": "MATCH (n) RETURN n"}).status_code == 409


def test_startup_counts_must_match():
    web, pg, _ = client()
    original = pg.probe
    pg.probe = lambda: {**original(), "object_counts": {"node": 7, "edge": 1}}
    with web:
        assert web.get("/health").status_code == 503


def test_postgres_query_routes_work_when_graph_unavailable_at_startup():
    config = settings()
    graph = GraphStore(config, Driver())
    graph.probe = lambda counts: (_ for _ in ()).throw(RuntimeError("graph unavailable"))
    with TestClient(create_app(config, graph, FakePostgres())) as web:
        assert web.get("/health").status_code == 503
        assert web.post("/genes/search", headers=AUTH, json={"query": "synthetic"}).status_code == 200
        assert web.post("/entities/search", headers=AUTH, json={"query": "synthetic", "entity_type": "Regulatory_region"}).status_code == 200
        assert web.post("/query", headers=AUTH, json={"postgres_search": {"query": "synthetic"}}).status_code == 200
        assert web.post("/records/search", headers=AUTH, json={"collection_id": "synthetic"}).status_code == 200
        assert web.post("/query", headers=AUTH, json={"cypher": "RETURN 1"}).status_code == 503


def test_public_health_refreshes_after_ttl_and_graph_brief_survives_pg_outage():
    web, pg, _ = client([{"n": node()}])
    with web:
        pg.probe = lambda: (_ for _ in ()).throw(RuntimeError("database unavailable"))
        web.app.state.runtime.checked_at = 0
        assert web.get("/health").status_code == 503
        assert web.post("/query", headers=AUTH, json={"cypher": "MATCH (n) RETURN n"}).status_code == 200
        assert web.post("/genes/search", headers=AUTH, json={"query": "synthetic"}).status_code == 503


def test_invalid_endpoint_mirrors_and_quarantined_edge_rejected():
    for rel in (edge(start_id=CELL_ID), edge(assertion_status="quarantined")):
        with pytest.raises(HTTPException):
            serialize(rel, set(), set())


def test_pagination_gene_search_contexts_and_sources():
    web, pg, _ = client()
    with web:
        r = web.get("/contexts?limit=1&offset=2&record_status=quarantined", headers=AUTH)
        assert r.json()["next_offset"] == 3
        assert pg.calls[-1][1].record_status == "quarantined"
        assert web.get("/contexts?limit=201", headers=AUTH).status_code == 422
        assert web.get("/contexts?record_status=positive", headers=AUTH).status_code == 422
        r = web.post("/genes/search", headers=AUTH, json={"query": "synthetic", "limit": 1, "offset": 1})
        assert r.status_code == 200 and r.json()["offset"] == 1
        r = web.get("/sources", headers=AUTH)
        assert r.json()["items"][0]["status"] == "unverified"
        assert r.json()["items"][0]["sha256"] is None
        assert r.json()["items"][0]["rejections"][0]["count"] == 7


def test_independent_records_search_includes_quarantine_without_object():
    web, pg, _ = client()
    with web:
        assert web.post("/records/search", headers=AUTH, json={"record_status": "quarantined"}).status_code == 422
        r = web.post("/records/search", headers=AUTH, json={"source_file_id": "synthetic-file", "record_status": "quarantined"})
        assert r.status_code == 200
        assert r.json()["items"][0]["assertion_status"] == "quarantined"
        assert pg.calls[-1][1] is None
        assert pg.calls[-1][2].source_file_id == "synthetic-file"


@pytest.mark.parametrize("body", [{}, {"cypher": "RETURN 1", "postgres_search": {"query": "test"}},
                                  {"cypher": "RETURN 1", "filters": {"condition": "x"}},
                                  {"postgres_search": {"query": "test"}, "parameters": {"x": 1}},
                                  {"sql": "SELECT * FROM secrets"}])
def test_exactly_one_structured_query_source(body):
    web, _, _ = client()
    with web:
        assert web.post("/query", headers=AUTH, json=body).status_code == 422


def test_limits_and_exception_sanitization():
    web, pg, _ = client([{"value": "x" * 1500}], config=settings(max_bytes=1024))
    with web:
        assert web.post("/query", headers=AUTH, json={"cypher": "RETURN 1"}).status_code == 413
        assert web.post("/query", headers=AUTH, content=b"x" * 64_001).status_code == 413
        pg.search_genes = lambda body: (_ for _ in ()).throw(RuntimeError("password=private-value"))
        response = web.post("/genes/search", headers=AUTH, json={"query": "x"})
        assert response.status_code == 503 and "private-value" not in response.text
    graph = GraphStore(settings(max_rows=2), Driver([{"x": 1}, {"x": 2}, {"x": 3}]))
    result = graph.query("RETURN 1", {}, 10)
    assert len(result["graph_rows"]) == 2 and result["graph_truncated"]


@pytest.mark.parametrize("changes", [
    {"neo4j_database": "neo4j"}, {"neo4j_uri": "bolt://other:7687"}, {"port": 8794},
    {"postgres_read_dsn": "host=127.0.0.1 port=15919 dbname=other user=pankgraph0919_reader"},
    {"postgres_read_dsn": "host=127.0.0.1 port=15919 dbname=pankgraph0919 user=postgres"},
    {"api_token": "short"}, {"query_timeout": 100}, {"max_rows": 1000}])
def test_configuration_cannot_select_existing_database_or_privileged_role(changes):
    with pytest.raises(ValueError):
        settings(**changes)


class Cursor:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))

    def fetchone(self):
        return self.answers.pop(0)

    def fetchall(self):
        return self.answers.pop(0)


class Connection:
    def __init__(self, cursor):
        self.cur = cursor
        self.kwargs = None

    def __call__(self, dsn, **kwargs):
        self.kwargs = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        return self.cur


PG_IDENTITY = {"database": "pankgraph0919", "role": "pankgraph0919_reader", "read_only": "on"}
ACCEPTED = {"status": "accepted", "snapshot_count": 1}


def source_record(classification="public_aggregate", status="source_reported"):
    return {"record_id": "synthetic-record", "source_metadata": {"privacy_classification": classification},
            "metrics": {"synthetic_count": 5}, "raw_record": {"synthetic_source_value": "5"},
            "metadata": {}, "context_metadata": {}, "assertion_status": status, "source_status": "released"}


def test_postgres_readonly_identity_and_bound_search_parameters():
    cur = Cursor([PG_IDENTITY, ACCEPTED, {"count": 1}, [{"object_id": NODE_ID}]])
    conn = Connection(cur)
    store = PostgresStore(settings(), conn)
    malicious = "x%' OR true --"
    assert store.search_genes(GeneSearch(query=malicious))["total"] == 1
    assert "default_transaction_read_only=on" in conn.kwargs["options"]
    assert "statement_timeout=10000" in conn.kwargs["options"]
    assert all(malicious not in sql for sql, _ in cur.calls)
    assert any("ILIKE %s" in sql for sql, _ in cur.calls)
    bad = PostgresStore(settings(), Connection(Cursor([{**PG_IDENTITY, "role": "postgres"}])))
    with pytest.raises(HTTPException):
        bad.probe()


def test_source_search_counts_and_privacy_redaction():
    cur = Cursor([PG_IDENTITY, ACCEPTED, {"count": 30}, {"count": 3}, [source_record("sensitive", "quarantined")]])
    store = PostgresStore(settings(), Connection(cur))
    result = store.records(None, Filters(source_file_id="synthetic-file", record_status="quarantined"), 2, 0)
    assert result["unfiltered_total"] == 30 and result["total"] == 3
    assert result["items"][0]["assertion_status"] == "quarantined"
    assert result["items"][0]["details_redacted"]
    assert "raw_record" not in result["items"][0] and "metrics" not in result["items"][0]
    assert "er.source_file_id = %s" in cur.calls[2][0]
    assert "assertion_status = %s" not in cur.calls[2][0]
    assert "assertion_status = %s" in cur.calls[3][0]


def test_source_coverage_remains_visible_without_sensitive_metadata():
    source = {"file_id": "synthetic-file", "metadata": {
        "privacy_classification": "sensitive", "acquisition_status": "downloaded",
        "controlled_access": None, "individual_measurement": "PRIVATE"},
        "build_summary": {"status": "build_failed", "reason": "invalid layout"}}
    cur = Cursor([PG_IDENTITY, ACCEPTED, {"count": 1}, [source], [], []])
    result = PostgresStore(settings(), Connection(cur)).sources(10, 0)
    item = result["items"][0]
    assert item["metadata"]["acquisition_status"] == "downloaded"
    assert item["metadata"]["controlled_access"] is None
    assert "individual_measurement" not in item["metadata"]
    assert item["build_summary"]["status"] == "build_failed"
    assert item["record_counts"] == {}


def test_aggregate_source_values_retained_and_sensitive_opt_in():
    for classification, enabled in (("public_aggregate", False), ("sensitive", True)):
        cur = Cursor([PG_IDENTITY, ACCEPTED, {"count": 1}, {"count": 1}, [source_record(classification)]])
        result = PostgresStore(settings(allow_sensitive_records=enabled), Connection(cur)).records(
            None, Filters(collection_id="synthetic-collection"), 10, 0)
        assert result["items"][0]["raw_record"] == {"synthetic_source_value": "5"}
        assert not result["items"][0]["details_redacted"]


def test_quarantined_record_cannot_expand_graph_edge_even_if_registry_corrupted():
    cur = Cursor([PG_IDENTITY, ACCEPTED, [{"object_id": EDGE_ID, "object_kind": "edge"}],
                  {"count": 1}, {"count": 1}, [source_record(status="quarantined")]])
    with pytest.raises(HTTPException) as err:
        PostgresStore(settings(), Connection(cur)).records([EDGE_ID], Filters(), 10, 0)
    assert err.value.status_code == 409


def test_region_requires_explicit_assembly_and_half_open_interval():
    with pytest.raises(ValueError):
        GeneSearch(chromosome="1", start=1, end=10)
    cur = Cursor([PG_IDENTITY, ACCEPTED, {"count": 0}, []])
    PostgresStore(settings(), Connection(cur)).search_genes(GeneSearch(chromosome="1", assembly="synthetic-assembly", start=1, end=10))
    assert any("ei.locus && int8range(%s, %s, '[)')" in sql for sql, _ in cur.calls)


def test_entity_route_requires_auth_and_preserves_region_parameters():
    web, pg, _ = client()
    body = {"entity_type": "Regulatory_region", "assembly": "synthetic-assembly", "chromosome": "1",
            "start": 10, "end": 20, "limit": 1, "offset": 1}
    with web:
        assert web.post("/entities/search", json=body).status_code == 401
        response = web.post("/entities/search", headers=AUTH, json=body)
        assert response.status_code == 200
        assert response.json()["items"][0]["object_id"] == REGION_ID
        assert response.json()["items"][0]["intervals"][0] == {
            "genome_assembly": "synthetic-assembly", "chr": "1", "start_loc": 10, "end_loc": 20}
        assert pg.calls[-1][0] == "entity_search" and pg.calls[-1][1].model_dump() == {"query": None, **body}


@pytest.mark.parametrize("entity_type", sorted(SEARCHABLE_ENTITY_TYPES))
def test_entity_search_accepts_only_named_entity_types(entity_type):
    web, _, _ = client()
    with web:
        assert web.post("/entities/search", headers=AUTH, json={"entity_type": entity_type, "query": "synthetic"}).status_code == 200


@pytest.mark.parametrize("body", [
    {"query": "synthetic"},
    {"entity_type": "Gene' OR true --", "query": "synthetic"},
    {"entity_type": "Donor", "query": "synthetic"},
    {"entity_type": "Regulatory_region", "chromosome": "1", "start": 10, "end": 20},
    {"entity_type": "Regulatory_region", "query": "synthetic", "limit": 101},
    {"entity_type": "Regulatory_region", "query": "synthetic", "offset": 1_000_001},
    {"entity_type": "Regulatory_region", "query": "synthetic", "sql": "SELECT 1"},
])
def test_entity_search_rejects_unsupported_type_or_unbounded_request(body):
    web, _, _ = client()
    with web:
        assert web.post("/entities/search", headers=AUTH, json=body).status_code == 422


def test_generic_entity_sql_binds_type_and_returns_authoritative_intervals():
    item = {"object_id": REGION_ID, "entity_type": "Regulatory_region", "node_labels": ["Regulatory_region"],
            "properties": {"name": "synthetic-source-region"},
            "intervals": [{"genome_assembly": "synthetic-assembly", "chr": "1", "start_loc": 10, "end_loc": 20}]}
    cur = Cursor([PG_IDENTITY, ACCEPTED, {"count": 1}, [item], {"available": True}])
    store = PostgresStore(settings(), Connection(cur))
    result = store.search_entities(EntitySearch(entity_type="Regulatory_region", assembly="synthetic-assembly",
                                                chromosome="1", start=10, end=20, query="synthetic_%"))
    assert result["items"] == [item] and result["interval_coverage"]["available"]
    assert result["interval_coverage"]["coordinate_convention"] == "zero_based_half_open"
    sql, parameters = cur.calls[2]
    assert "go.entity_type = %s" in sql and "Regulatory_region" not in sql
    assert parameters[:2] == [SNAPSHOT, "Regulatory_region"]
    assert parameters[-4:] == ["synthetic-assembly", "1", 10, 20]
    assert "jsonb_agg" in cur.calls[3][0] and "FROM cakg_mm.entity_interval stored" in cur.calls[3][0]
    assert cur.calls[4][1] == [SNAPSHOT, "Regulatory_region"]
    with pytest.raises(HTTPException):
        store.search_entities(EntitySearch.model_construct(entity_type="Gene' OR true --", query="x"))


def test_gene_interval_absence_is_explicit_without_changing_old_gene_contract():
    cur = Cursor([PG_IDENTITY, ACCEPTED, {"count": 0}, [], {"available": False}])
    result = PostgresStore(settings(), Connection(cur)).search_entities(
        EntitySearch(entity_type="Gene", chromosome="1", assembly="synthetic-assembly", start=10, end=20))
    assert result["items"] == [] and result["total"] == 0
    assert result["interval_coverage"]["available"] is False
    assert "biological absence is not established" in result["interval_coverage"]["empty_result_interpretation"]
    old = Cursor([PG_IDENTITY, ACCEPTED, {"count": 1}, [{"object_id": NODE_ID}]])
    old_result = PostgresStore(settings(), Connection(old)).search_genes(GeneSearch(query="synthetic"))
    assert "interval_coverage" not in old_result and "intervals" not in old_result["items"][0]
    assert len(old.calls) == 4 and old.calls[2][1][:2] == [SNAPSHOT, "Gene"]
    web, _, _ = client()
    with web:
        assert web.post("/genes/search", headers=AUTH, json={"query": "synthetic", "entity_type": "Regulatory_region"}).status_code == 422
        assert web.post("/query", headers=AUTH, json={"postgres_search": {
            "query": "synthetic", "entity_type": "Regulatory_region"}}).status_code == 422
