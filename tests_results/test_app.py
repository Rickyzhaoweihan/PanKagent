"""Offline API integration contracts: scientific work must be explicit and once."""
import asyncio
from contextlib import asynccontextmanager
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from pankagent_vnext.config import Settings
from pankagent_vnext.answer_router import AnswerSkillRouter
from pankagent_vnext.llm import ClaudeGateway
from pankgraph_results.app import create_app
from pankgraph_results.auth import hash_password
from pankgraph_results.config import ResultsSettings


RELEASE = "test-rl-release"
NODE = {"id": "ENSG00000001084", "labels": ["Gene"], "properties": {"name": "GCLC"}}
STEP = {"step_id": "lookup", "status": "complete", "nodes": [NODE], "edges": [],
        "rows": [{"gene": NODE["id"], "measured_value": .123}], "graph_version": RELEASE,
        "validation": [{"valid": True}], "queries": [{"cypher": "MATCH (g:Gene) RETURN g", "parameters": {}}],
        "truncated": False, "purpose": "primary"}
EVIDENCE = {"graph_version": RELEASE, "nodes": [NODE], "edges": [], "rows": STEP["rows"],
            "steps": [STEP], "completeness": "complete", "truncated": False}


def persisted_run(run_id=None):
    return {"run_id": run_id or str(uuid4()), "session_id": str(uuid4()), "status": "completed",
        "question": "What evidence concerns GCLC?", "evidence": copy.deepcopy(EVIDENCE),
        "preview": {"evidence": copy.deepcopy(EVIDENCE)}, "graph_answer": "The retained graph answer [G1].",
        "literature": {"status": "complete", "perspectives": [{"answer": "A retained cited perspective."}]},
        "plan": {"steps": [{"resolved_entities": [{"id": NODE["id"], "state": "resolved", "graph_version": RELEASE}]}]}}


class FakeQuery:
    def __init__(self):
        self.calls = self.searches = self.probes = 0
        self.gate = None
        self.evidence = copy.deepcopy(EVIDENCE)

    async def execute(self, template_id, parameters, question):
        self.calls += 1
        if self.gate:
            await self.gate.wait()
        return copy.deepcopy(self.evidence)

    async def search(self, *args, **kwargs):
        self.searches += 1
        return {"items": [], "coverage": {"complete": True, "source": "configured_graph"}}

    async def probe(self):
        self.probes += 1
        return {"ok": True}

    async def close(self): pass


class FakeLayout:
    def __init__(self):
        self.calls = 0
        self.inputs = []

    async def layout(self, evidence, focus, previous_layout=None):
        self.calls += 1
        self.inputs.append((copy.deepcopy(evidence), list(focus)))
        nodes = [{"~id": node["id"], "~entityType": "node", "~labels": node["labels"],
                  "~properties": node["properties"]} for node in evidence["nodes"]]
        return {"combined_query_result": {"nodes": nodes, "edges": []}, "core_nodes": list(focus),
            "xy_json": {node["~id"]: {"x": index * 10, "y": 0, "Level": "Core"} for index, node in enumerate(nodes)},
            "full_evidence": {"node_count": len(evidence["nodes"]), "edge_count": len(evidence["edges"])},
            "display": {"displayed_node_count": len(nodes)}, "layout": {"status": "available"}}

    def snapshot(self): return {"state": "healthy", "version": "test-layout"}
    async def close(self): pass


class FakeResources:
    def __init__(self):
        self.calls = self.downloads = self.lookups = self.asset_reads = 0
        self.error = None
        self.gate = None

    async def resolve(self, evidence):
        self.calls += 1
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return {"status": "not_applicable", "resources_tabs": {"references": {}}, "assets": []}

    async def indexed_lookup(self, **kwargs):
        self.lookups += 1
        return {"rows": [], "coverage": {"exhaustive": False, "indexed_sets": 0}}

    async def download(self, *args):
        self.downloads += 1
        raise KeyError("unregistered")

    async def asset(self, *args):
        self.asset_reads += 1
        raise KeyError("missing")

    def snapshot(self): return {"state": "unknown", "coverage": {"exhaustive": False}}
    async def close(self): pass


