"""Network-free acceptance checks for runtime, health, and durable replay."""

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx

from pankagent_vnext.app import CitationFilter, create_app
from pankagent_vnext.config import Settings
from pankagent_vnext.store import Store


PLAN = {
    "interpreted_question": "Which cell types express INS?",
    "steps": [{"id": "s1", "question": "Which cell types express INS?", "depends_on": [], "constraints": [], "complete": True}],
    "literature": False, "clarification": None,
}


class Budget:
    def __init__(self):
        self.remaining = 10.0

    def snapshot(self):
        return {"remaining_usd": self.remaining, "spent_usd": 10 - self.remaining, "reserved_usd": 0.0}


class Gateway:
    def __init__(self, plan=None, delay=0.01):
        self.budget = Budget()
        self.plan_value = plan or PLAN
        self.delay = delay
        self.plans = 0
        self.syntheses = 0
        self.probes = 0
        self.histories = []
        self.access = True

    async def plan(self, question, history):
        self.plans += 1
        self.histories.append(history)
        await asyncio.sleep(self.delay)
        return json.loads(json.dumps(self.plan_value))

    async def synthesize(self, question, evidence):
        self.syntheses += 1
        await asyncio.sleep(self.delay)
        for token in ("INS is linked to beta cells ", "[G", "1]."):
            yield token

    async def probe(self):
        self.probes += 1
        return {"state": "healthy" if self.access else "unavailable", "model": "test-model", "auth_ok": self.access}

    async def close(self):
        pass


class Graph:
    def __init__(self, delay=0.01):
        self.delay = delay
        self.calls = 0
        self.cancelled = 0
        self.cypher_state = "healthy"
        self.error = None
        self.previous = []

    async def execute(self, step, previous, emit):
        self.calls += 1
        self.previous.append(dict(previous))
        await emit("progress", {"stage": "generating_cypher"})
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.error:
            raise self.error
        await emit("progress", {"stage": "validating"})
        await emit("progress", {"stage": "querying_graph"})
        return {"step_id": step["id"], "status": "complete", "nodes": [{"id": "INS", "name": "INS"}], "edges": [], "rows": [], "queries": [{"cypher": "MATCH (n:Gene {name:'INS'}) RETURN n"}], "validation": [{"valid": True}], "graph_version": "test-release", "truncated": False}

    async def probe(self):
        return {"state": "healthy", "identity_verified": True, "graph_version": "test-release"}

    async def probe_cypher(self):
        return {"state": self.cypher_state, "replicas": 2 if self.cypher_state == "healthy" else 0}

    async def close(self):
        pass


class Literature:
    def __init__(self, delay=0.01, available=True):
        self.delay = delay
        self.available = available
        self.calls = 0

    async def search(self, question, conversation, emit):
        self.calls += 1
        perspective = {"perspective": "mechanism", "answer": "A supported mechanism.", "references": [{"pmid": "123", "source_type": "paper"}]}
        await emit("literature_perspective", perspective)
        await asyncio.sleep(self.delay)
        if not self.available:
            raise ConnectionError("UPSTREAM_SECRET_SHOULD_NOT_APPEAR")
        return {"status": "complete", "perspectives": [perspective], "corpus_version": "test-corpus", "source_policy": "mixed"}

    async def probe(self):
        return {"state": "healthy" if self.available else "unavailable", "source_policy": "mixed", "corpus_version": "test-corpus"}

    async def close(self):
        pass


@asynccontextmanager
async def service(tmp_path, gateway=None, graph=None, literature=None, **options):
    settings = Settings(state_dir=tmp_path, heartbeat_seconds=0.01, health_interval=1000, claude_health_interval=1000, provider_status_url="", **options)
    gateway, graph, literature = gateway or Gateway(), graph or Graph(), literature or Literature()
    app = create_app(settings, gateway, graph, literature)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            yield client, app.state.runtime, gateway, graph, literature


async def wait_state(client, run_id, states):
    for _ in range(300):
        run = (await client.get(f"/v2/runs/{run_id}")).json()
        if run["status"] in states:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"Run never reached {states}: {run}")


