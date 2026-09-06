"""Network-free contracts from BIM selection through streamed, durable answers.

These verify orchestration and the evidence/prompt contract, not whether a mocked
model can reason scientifically. No provider or shared graph calls are made.
"""

import asyncio
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from pankagent_vnext.app import create_app
from pankagent_vnext.config import Settings
from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.store import Store


QUESTION = "Which cell types express INS in T1D?"
USAGE = {
    "input_tokens": 1000,
    "output_tokens": 100,
    "cache_creation_input_tokens": 400,
    "cache_read_input_tokens": 500,
}


def detection_evidence():
    return {
        "s1": {
            "step_id": "s1",
            "status": "complete",
            "graph_version": "synthetic-test-release",
            "truncated": False,
            "nodes": [
                {"id": "NCBIGene:3630", "labels": ["Gene"], "properties": {"name": "INS"}},
                {"id": "CL:0000169", "labels": ["anatomical_structure"], "properties": {"name": "beta cell"}},
            ],
            "edges": [{
                "start_id": "NCBIGene:3630", "end_id": "CL:0000169", "type": "GENE_DETECTED_IN",
                "properties": {"condition": "T1D", "median_donor_cpm": 12.34567, "expression_call": "detected"},
            }],
            "rows": [],
            "queries": [{"cypher": "PRIVATE_QUERY_SENTINEL", "parameters": {"private": "PRIVATE_PARAMETER_SENTINEL"}}],
            "validation": [{"valid": True, "n": 1}],
        },
    }


class MockStream:
    def __init__(self, owner):
        self.owner = owner

    async def __aenter__(self):
        if self.owner.on_enter:
            self.owner.on_enter()
        return self

    async def __aexit__(self, *_):
        return False

    @property
    def text_stream(self):
        async def pieces():
            for token in self.owner.tokens:
                await asyncio.sleep(0)
                yield token
        return pieces()

    async def get_final_message(self):
        self.owner.final_messages += 1
        return SimpleNamespace(
            usage=SimpleNamespace(model_dump=lambda: deepcopy(USAGE)),
            stop_reason="end_turn",
        )


class MockClaude:
    def __init__(self, tokens):
        self.tokens = tokens
        self.stream_calls = []
        self.create_calls = []
        self.final_messages = 0
        self.on_enter = None
        self.messages = self
        self.closed = False

    def stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return MockStream(self)

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        raise AssertionError("Routing and formatting must not make additional model calls")

    async def close(self):
        self.closed = True


def gateway_with_mock(monkeypatch, tmp_path, tokens, gateway_class=ClaudeGateway):
    fake = MockClaude(tokens)
    monkeypatch.setattr("pankagent_vnext.llm.anthropic.AsyncAnthropic", lambda **_: fake)
    settings = Settings(
        state_dir=tmp_path, anthropic_key="synthetic-test-credential", model="claude-sonnet-5",
        heartbeat_seconds=0.01, health_interval=1000, claude_health_interval=1000,
        provider_status_url="",
    )
    return gateway_class(settings), fake, settings


def selected_ids(prepared):
    return {rule["id"] for rule in prepared.profile["selected_rules"]}