class FakeGateway:
    def __init__(self):
        self.calls = 0
        self.budget_reads = 0
        self.budget = SimpleNamespace(snapshot=self.budget_snapshot)

    def budget_snapshot(self):
        self.budget_reads += 1
        return {"remaining_usd": 8.0, "spent_usd": 2.0, "reserved_usd": 0}

    async def synthesize(self, question, evidence):
        self.calls += 1
        yield "A new grounded "
        yield "answer [G1]."

    async def close(self): pass


class PreparingGateway(FakeGateway):
    """Exercise the real pure prompt compiler, never construct an API client."""
    def __init__(self, chunks=("Prepared grounded answer [G1].",)):
        super().__init__()
        self.compiler = SimpleNamespace(answer_router=AnswerSkillRouter())
        self.chunks = chunks
        self.prepared = None
        self.evidence_input = None

    async def synthesize(self, question, evidence):
        self.calls += 1
        self.evidence_input = copy.deepcopy(evidence)
        self.prepared = ClaudeGateway.prepare_answer(self.compiler, question, evidence)
        for chunk in self.chunks:
            yield chunk


@asynccontextmanager
async def service(tmp_path, *, run=None, query=None, layout=None, resources=None, gateway=None,
                  upstream_handler=None, testing=True, transport_client=("127.0.0.1", 24000)):
    frontend = tmp_path / "frontend"
    frontend.mkdir(exist_ok=True)
    (frontend / "index.html").write_text("<!doctype html><title>Existing result flow</title>")
    settings = ResultsSettings(state_dir=tmp_path / "results", frontend_dir=frontend,
        testing=testing, basic_user="demo", password_hash=hash_password("synthetic-test-password"),
        operator_token="synthetic-operator-token")
    vnext = Settings(state_dir=tmp_path / "shared-ledger", graph_version=RELEASE)
    query, layout = query or FakeQuery(), layout or FakeLayout()
    resources, gateway = resources or FakeResources(), gateway or FakeGateway()
    upstream_calls = []
    def upstream(request):
        upstream_calls.append(request)
        if upstream_handler:
            return upstream_handler(request)
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"ready": True})
        if run is not None and request.url.path == "/v2/runs/" + run["run_id"]:
            return httpx.Response(200, json=run)
        return httpx.Response(404, json={"detail": "not found"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(settings, vnext, query=query, layout=layout, resources=resources, gateway=gateway, http=http)
    runtime = app.state.runtime
    # Explicitly separate the background probe scheduler from health endpoint tests.
    # It has independent probe tests; dashboard requests must not schedule it.
    runtime.health.start = lambda: None
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=transport_client),
                                     base_url="http://results.local") as client:
            yield SimpleNamespace(client=client, runtime=runtime, query=query, layout=layout,
                                  resources=resources, gateway=gateway, upstream_calls=upstream_calls)


async def finished(client, result_id):
    for _ in range(300):
        response = await client.get("/pankgraph-vnext/api/results/" + result_id)
        assert response.status_code == 200
        result = response.json()
        if result["status"] == "failed" or all(value not in {"pending", "running"} for value in result["component_status"].values()):
            return result
        await asyncio.sleep(.005)
    raise AssertionError(f"Result did not finish: {result}")


def test_agent_result_reuses_persisted_evidence_answer_and_literature_without_queries(tmp_path):
    async def scenario():
        run = persisted_run()
        async with service(tmp_path, run=run) as s:
            response = await s.client.post("/pankgraph-vnext/api/results", json={"run_id": run["run_id"]})
            assert response.status_code == 202
            result = await finished(s.client, response.json()["result_id"])
            assert result["status"] == "ready"
            assert result["answer"] == run["graph_answer"] and result["literature"] == run["literature"]
            assert result["evidence"] == run["evidence"]
            assert result["source"]["retrieval"] == "persisted_final"
            assert s.query.calls == s.gateway.calls == 0
            assert s.layout.calls == s.resources.calls == 1
            assert s.layout.inputs[0][1] == [NODE["id"]]
            count = len(s.upstream_calls)
            for _ in range(3):
                await s.client.get("/api/results/" + result["result_id"])
            assert len(s.upstream_calls) == count and s.layout.calls == 1
    asyncio.run(scenario())