async def new_plan(client, question="Which cell types express INS?", **options):
    response = await client.post("/v2/plans", json={"question": question, **options})
    assert response.status_code == 202
    created = response.json()
    await wait_state(client, created["run_id"], {"awaiting_confirmation"})
    return created


def test_cell_constraint_is_persisted_before_confirmation(tmp_path):
    async def scenario():
        plan = json.loads(json.dumps(PLAN))
        question = "Is CFTR specifically enriched in ductal cells?"
        plan["interpreted_question"] = question
        plan["steps"][0].update(question=question, constraints=[{"property": "name", "operator": "=", "value": "CFTR"}])
        async with service(tmp_path, gateway=Gateway(plan=plan)) as (client, runtime, gateway, graph, literature):
            created = await new_plan(client, question)
            run = (await client.get(f'/v2/runs/{created["run_id"]}')).json()
            assert run["status"] == "awaiting_confirmation"
            assert run["plan"]["steps"][0]["constraints"] == [
                {"property": "name", "operator": "=", "value": "CFTR"},
                {"property": "name", "operator": "=", "value": "ductal cell"},
            ]
            assert graph.calls == 1
            assert run["preview"]["evidence"]["steps"][0]["status"] == "complete"
            assert gateway.syntheses == literature.calls == 0
            assert gateway.plans == 1
            assert run["plan"]["literature"] is True
            assert run["plan"]["literature_intent"]["reason"] == "always_enabled"
            ready = next(event for event in runtime.store.events_after(created["run_id"], 0)
                         if event["type"] == "plan_ready")
            assert ready["payload"]["plan"]["literature_intent"] == run["plan"]["literature_intent"]
            responses = await asyncio.gather(*[client.post(f'/v2/plans/{created["plan_id"]}/confirm') for _ in range(3)])
            assert all(response.status_code == 202 for response in responses)
            completed = await wait_state(client, created["run_id"], {"completed"})
            assert completed["literature"]["status"] == "complete"
            assert gateway.plans == gateway.syntheses == graph.calls == literature.calls == 1
            await client.get(created["events_url"])
            await client.get(f'/v2/runs/{created["run_id"]}')
            assert literature.calls == 1
    asyncio.run(scenario())


def test_literature_policy_always_enabled_without_changing_graph_scope(tmp_path):
    async def scenario():
        question = "Is KRT19 selectively expressed in ductal cells? Use graph evidence only."
        plan = json.loads(json.dumps(PLAN))
        plan.update(interpreted_question="Is KRT19 selectively expressed in ductal cells?", literature=True)
        plan["steps"][0]["question"] = plan["interpreted_question"]
        async with service(tmp_path, gateway=Gateway(plan=plan)) as (client, runtime, gateway, graph, literature):
            created = await new_plan(client, question)
            saved = runtime.store.get(created["run_id"])
            assert saved["plan"]["literature"] is True
            assert saved["plan"]["literature_intent"]["reason"] == "always_enabled"
            await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            completed = await wait_state(client, created["run_id"], {"completed"})
            assert completed["literature"]["status"] == "complete" and literature.calls == 1
            assert gateway.plans == gateway.syntheses == graph.calls == 1
    asyncio.run(scenario())


def test_confirmation_replay_and_followup_are_idempotent(tmp_path):
    async def scenario():
        async with service(tmp_path) as (client, runtime, gateway, graph, literature):
            created = await new_plan(client)
            responses = await asyncio.gather(*[client.post(f'/v2/plans/{created["plan_id"]}/confirm') for _ in range(4)])
            assert all(response.status_code == 202 for response in responses)
            assert {response.json()["run_id"] for response in responses} == {created["run_id"]}
            run = await wait_state(client, created["run_id"], {"completed"})
            assert graph.calls == gateway.syntheses == gateway.plans == 1
            assert literature.calls == 1
            assert run["evidence"]["graph_version"] == "test-release"
            assert run["graph_answer"] == "INS is linked to beta cells [G1]."
            initial = await client.get(created["events_url"])
            events = [json.loads(line[6:]) for line in initial.text.splitlines() if line.startswith("data: ")]
            assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
            assert all(event["version"] == 2 and event["session_id"] == created["session_id"] for event in events)
            replay = await client.get(created["events_url"], headers={"Last-Event-ID": str(events[-2]["sequence"])})
            assert replay.text.count("data: ") == 1
            assert graph.calls == gateway.syntheses == gateway.plans == 1
            followup = await new_plan(client, "What about GCG?", session_id=created["session_id"])
            assert followup["run_id"] != created["run_id"]
            assert gateway.histories[-1] == [{"role": "user", "content": "Which cell types express INS?"}, {"role": "assistant", "content": run["graph_answer"]}]
    asyncio.run(scenario())


