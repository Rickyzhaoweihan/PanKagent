"""Issue #34: private client routing and committed-load statistics boundaries."""
import io
import json
from types import SimpleNamespace

import psycopg
import pytest

from deploy_0919 import build, query
from deploy_0919.acceptance import Acceptance, GateFailure, GENE_POINT_QUERY, GENE_RELATIONSHIP_TYPES, gene_edge_point_query
from pankgraph0919_backend.graph import validate_cypher


def test_private_client_routes_entity_search_without_exposing_token(monkeypatch, capsys):
    payload = {"entity_type": "Regulatory_region", "assembly": "synthetic-assembly", "chromosome": "1", "start": 10, "end": 20}
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=200, json=lambda: {"items": [], "total": 0})

    token = "synthetic-unit-test-token-not-a-live-secret"
    monkeypatch.setattr(query, "owned_root", lambda root: None)
    monkeypatch.setattr(query, "credentials", lambda root: {"api_token": token})
    monkeypatch.setattr(query.httpx, "Client", Client)
    monkeypatch.setattr(query.sys, "argv", ["query", "--root", "/synthetic-owned-root", "--request", "-", "--route", "/entities/search"])
    monkeypatch.setattr(query.sys, "stdin", io.StringIO(json.dumps(payload)))
    query.main()
    assert calls == [("http://127.0.0.1:18919/entities/search", {"json": payload, "headers": {"Authorization": "Bearer " + token}})]
    assert token not in capsys.readouterr().out


class AnalyzeConnection:
    def __init__(self, *, autocommit=True, status=psycopg.pq.TransactionStatus.IDLE,
                 identity=("pankgraph0919", "serviceuser"), snapshot_status="loaded"):
        self.autocommit = autocommit
        self.info = SimpleNamespace(transaction_status=status)
        self.identity, self.snapshot_status = identity, snapshot_status
        self.calls = []

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        if sql.startswith("SELECT current_database"):
            return SimpleNamespace(fetchone=lambda: self.identity)
        if sql.startswith("SELECT status"):
            return SimpleNamespace(fetchone=lambda: (self.snapshot_status,))
        assert sql.startswith("ANALYZE cakg_mm.")
        assert self.autocommit and self.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        return SimpleNamespace()


def test_analyze_only_known_tables_after_loaded_snapshot_commit():
    conn = AnalyzeConnection()
    result = build.analyze_loaded_snapshot(conn, "synthetic-snapshot")
    assert result["status"] == "analyzed"
    analyzed = {sql for sql, _ in conn.calls if sql.startswith("ANALYZE")}
    assert analyzed == {
        "ANALYZE cakg_mm.snapshot", "ANALYZE cakg_mm.graph_object", "ANALYZE cakg_mm.snapshot_object",
        "ANALYZE cakg_mm.entity_interval", "ANALYZE cakg_mm.source_file", "ANALYZE cakg_mm.context",
        "ANALYZE cakg_mm.evidence_record", "ANALYZE cakg_mm.snapshot_record",
        "ANALYZE cakg_mm.object_evidence", "ANALYZE cakg_mm.source_rejection",
    }
    assert conn.calls[1][1] == ("synthetic-snapshot",)


@pytest.mark.parametrize("changes", [
    {"autocommit": False}, {"status": psycopg.pq.TransactionStatus.INTRANS},
    {"identity": ("unrelated", "serviceuser")}, {"identity": ("pankgraph0919", "another_owner")},
    {"snapshot_status": "building"}, {"snapshot_status": "failed"},
])
def test_analyze_refuses_uncommitted_unloaded_or_foreign_database(changes):
    conn = AnalyzeConnection(**changes)
    with pytest.raises(RuntimeError):
        build.analyze_loaded_snapshot(conn, "synthetic-snapshot")
    assert not any(sql.startswith("ANALYZE") for sql, _ in conn.calls)


@pytest.mark.parametrize("relationship_type", sorted(GENE_RELATIONSHIP_TYPES))
def test_acceptance_point_lookup_uses_indexed_labels_and_whitelisted_type(relationship_type):
    cypher = gene_edge_point_query(relationship_type)
    validate_cypher(cypher, {"gene_id": "synthetic-gene", "edge_id": "synthetic-edge"})
    assert "(g:BioEntity:Gene {id:$gene_id})" in cypher
    assert "[r:" + relationship_type + " {id:$edge_id}]" in cypher
    assert "(c:BioEntity)" in cypher and "[r]" not in cypher
    assert GENE_POINT_QUERY == "MATCH (g:BioEntity:Gene {id:$gene_id}) RETURN g"


@pytest.mark.parametrize("relationship_type", [
    "UNKNOWN", "HAS_EXPRESSION_RESULT_IN] RETURN 1 //", "HAS_ACCESSIBILITY_RESULT_IN", None,
])
def test_acceptance_rejects_untrusted_relationship_type_before_api_request(relationship_type):
    check = Acceptance(None, None, None, None, None, {}, None)
    check.select_sample = lambda: check.sample.update(relationship_type=relationship_type)
    check.request = lambda *args, **kwargs: pytest.fail("Unvalidated relationship reached the API")
    with pytest.raises(GateFailure, match="unsupported_gene_point_lookup_relationship_type"):
        check.api_graph_evidence()