def test_searched_variant_never_inherits_lead_variant_pip(tmp_path):
    class Query(FakeQuery):
        async def search(self, *args, **kwargs):
            return {"items": [{"snp": "rsLead", "pip": .13, "credible_set": "cs1", "data_source": "study"}],
                    "coverage": {"complete": True}}
    class Resources(FakeResources):
        async def indexed_lookup(self, **kwargs):
            return {"rows": [{"snp": "rsSearched", "pip": 0, "credible_set": "cs1", "data_source": "study"}],
                    "coverage": {"exhaustive": False}}
    async def scenario():
        async with service(tmp_path, query=Query(), resources=Resources()) as s:
            result = await s.runtime.search("credible_set", "", "qtl_by_variant", {"variant_id": "rsSearched"})
            row = result["items"][0]
            assert (row["searched_snp"], row["searched_pip"]) == ("rsSearched", 0)
            assert (row["lead_snp"], row["lead_pip"]) == ("rsLead", .13)
            assert row["snp"] == "rsLead"  # The graph endpoint remains its actual node.
            assert result["coverage"]["complete"] is False
            s.runtime.resources = FakeResources()
            unknown = (await s.runtime.search("credible_set", "", "qtl_by_variant", {"variant_id": "rsMissing"}))["items"][0]
            assert unknown["searched_pip"] is None and unknown["lead_pip"] == .13
    asyncio.run(scenario())


def test_conventional_result_is_executed_and_synthesized_once_across_duplicate_and_reload(tmp_path):
    async def scenario():
        body = {"template_id": "expression_by_gene", "parameters": {"gene_id": NODE["id"]}, "question": "Describe GCLC expression"}
        async with service(tmp_path) as s:
            responses = await asyncio.gather(*(s.client.post("/api/results", json=body) for _ in range(3)))
            assert all(response.status_code == 202 for response in responses)
            ids = {response.json()["result_id"] for response in responses}
            assert len(ids) == 1
            rid = ids.pop()
            result = await finished(s.client, rid)
            assert result["answer"] == "A new grounded answer [G1]."
            assert result["answer_validation"]["evidence_references"] == [1]
            assert s.query.calls == s.gateway.calls == s.layout.calls == s.resources.calls == 1
        async with service(tmp_path) as reloaded:
            saved = (await reloaded.client.get("/api/results/" + rid)).json()
            assert saved["answer"] == result["answer"]
            duplicate = await reloaded.client.post("/api/results", json=body)
            assert duplicate.json()["result_id"] == rid
            assert reloaded.query.calls == reloaded.gateway.calls == reloaded.layout.calls == reloaded.resources.calls == 0
    asyncio.run(scenario())


def test_optional_resources_arrive_after_graph_and_failure_keeps_evidence(tmp_path):
    async def scenario():
        resources = FakeResources()
        resources.gate = asyncio.Event()
        resources.error = RuntimeError("PRIVATE_UPSTREAM_PROCESSING_MUST_NOT_LEAK")
        async with service(tmp_path, resources=resources) as s:
            created = await s.client.post("/api/results", json={"template_id": "expression_by_gene", "parameters": {"gene_id": NODE["id"]}})
            rid = created.json()["result_id"]
            for _ in range(200):
                result = (await s.client.get("/api/results/" + rid)).json()
                if result["status"] == "ready":
                    break
                await asyncio.sleep(.005)
            assert result["component_status"]["graph"] == "available"
            assert result["component_status"]["resources"] == "pending"
            resources.gate.set()
            result = await finished(s.client, rid)
            assert result["component_status"]["resources"] == "unavailable"
            assert result["evidence"] == EVIDENCE and result["answer"]
            assert "PRIVATE_UPSTREAM" not in json.dumps(result)
            assert result["combined_query_result"]["nodes"][0]["~id"] == NODE["id"]
    asyncio.run(scenario())