def test_graph_answer_precedes_early_literature_and_heartbeats(tmp_path):
    async def scenario():
        gateway = Gateway(plan={**PLAN, "literature": True})
        async with service(tmp_path, gateway=gateway, graph=Graph(delay=0.05)) as (client, runtime, *_):
            created = await new_plan(client)
            await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            run = await wait_state(client, created["run_id"], {"completed"})
            events = runtime.store.events_after(created["run_id"], 0)
            graph_final = next(index for index, event in enumerate(events) if event["type"] == "graph_answer" and not event["payload"].get("delta"))
            lit = next(index for index, event in enumerate(events) if event["type"] == "literature_perspective")
            assert graph_final < lit
            assert any(event["type"] == "heartbeat" for event in events)
            assert not any("percent" in json.dumps(event) for event in events)
            assert run["literature"]["perspectives"][0]["references"][0]["pmid"] == "123"
    asyncio.run(scenario())


def test_literature_failure_preserves_graph_and_health_is_independent(tmp_path):
    async def scenario():
        gateway = Gateway(plan={**PLAN, "literature": True})
        async with service(tmp_path, gateway=gateway, literature=Literature(available=False)) as (client, runtime, *_):
            created = await new_plan(client)
            await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            run = await wait_state(client, created["run_id"], {"partial"})
            assert run["graph_answer"]
            assert run["literature"]["status"] == "unavailable"
            assert "UPSTREAM_SECRET" not in json.dumps(run)
            await runtime.health.refresh(online=True)
            assert (await client.get("/health/ready")).status_code == 200
            health = (await client.get("/health/components")).json()
            assert health["components"]["hirn"]["state"] == "unavailable"
    asyncio.run(scenario())


def test_health_polling_no_inference_and_dependency_failures(tmp_path):
    async def scenario():
        async with service(tmp_path) as (client, runtime, gateway, graph, literature):
            await runtime.health.refresh(online=True)
            probes = gateway.probes
            for _ in range(8):
                assert (await client.get("/health/live")).status_code == 200
                assert (await client.get("/health/ready")).status_code == 200
                health = (await client.get("/health/components")).json()
            assert gateway.plans == gateway.syntheses == graph.calls == literature.calls == 0
            assert gateway.probes == probes
            assert health["components"]["claude"]["recent_inference"]["state"] == "unknown"
            graph.cypher_state = "unavailable"
            await runtime.health.refresh()
            assert (await client.get("/health/ready")).status_code == 503
            graph.cypher_state = "healthy"
            gateway.access = False
            await runtime.health.refresh(online=True)
            assert (await client.get("/health/ready")).status_code == 503
            gateway.access = True
            await runtime.health.refresh(online=True)
            runtime.health.observations["claude"]["checked_epoch"] = time.time() - 4000
            assert (await client.get("/health/ready")).status_code == 503
            gateway.budget.remaining = 0
            health = (await client.get("/health/components")).json()
            assert health["components"]["runtime"]["error_category"] == "budget_exhausted"
    asyncio.run(scenario())