def test_prepared_bim_answer_uses_one_stream_and_settles_actual_cost(monkeypatch, tmp_path):
    async def scenario():
        tokens = ["INS was detected in beta cells. [G1]\n\n", "| Cell | Condition | CPM |\n", "|---|---|---:|\n| beta cell | T1D | 12.34567 |"]
        gateway, fake, _ = gateway_with_mock(monkeypatch, tmp_path, tokens)
        evidence = detection_evidence()
        original = deepcopy(evidence)
        try:
            prepared = gateway.prepare_answer(QUESTION, evidence)
            assert gateway.budget.snapshot()["calls"] == 0
            assert fake.stream_calls == fake.create_calls == []
            assert "edge.gene_detected_in" in selected_ids(prepared)
            assert "edge.part_of_qtl_signal" not in selected_ids(prepared)
            style, matched, contract = [block["text"] for block in prepared.system]
            assert "compact Markdown tables" in style
            assert "exact values, units" in style
            assert "meaningful names alongside stable identifiers" in style
            assert "[edge.gene_detected_in]" in matched
            assert all(block["cache_control"] == {"type": "ephemeral"} for block in prepared.system[:-1])
            assert sum("cache_control" in block for block in prepared.system) == 2
            assert "cache_control" not in prepared.system[-1]
            assert "answer summary only, without follow-up questions or suggested searches" in contract
            assert "80–160 words" in contract
            assert "Preserve IDs and units exactly" in contract
            assert "Include only returned entities and supported observations" in contract
            assert "Never infer unreturned records from generation settings" in contract
            assert prepared.profile["source_commit"] == "40cb7f5b08a2082a4f67ae7198591d92fa0c175d"
            body = json.loads(prepared.body)
            assert body["question"] == QUESTION
            assert body["evidence"][0]["evidence_id"] == "G1"
            assert body["evidence"][0]["edges"][0]["properties"] == original["s1"]["edges"][0]["properties"]
            assert "PRIVATE_QUERY_SENTINEL" not in prepared.body
            assert "PRIVATE_PARAMETER_SENTINEL" not in prepared.body

            def no_second_preparation(*_):
                raise AssertionError("A supplied PreparedAnswer must be consumed without rerouting")

            gateway.prepare_answer = no_second_preparation
            reserved_during_stream = []
            fake.on_enter = lambda: reserved_during_stream.append(gateway.budget.snapshot())
            answer = "".join([text async for text in gateway.synthesize(QUESTION, evidence, prepared=prepared)])
            assert answer == "".join(tokens)
            assert len(fake.stream_calls) == fake.final_messages == 1
            assert fake.create_calls == []
            call = fake.stream_calls[0]
            assert call["model"] == "claude-sonnet-5"
            assert call["thinking"] == {"type": "disabled"}
            assert not {"temperature", "top_p", "top_k", "tools", "tool_choice"} & call.keys()
            assert call["system"] == prepared.system
            assert call["messages"] == [{"role": "user", "content": prepared.body}]
            assert reserved_during_stream[0]["pending_calls"] == 1
            assert reserved_during_stream[0]["reserved_usd"] > 0
            settled = gateway.budget.snapshot()
            # Includes cache writes and reads; charging the reservation would be larger.
            assert settled["spent_usd"] == pytest.approx(0.0041)
            assert settled["calls"] == 1
            assert settled["pending_calls"] == 0
            assert settled["reserved_usd"] == 0
            assert settled["input_tokens"] == USAGE["input_tokens"]
            assert settled["output_tokens"] == USAGE["output_tokens"]
            assert settled["cache_creation_tokens"] == USAGE["cache_creation_input_tokens"]
            assert settled["cache_read_tokens"] == USAGE["cache_read_input_tokens"]
            with sqlite3.connect(gateway.budget.path) as db:
                assert db.execute("SELECT purpose FROM usage").fetchall() == [("synthesis",)]
            assert evidence == original
        finally:
            await gateway.close()
    asyncio.run(scenario())


def test_preparation_routes_full_schema_before_bounded_context(monkeypatch, tmp_path):
    async def scenario():
        gateway, fake, _ = gateway_with_mock(monkeypatch, tmp_path, [])
        evidence = detection_evidence()
        step = evidence["s1"]
        step["nodes"] += [{"id": f"synthetic-gene-{i}", "labels": ["Gene"], "properties": {"name": f"gene-{i}"}} for i in range(65)]
        # This ordinary Gene record falls beyond the model's full-record cap.
        step["nodes"][-1]["properties"]["t1d_stage"] = "recorded synthetic metadata"
        step["edges"] *= 120
        # The rare edge is beyond a naive first-100 slice, but must route and survive.
        step["edges"].append({
            "start_id": "NCBIGene:3630", "end_id": "synthetic-gene-0", "type": "PHYSICAL_INTERACTION",
            "properties": {"source": "synthetic assay", "score": 0.87654321},
        })
        original = deepcopy(evidence)
        try:
            prepared = gateway.prepare_answer(QUESTION, evidence)
            compact = json.loads(prepared.body)["evidence"][0]
            assert {"edge.gene_detected_in", "edge.physical_interaction", "clinical.recorded_t1d_stage"} <= selected_ids(prepared)
            assert prepared.profile["matched_schema"]["edges"] == ["GENE_DETECTED_IN", "PHYSICAL_INTERACTION"]
            assert prepared.profile["clinical_fields"] == ["t1d_stage"]
            assert prepared.profile["context_sampled"] is True
            assert compact["context_sampled"] is True
            assert compact["truncated"] is False
            assert compact["status"] == "complete"
            assert "synthetic-gene-64" not in {node["id"] for node in compact["nodes"]}
            rare = [edge for edge in compact["edges"] if edge["type"] == "PHYSICAL_INTERACTION"]
            assert rare == [original["s1"]["edges"][-1]]
            assert compact["context_dropped"]["edges_by_type"]["GENE_DETECTED_IN"] > 0
            assert evidence == original
            assert fake.stream_calls == fake.create_calls == []
        finally:
            await gateway.close()
    asyncio.run(scenario())