def test_basic_authentication_and_forwarded_operator_boundary(tmp_path):
    async def scenario():
        async with service(tmp_path, testing=False) as s:
            assert (await s.client.get("/pankgraph-vnext/")).status_code == 401
            assert (await s.client.get("/health/components")).status_code == 200  # Direct local operator.
            assert (await s.client.get("/pankgraph-vnext/health/components", headers={"X-Forwarded-For": "203.0.113.8"})).status_code == 401
            auth = httpx.BasicAuth("demo", "synthetic-test-password")
            assert (await s.client.get("/pankgraph-vnext/", auth=auth)).status_code == 200
            headers = {"X-Forwarded-For": "203.0.113.8"}
            assert (await s.client.get("/pankgraph-vnext/health/components", headers=headers, auth=auth)).status_code == 403
            assert (await s.client.get("/pankgraph-vnext/metrics", headers=headers, auth=auth)).status_code == 403
            headers["X-Operator-Token"] = "synthetic-operator-token"
            assert (await s.client.get("/pankgraph-vnext/health/components", headers=headers, auth=auth)).status_code == 200
            assert (await s.client.post("/api/results", headers={"Sec-Fetch-Site": "cross-site"}, auth=auth,
                json={"template_id": "expression_by_gene", "parameters": {"gene_id": NODE["id"]}})).status_code == 403
            assert s.query.calls == s.gateway.calls == s.resources.calls == 0
    asyncio.run(scenario())


def test_cached_health_polling_performs_no_scientific_work_or_new_probe(tmp_path):
    async def scenario():
        async with service(tmp_path) as s:
            for component in ("neo4j", "agent", "result_storage", "budget"):
                s.runtime.health.record(component, "healthy")
            s.runtime.health.budget = {"remaining_usd": 8}
            for _ in range(4):
                for route in ("/health/live", "/health/ready", "/health/components", "/metrics"):
                    assert (await s.client.get(route)).status_code == 200
            assert s.query.calls == s.query.searches == s.query.probes == s.gateway.calls == s.gateway.budget_reads == 0
            assert s.layout.calls == s.resources.calls == s.resources.downloads == s.resources.lookups == 0
            assert not s.upstream_calls
            s.runtime.health.observations["neo4j"]["checked_epoch"] -= 100
            assert (await s.client.get("/health/ready")).status_code == 503
            assert (await s.client.get("/health/components")).json()["components"]["neo4j"]["error_category"] == "stale_observation"
    asyncio.run(scenario())


def test_sse_proxy_preserves_replay_and_strips_browser_and_response_credentials(tmp_path):
    async def scenario():
        run_id = str(uuid4())
        event = 'id: 8\nevent: graph_answer\ndata: {"sequence":8,"text":"retained"}\n\n'
        async with service(tmp_path, upstream_handler=lambda request: httpx.Response(200, content=event,
            headers={"Content-Type": "text/event-stream", "Set-Cookie": "private=upstream", "Authorization": "provider-secret"})) as s:
            for replay in ("after=7", "after_sequence=7"):
                response = await s.client.get(f"/pankgraph-vnext/api/agent/v2/runs/{run_id}/events?{replay}", headers={
                    "Accept": "text/event-stream", "Last-Event-ID": "7", "Authorization": "Basic browser-credential",
                    "Cookie": "private-session=browser", "X-Operator-Token": "operator-credential", "X-Api-Key": "not-forwarded"})
                assert response.status_code == 200 and response.text == event
                assert response.headers["x-accel-buffering"] == "no"
                assert "set-cookie" not in response.headers and "authorization" not in response.headers
                upstream = s.upstream_calls[-1]
                assert upstream.headers["last-event-id"] == "7"
                assert upstream.url.query.decode() == replay
                assert not any(name in upstream.headers for name in ("authorization", "cookie", "x-operator-token", "x-api-key"))
            assert s.query.calls == s.gateway.calls == 0
            bad = await s.client.get(f"/api/agent/v2/runs/{run_id}/events?target=https://attacker.invalid")
            assert bad.status_code == 422 and len(s.upstream_calls) == 2
    asyncio.run(scenario())