def test_cancellation_does_not_restart_on_reconnect(tmp_path):
    async def scenario():
        async with service(tmp_path, graph=Graph(delay=5)) as (client, runtime, gateway, graph, literature):
            created = (await client.post("/v2/plans", json={"question": "Which cell types express INS?"})).json()
            for _ in range(100):
                if graph.calls:
                    break
                await asyncio.sleep(0.01)
            response = await client.post(f'/v2/runs/{created["run_id"]}/cancel')
            assert response.json()["status"] == "cancelled"
            await asyncio.sleep(0.02)
            assert graph.cancelled == 1
            assert (await client.get(created["events_url"])).status_code == 200
            assert graph.calls == 1 and gateway.syntheses == 0
            assert (await client.post(f'/v2/plans/{created["plan_id"]}/confirm')).status_code == 409
    asyncio.run(scenario())


def test_restart_interrupts_work_but_preserves_unconfirmed_plan(tmp_path):
    store = Store(tmp_path)
    active = store.create("unfinished")
    waiting = store.create("waiting")
    store.update(waiting["run_id"], status="awaiting_confirmation", stage="awaiting_confirmation", plan=PLAN)
    store.close()

    async def scenario():
        async with service(tmp_path) as (client, runtime, *_):
            assert (await client.get(f'/v2/runs/{active["run_id"]}')).json()["status"] == "interrupted"
            assert (await client.get(f'/v2/runs/{waiting["run_id"]}')).json()["status"] == "awaiting_confirmation"
            response = await client.post(f'/v2/plans/{waiting["plan_id"]}/confirm')
            assert response.status_code == 409
            assert "Revise this saved plan" in response.json()["detail"]
    asyncio.run(scenario())


def test_queue_is_bounded_and_health_remains_responsive(tmp_path):
    async def scenario():
        async with service(tmp_path, gateway=Gateway(delay=5), max_concurrent=1, max_queue=1) as (client, runtime, *_):
            assert (await client.post("/v2/plans", json={"question": "first"})).status_code == 202
            assert (await client.post("/v2/plans", json={"question": "second"})).status_code == 202
            assert (await client.post("/v2/plans", json={"question": "third"})).status_code == 429
            await runtime.health.refresh(online=True)
            ready = (await client.get("/health/ready"))
            assert ready.status_code == 503
            health = (await client.get("/health/components")).json()
            assert health["components"]["runtime"]["error_category"] == "queue_full"
            started = time.monotonic()
            assert (await client.get("/health/live")).status_code == 200
            assert time.monotonic() - started < 0.5
    asyncio.run(scenario())


def test_step_errors_are_visible_sanitized_and_dependencies_passed(tmp_path):
    async def scenario():
        plan = json.loads(json.dumps(PLAN))
        plan["steps"].append({"id": "s2", "question": "Find pathways for those genes", "depends_on": ["s1"], "constraints": [], "complete": True})
        graph = Graph()
        graph.error = ConnectionError("a token is SECRET_VALUE")
        async with service(tmp_path, gateway=Gateway(plan=plan), graph=graph) as (client, runtime, *_):
            created = await new_plan(client)
            response = await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            assert response.status_code == 409
            run = await wait_state(client, created["run_id"], {"awaiting_confirmation"})
            assert run["preview"]["evidence"]["steps"][0]["status"] == "failed"
            assert graph.previous[1]["s1"]["status"] == "failed"
            assert "SECRET_VALUE" not in json.dumps(run)
    asyncio.run(scenario())


def test_timeout_preserves_partial_evidence_and_stops_waiting(tmp_path):
    async def scenario():
        async with service(tmp_path, graph=Graph(delay=5), preview_timeout=0.03, run_timeout=0.03) as (client, runtime, *_):
            created = await new_plan(client)
            response = await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            assert response.status_code == 409
            run = await wait_state(client, created["run_id"], {"awaiting_confirmation"})
            assert run["preview"]["error"]["category"] == "timeout"
            assert run["preview"]["evidence"]["completeness"] == "partial"
    asyncio.run(scenario())


def test_clarification_cannot_be_confirmed(tmp_path):
    async def scenario():
        async with service(tmp_path, gateway=Gateway(plan={**PLAN, "steps": [], "clarification": "Which gene?"})) as (client, runtime, gateway, graph, literature):
            created = await new_plan(client)
            assert (await client.post(f'/v2/plans/{created["plan_id"]}/confirm')).status_code == 409
            assert graph.calls == 0
    asyncio.run(scenario())