def test_matched_functional_clinical_guidance_keeps_evidence_priority(monkeypatch, tmp_path):
    async def scenario():
        gateway, _, _ = gateway_with_mock(monkeypatch, tmp_path, [])
        evidence = detection_evidence()
        evidence["s1"]["nodes"].append({
            "id": "synthetic-sample", "labels": ["Sample_node"],
            "properties": {"INS-basal (ng/100 IEQs/min)": 0.1234567, "diabetes_type": "T2D", "t1d_stage": "not recorded"},
        })
        try:
            prepared = gateway.prepare_answer("Explain the recorded measurements.", evidence)
            assert {"node.sample", "functional.measurement_rules", "functional.feature:INS-basal (ng/100 IEQs/min)", "clinical.recorded_t1d_stage"} <= selected_ids(prepared)
            assert prepared.profile["functional_features"] == ["INS-basal (ng/100 IEQs/min)"]
            assert json.loads(prepared.body)["evidence"][0]["nodes"][-1]["properties"] == evidence["s1"]["nodes"][-1]["properties"]
            style = prepared.system[0]["text"]
            # Guardrails precede matched source text containing older assumptions.
            for requirement in (
                "Actual evidence schema and recorded conditions take precedence",
                "Do not convert CPM to logCPM",
                "not additional observations",
                "enrichment alone does not establish a validated marker",
                "none alone proves causality",
                "Co-occurring types anywhere in a result are not a joined mechanism",
                "Preserve ng versus pg, rate versus AUC, SI versus II",
                "Never assign T1D or Stage 3 from hyperglycemia alone",
                "override a recorded T2D classification",
                "context_sampled or context_content_omissions",
            ):
                assert requirement in style
            assert "[functional.feature:INS-basal (ng/100 IEQs/min)]" in prepared.system[1]["text"]
            assert "[functional.feature:GCG-basal" not in prepared.system[1]["text"]
        finally:
            await gateway.close()
    asyncio.run(scenario())


def test_empty_and_failed_steps_remain_visible_without_unmatched_bim_guidance(monkeypatch, tmp_path):
    async def scenario():
        gateway, _, _ = gateway_with_mock(monkeypatch, tmp_path, [])
        evidence = {
            "s1": {"step_id": "s1", "status": "empty", "nodes": [], "edges": [], "rows": [], "truncated": False},
            "s2": {"step_id": "s2", "status": "failed", "nodes": [], "edges": [], "rows": [], "validation": [{"valid": False, "reasons": ["missing_required_predicate"]}]},
        }
        try:
            prepared = gateway.prepare_answer(QUESTION, evidence)
            body = json.loads(prepared.body)
            assert [(step["evidence_id"], step["status"]) for step in body["evidence"]] == [("G1", "empty"), ("G2", "failed")]
            assert body["evidence"][1]["validation"] == [{"valid": False, "reasons": ["missing_required_predicate"]}]
            assert selected_ids(prepared) == set()
            assert len(prepared.system) == 2
            assert sum("cache_control" in block for block in prepared.system) == 1
            assert "cache_control" not in prepared.system[-1]
            assert "answer summary only, without follow-up questions" in prepared.system[-1]["text"]
            assert "Empty results mean no matching evidence was retrieved, not biological absence" in prepared.system[0]["text"]
            assert "Failed validation or queries cannot support a biological conclusion" in prepared.system[0]["text"]
        finally:
            await gateway.close()
    asyncio.run(scenario())