def test_unknown_queries_and_client_supplied_agent_evidence_are_rejected(tmp_path):
    async def scenario():
        async with service(tmp_path) as s:
            for body in ({"cypher": ["MATCH (n) RETURN n"]},
                         {"template_id": "arbitrary_query", "parameters": {}},
                         {"template_id": "expression_by_gene", "parameters": {"gene_id": NODE["id"], "sql": "SELECT * FROM private"}},
                         {"run_id": str(uuid4()), "evidence": EVIDENCE},
                         {"run_id": str(uuid4()), "question": "Override persisted intent"}):
                assert (await s.client.post("/api/results", json=body)).status_code == 422
            assert (await s.client.get("/api/search?kind=gene&term=GC&query=MATCH")).status_code == 422
            for path in ("/api/agent/health/components", "/api/agent/v1/cypher", "/api/agent/v2/plans/../../../health"):
                assert (await s.client.get(path)).status_code == 404
            assert s.query.calls == s.query.searches == s.gateway.calls == s.layout.calls == 0
            assert not s.upstream_calls
    asyncio.run(scenario())


def test_agent_release_mismatch_and_missing_preview_never_reexecute_queries(tmp_path):
    async def scenario():
        run = persisted_run()
        run["evidence"]["graph_version"] = "another-release"
        run["preview"] = None
        async with service(tmp_path, run=run) as s:
            for phase in ("final", "preview"):
                response = await s.client.post("/api/results", json={"run_id": run["run_id"], "phase": phase})
                assert response.status_code == 409
            assert s.query.calls == s.gateway.calls == s.layout.calls == 0
    asyncio.run(scenario())


def test_duplicate_at_full_capacity_reuses_job_but_new_work_is_rejected(tmp_path):
    async def scenario():
        query = FakeQuery()
        query.gate = asyncio.Event()
        async with service(tmp_path, query=query) as s:
            s.runtime.settings.max_queue = 0
            s.runtime.settings.max_concurrent = 1
            body = {"template_id": "expression_by_gene", "parameters": {"gene_id": NODE["id"]}}
            original = await s.client.post("/api/results", json=body)
            assert original.status_code == 202
            duplicate = await s.client.post("/api/results", json=body)
            assert duplicate.status_code == 202
            assert duplicate.json()["result_id"] == original.json()["result_id"]
            rejected = await s.client.post("/api/results", json={**body, "parameters": {"gene_id": "ENSG00000000002"}})
            assert rejected.status_code == 429
            query.gate.set()
            await finished(s.client, original.json()["result_id"])
            assert query.calls == s.gateway.calls == 1
    asyncio.run(scenario())


def test_concurrent_distinct_creators_cannot_overfill_admission_capacity(tmp_path):
    async def scenario():
        query = FakeQuery()
        query.gate = asyncio.Event()
        async with service(tmp_path, query=query) as s:
            s.runtime.settings.max_queue = 0
            s.runtime.settings.max_concurrent = 1
            responses = await asyncio.gather(*(s.client.post("/api/results", json={
                "template_id": "expression_by_gene", "parameters": {"gene_id": f"ENSG{index:011d}"}}) for index in range(1, 7)))
            assert [response.status_code for response in responses].count(202) == 1
            assert [response.status_code for response in responses].count(429) == 5
            assert len(s.runtime.tasks) == 1
            accepted = next(response for response in responses if response.status_code == 202)
            query.gate.set()
            await finished(s.client, accepted.json()["result_id"])
            assert query.calls == s.gateway.calls == 1
    asyncio.run(scenario())