def test_operator_health_restriction_and_no_credential_echo(tmp_path):
    async def scenario():
        async with service(tmp_path, operator_token="test-operator-secret") as (client, runtime, *_):
            assert (await client.get("/health/components")).status_code == 403
            response = await client.get("/health/components", headers={"Authorization": "Bearer test-operator-secret"})
            assert response.status_code == 200
            assert "test-operator-secret" not in response.text
            assert (await client.get("/metrics")).status_code == 403
    asyncio.run(scenario())


def test_invalid_evidence_reference_is_filtered_across_tokens():
    filter = CitationFilter(2)
    assert filter.feed("Valid [G") == "Valid "
    assert filter.feed("1] and invalid [G9") == "[G1] and invalid "
    assert filter.feed("99].") == "[unverified reference]."
    assert filter.invalid


def test_inference_billing_failure_blocks_readiness_until_success(tmp_path):
    async def scenario():
        async with service(tmp_path) as (client, runtime, *_):
            await runtime.health.refresh(online=True)
            assert (await client.get("/health/ready")).status_code == 200
            runtime.health.record_inference("claude", False, "billing")
            # A successful model-access lookup does not verify paid inference.
            await runtime.health.refresh(online=True)
            assert (await client.get("/health/ready")).status_code == 503
            health = (await client.get("/health/components")).json()
            assert health["components"]["claude"]["state"] == "unavailable"
            assert health["components"]["claude"]["error_category"] == "billing"
            runtime.health.record_inference("claude", True)
            assert (await client.get("/health/ready")).status_code == 200
    asyncio.run(scenario())


def test_budget_exhaustion_rejects_new_work_before_gpu_calls(tmp_path):
    async def scenario():
        async with service(tmp_path) as (client, runtime, gateway, graph, _):
            created = await new_plan(client)
            gateway.budget.remaining = 0
            assert (await client.post(f'/v2/plans/{created["plan_id"]}/confirm')).status_code == 503
            assert (await client.post("/v2/plans", json={"question": "Another question"})).status_code == 503
            assert gateway.plans == graph.calls == 1
            assert gateway.syntheses == 0
            assert (await client.get(f'/v2/runs/{created["run_id"]}')).json()["status"] == "awaiting_confirmation"
    asyncio.run(scenario())


def test_cancelled_plan_is_not_resurrected_by_upstream_cancellation_suppression(tmp_path):
    class SuppressedCancellationGateway(Gateway):
        async def plan(self, question, history):
            self.plans += 1
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                return json.loads(json.dumps(PLAN))

    async def scenario():
        async with service(tmp_path, gateway=SuppressedCancellationGateway()) as (client, runtime, gateway, *_):
            created = (await client.post("/v2/plans", json={"question": "Question"})).json()
            while gateway.plans == 0:
                await asyncio.sleep(0.01)
            await client.post(f'/v2/runs/{created["run_id"]}/cancel')
            await asyncio.sleep(0.03)
            run = (await client.get(f'/v2/runs/{created["run_id"]}')).json()
            assert run["status"] == "cancelled"
            assert run["plan"] is None
            assert not any(event["type"] == "plan_ready" for event in runtime.store.events_after(created["run_id"], 0))
    asyncio.run(scenario())


def test_stale_inference_is_distinct_from_fresh_access_probe(tmp_path):
    async def scenario():
        async with service(tmp_path) as (client, runtime, *_):
            await runtime.health.refresh(online=True)
            runtime.health.record_inference("claude", True)
            runtime.health.inference["claude"]["checked_at"] = "2020-01-01T00:00:00+00:00"
            health = (await client.get("/health/components")).json()
            assert health["components"]["claude"]["state"] == "healthy"
            assert health["components"]["claude"]["recent_inference"]["state"] == "unknown"
            assert health["components"]["claude"]["recent_inference"]["stale"] is True
    asyncio.run(scenario())