class RuntimeGateway(ClaudeGateway):
    """Keep real preparation/synthesis, replacing only planning and access probes."""

    def __init__(self, settings):
        super().__init__(settings)
        self.plan_calls = 0
        self.prepare_calls = 0
        self.prepared = None

    async def plan(self, question, history):
        self.plan_calls += 1
        return {
            "interpreted_question": question, "entities": [{"name": "INS", "type": "Gene"}],
            "steps": [{"id": "s1", "question": question, "depends_on": [], "constraints": [], "complete": True}],
            "literature": False, "clarification": None,
        }

    def prepare_answer(self, question, evidence):
        self.prepare_calls += 1
        self.prepared = super().prepare_answer(question, evidence)
        return self.prepared

    async def probe(self):
        return {"state": "healthy", "model": self.settings.model, "auth_ok": True}


class RuntimeGraph:
    def __init__(self):
        self.calls = 0

    async def execute(self, step, previous, emit):
        self.calls += 1
        assert previous == {}
        await emit("progress", {"stage": "querying_graph"})
        return detection_evidence()["s1"]

    async def probe(self):
        return {"state": "healthy", "identity_verified": True, "graph_version": "synthetic-test-release"}

    async def probe_cypher(self):
        return {"state": "healthy", "replicas": 1}

    async def close(self):
        pass


class UnrequestedLiterature:
    async def search(self, *_):
        raise AssertionError("A graph-only plan must not invoke literature")

    async def probe(self):
        return {"state": "healthy", "source_policy": "mixed", "corpus_version": "synthetic"}

    async def close(self):
        pass