def test_saved_preview_never_inherits_final_answer_or_literature_from_different_evidence(tmp_path):
    async def scenario():
        run = persisted_run()
        run["evidence"]["rows"] = [{"gene": NODE["id"], "measured_value": 99.0}]
        run["graph_answer"] = "Final-only answer based on a later measurement."
        async with service(tmp_path, run=run) as s:
            created = await s.client.post("/api/results", json={"run_id": run["run_id"], "phase": "preview"})
            result = await finished(s.client, created.json()["result_id"])
            assert result["evidence"] == run["preview"]["evidence"]
            assert result["answer"] == "" and result["literature"] == []
            assert result["component_status"]["answer"] == "not_requested"
            assert result["source"]["retrieval"] == "persisted_preview"
            assert s.query.calls == s.gateway.calls == 0
    asyncio.run(scenario())


def test_conventional_aggregate_is_converted_to_real_gateway_step_mapping(tmp_path):
    async def scenario():
        gateway = PreparingGateway()
        async with service(tmp_path, gateway=gateway) as s:
            created = await s.client.post("/api/results", json={"template_id": "expression_by_gene", "parameters": {"gene_id": NODE["id"]}})
            result = await finished(s.client, created.json()["result_id"])
            assert result["component_status"]["answer"] == "available"
            assert gateway.calls == 1
            assert gateway.evidence_input == {"lookup": STEP}
            prepared = json.loads(gateway.prepared.body)
            assert isinstance(prepared["evidence"], list) and len(prepared["evidence"]) == 1
            assert NODE["id"] in gateway.prepared.body and "GCLC" in gateway.prepared.body
            assert "style_version" in gateway.prepared.profile
            assert gateway.prepared.system[-1].get("cache_control") is None
    asyncio.run(scenario())


@pytest.mark.parametrize("chunks,status,fallback,valid", [
    (("Answer with no explicit marker.",), "available", True, True),
    (("Answer with ", "[G", "99]."), "partial", False, False),
    (("Answer with ", "[G", "1]."), "available", False, True),
])
def test_answer_citations_fallback_or_partial_preserves_valid_reference_contract(tmp_path, chunks, status, fallback, valid):
    async def scenario():
        gateway = PreparingGateway(chunks)
        query = FakeQuery()
        query.evidence["steps"] += [
            {"step_id": "empty", "status": "empty", "nodes": [], "edges": [], "rows": [], "validation": [{"valid": True}]},
            {"step_id": "failed", "status": "failed", "nodes": [], "edges": [], "rows": [], "validation": [{"valid": False}]},
        ]
        async with service(tmp_path, gateway=gateway, query=query) as s:
            created = await s.client.post("/api/results", json={"template_id": "expression_by_gene", "parameters": {"gene_id": NODE["id"]}})
            result = await finished(s.client, created.json()["result_id"])
            assert gateway.calls == 1
            assert result["component_status"]["answer"] == status
            validation = result["answer_validation"]
            assert validation["scope"] == "reference_ids_only"
            assert validation["valid"] is valid and validation["application_fallback"] is fallback
            assert "[G99]" not in result["answer"]
            if fallback:
                assert result["answer"].endswith("Graph evidence supplied: [G1].")
                assert "[G2]" not in result["answer"] and "[G3]" not in result["answer"]
            elif valid:
                assert result["answer"] == "".join(chunks)
            else:
                assert "[unverified reference]" in result["answer"]
    asyncio.run(scenario())


def test_successful_empty_model_stream_is_unavailable_without_fabricated_evidence_footer(tmp_path):
    async def scenario():
        async with service(tmp_path, gateway=PreparingGateway(("",))) as s:
            created = await s.client.post("/api/results", json={"template_id": "expression_by_gene", "parameters": {"gene_id": NODE["id"]}})
            result = await finished(s.client, created.json()["result_id"])
            assert result["component_status"]["answer"] == "unavailable"
            assert result["answer"] == "" and result["evidence"] == EVIDENCE
            assert s.gateway.calls == 1
    asyncio.run(scenario())