async def await_state(client, run_id, expected):
    for _ in range(300):
        response = await client.get(f"/v2/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["status"] in expected:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"Run did not reach {expected}: {run}")


def sse_events(response):
    assert response.status_code == 200
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


@pytest.mark.parametrize("invalid_reference", [False, True])
def test_answer_profile_persists_and_replays_once_with_citation_filter(monkeypatch, tmp_path, invalid_reference):
    async def scenario():
        tokens = ["INS was detected in beta cells ", "[G", "1]."]
        if invalid_reference:
            tokens += [" Unsupported reference ", "[G9", "9]."]
        gateway, fake, settings = gateway_with_mock(monkeypatch, tmp_path, tokens, RuntimeGateway)
        graph = RuntimeGraph()
        app = create_app(settings, gateway, graph, UnrequestedLiterature())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                response = await client.post("/v2/plans", json={"question": QUESTION})
                assert response.status_code == 202
                created = response.json()
                run_id = created["run_id"]
                await await_state(client, run_id, {"awaiting_confirmation"})
                confirmations = await asyncio.gather(*[client.post(f'/v2/plans/{created["plan_id"]}/confirm') for _ in range(3)])
                assert all(response.status_code == 202 for response in confirmations)
                expected_status = "partial" if invalid_reference else "completed"
                run = await await_state(client, run_id, {expected_status})
                assert graph.calls == gateway.plan_calls == gateway.prepare_calls == 1
                assert len(fake.stream_calls) == 1
                assert fake.create_calls == []
                profile = run["evidence"]["answer_profile"]
                assert profile == gateway.prepared.profile
                assert "edge.gene_detected_in" in selected_ids(gateway.prepared)
                assert profile["style_version"]
                assert profile["source_commit"]
                assert profile["context_sampled"] is False
                assert run["evidence"]["answer_reference_validation"]["valid"] is not invalid_reference
                assert "[G1]" in run["graph_answer"]
                assert "[G99]" not in run["graph_answer"]
                if invalid_reference:
                    assert "[unverified reference]" in run["graph_answer"]

                events = sse_events(await client.get(created["events_url"]))
                profiles = [event for event in events if event["type"] == "answer_profile"]
                assert len(profiles) == 1
                profile_event = profiles[0]
                assert profile_event["payload"] == {"profile": profile}
                assert profile_event["run_id"] == run_id
                assert profile_event["session_id"] == created["session_id"]
                graph_events = [event for event in events if event["type"] == "graph_answer"]
                assert profile_event["sequence"] < graph_events[0]["sequence"]
                assert graph_events[-1]["payload"]["evidence"]["answer_profile"] == profile
                deltas = "".join(event["payload"]["text"] for event in graph_events if event["payload"].get("delta"))
                assert deltas == run["graph_answer"]
                assert "[G99]" not in deltas
                assert events[-1]["type"] == "terminal"

                replay = sse_events(await client.get(created["events_url"], headers={"Last-Event-ID": str(profile_event["sequence"] - 1)}))
                assert replay == [event for event in events if event["sequence"] >= profile_event["sequence"]]
                assert gateway.prepare_calls == len(fake.stream_calls) == 1
                assert gateway.budget.snapshot()["calls"] == 1
                assert gateway.budget.snapshot()["spent_usd"] == pytest.approx(0.0041)
                assert gateway.budget.snapshot()["pending_calls"] == 0
        assert fake.closed is True
        reopened = Store(tmp_path)
        try:
            assert reopened.get(run_id)["evidence"]["answer_profile"] == profile
            persisted = [event for event in reopened.events_after(run_id, 0) if event["type"] == "answer_profile"]
            assert persisted == [profile_event]
        finally:
            reopened.close()
    asyncio.run(scenario())


def test_local_preparation_failure_preserves_graph_and_successful_claude_health(monkeypatch, tmp_path):
    class PreparationFailure(RuntimeGateway):
        def prepare_answer(self, question, evidence):
            self.prepare_calls += 1
            raise ValueError("LOCAL_PREPARATION_DETAIL_MUST_NOT_LEAK")

        def synthesize(self, *args, **kwargs):
            raise AssertionError("Synthesis must not be entered after local preparation fails")

    async def scenario():
        gateway, fake, settings = gateway_with_mock(monkeypatch, tmp_path, [], PreparationFailure)
        graph = RuntimeGraph()
        app = create_app(settings, gateway, graph, UnrequestedLiterature())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                response = await client.post("/v2/plans", json={"question": QUESTION})
                assert response.status_code == 202
                created = response.json()
                await await_state(client, created["run_id"], {"awaiting_confirmation"})
                runtime = app.state.runtime
                completed_planning_health = deepcopy(runtime.health.inference["claude"])
                assert completed_planning_health["state"] == "healthy"
                assert completed_planning_health["last_success"]

                response = await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
                assert response.status_code == 202
                run = await await_state(client, created["run_id"], {"partial"})
                assert graph.calls == gateway.plan_calls == gateway.prepare_calls == 1
                assert fake.stream_calls == fake.create_calls == []
                assert gateway.budget.snapshot()["calls"] == 0
                assert runtime.health.inference["claude"] == completed_planning_health
                health = (await client.get("/health/components")).json()
                assert health["components"]["claude"]["recent_inference"]["state"] == "healthy"
                assert health["components"]["claude"]["recent_inference"]["last_success"] == completed_planning_health["last_success"]

                evidence = run["evidence"]
                assert evidence["nodes"] == detection_evidence()["s1"]["nodes"]
                assert evidence["edges"] == detection_evidence()["s1"]["edges"]
                assert evidence["completeness"] == "complete"
                assert evidence["answer_preparation_error"] == evidence["synthesis_error"]
                assert "answer_profile" not in evidence
                assert "graph evidence is available" in run["graph_answer"]
                assert "LOCAL_PREPARATION_DETAIL_MUST_NOT_LEAK" not in json.dumps(run)
                metrics = await client.get("/metrics")
                assert 'pankagent_events_total{kind="answer_preparation_errors"} 1' in metrics.text
                events = sse_events(await client.get(created["events_url"]))
                assert not any(event["type"] == "answer_profile" for event in events)
                final = [event for event in events if event["type"] == "graph_answer"][-1]
                assert final["payload"]["evidence"] == evidence
                assert events[-1]["payload"]["status"] == "partial"
    asyncio.run(scenario())