def test_returned_unavailable_graph_probe_cannot_be_reported_healthy(tmp_path):
    async def scenario():
        query = FakeQuery()
        async def unavailable():
            query.probes += 1
            return {"state": "unavailable", "identity_verified": False, "error_category": "identity_mismatch"}
        query.probe = unavailable
        async with service(tmp_path, query=query) as s:
            await s.runtime.health.probe()
            assert (await s.client.get("/health/ready")).status_code == 503
            component = (await s.client.get("/health/components")).json()["components"]["neo4j"]
            assert component["state"] == "unavailable"
            assert component["error_category"] == "identity_mismatch"
            assert query.probes == 1 and query.calls == s.gateway.calls == s.layout.calls == s.resources.calls == 0
    asyncio.run(scenario())


def test_agent_cancel_stops_queued_presentation_before_any_work(tmp_path):
    async def scenario():
        run = persisted_run()
        def upstream(request):
            if request.method == "GET":
                return httpx.Response(200, json=run)
            assert request.url.path == "/v2/runs/" + run["run_id"] + "/cancel"
            return httpx.Response(200, json={"run_id": run["run_id"], "status": "cancelled"})
        async with service(tmp_path, upstream_handler=upstream) as s:
            s.runtime.semaphore = asyncio.Semaphore(0)
            created = await s.client.post("/api/results", json={"run_id": run["run_id"]})
            rid = created.json()["result_id"]
            cancelled = await s.client.post("/api/agent/v2/runs/" + run["run_id"] + "/cancel")
            assert cancelled.status_code == 200
            result = (await s.client.get("/api/results/" + rid)).json()
            assert result["status"] == "cancelled"
            assert set(result["component_status"].values()) == {"cancelled"}
            assert s.query.calls == s.gateway.calls == s.layout.calls == s.resources.calls == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("dependent_failed", [True, False])
def test_query_adapter_health_distinguishes_failed_dependency_from_safety_truncation(tmp_path, dependent_failed):
    async def scenario():
        query = FakeQuery()
        query.evidence["completeness"] = "partial"
        query.evidence["truncated"] = not dependent_failed
        query.evidence["steps"].append({"step_id": "coloc_leads", "purpose": "context", "depends_on": ["lookup"],
            "status": "failed" if dependent_failed else "partial", "nodes": [], "edges": [], "rows": [],
            "truncated": not dependent_failed, "validation": [{"valid": not dependent_failed}],
            **({"error": {"category": "UNSAFE_UPSTREAM_ERROR_TEXT"}} if dependent_failed else {})})
        async with service(tmp_path, query=query) as s:
            created = await s.client.post("/api/results", json={"template_id": "coloc_by_gene", "parameters": {"gene_id": NODE["id"]}})
            result = await finished(s.client, created.json()["result_id"])
            assert result["status"] == "ready"
            assert result["evidence"] == query.evidence
            assert result["evidence"]["nodes"] == [NODE] and result["completeness"] == "partial"
            health = (await s.client.get("/health/components")).json()
            component = health["components"]["query_adapter"]
            assert component["state"] == ("degraded" if dependent_failed else "healthy")
            assert component["error_category"] == ("query_step_failed" if dependent_failed else None)
            assert component["details"]["failed_step_count"] == int(dependent_failed)
            assert "UNSAFE_UPSTREAM_ERROR_TEXT" not in json.dumps(health)
            assert s.runtime.health.counters.get("query_adapter_errors", 0) == int(dependent_failed)
            metrics = (await s.client.get("/metrics")).text
            if dependent_failed:
                assert "pank_results_query_adapter_errors_total 1" in metrics
            else:
                assert "pank_results_query_adapter_errors_total" not in metrics
            assert query.calls == s.gateway.calls == 1
    asyncio.run(scenario())
