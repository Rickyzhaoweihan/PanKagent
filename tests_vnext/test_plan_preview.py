"""Validated previews, revision races, and bounded confirmation reuse."""

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
import threading
import time

import pytest

from pankagent_vnext.store import Store
from test_runtime import Gateway, Graph, PLAN, new_plan, service, wait_state


class BiologicalGateway(Gateway):
    async def plan(self, question, history):
        plan = await super().plan(question, history)
        plan["interpreted_question"] = question
        for step in plan["steps"]:
            step["question"] = question
        return plan


class PreviewGraph(Graph):
    def __init__(self, *, outcomes=None, add_context=False, block_step=None, suppress_cancel=False):
        super().__init__(delay=0)
        self.prepares = []
        self.outcomes = outcomes or {}
        self.step_calls = Counter()
        self.identity = {"graph_version": "test-release", "identity_manifest_sha256": "fixture-release"}
        self.add_context = add_context
        self.block_step = block_step
        self.suppress_cancel = suppress_cancel
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.preparation_error = None

    def preview_identity(self):
        return dict(self.identity)

    async def prepare_plan(self, plan, emit):
        self.prepares.append(deepcopy(plan))
        await emit("progress", {"stage": "resolving_entities"})
        if self.preparation_error:
            raise self.preparation_error
        gene = "GCG" if "GCG" in plan["interpreted_question"] else "INS"
        for step in plan["steps"]:
            step["resolved_entities"] = [{"state": "resolved", "id": gene, "name": gene, "labels": ["Gene"], "graph_version": "test-release"}]
            step["entity_resolution"] = {"state": "resolved", "graph_version": "test-release"}
        if self.add_context and plan["include_context"] and len(plan["steps"]) < 3:
            plan["steps"].append({"id": "context", "question": f"Show the recorded context for {gene}", "depends_on": [], "constraints": [], "complete": True,
                                  "purpose": "context", "context_for": plan["steps"][0]["id"], "title": "Recorded context", "rationale": "Explain the returned relation."})
        return plan

    async def execute(self, step, previous, emit):
        self.calls += 1
        self.step_calls[step["id"]] += 1
        self.previous.append(deepcopy(previous))
        await emit("progress", {"stage": "querying_graph"})
        if self.block_step == step["id"]:
            self.blocked.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                if not self.suppress_cancel:
                    raise
        options = self.outcomes.get(step["id"], ["complete"])
        status = options[min(self.step_calls[step["id"]] - 1, len(options) - 1)]
        if isinstance(status, Exception):
            raise status
        gene = "GCG" if "GCG" in step["question"] else "INS"
        return {"step_id": step["id"], "status": status, "graph_version": self.identity["graph_version"],
                "nodes": [{"id": gene, "labels": ["Gene"], "properties": {"name": gene}}] if status not in {"failed", "empty"} else [],
                "edges": [], "rows": [], "queries": [{"cypher": "MATCH (g:Gene {name:$gene}) RETURN g", "parameters": {"gene": gene}}],
                "validation": [{"valid": status != "failed"}], "truncated": status == "partial"}


def multi_plan(dependent=False):
    plan = deepcopy(PLAN)
    plan["steps"] += [
        {"id": "s2", "question": "Second requested step", "depends_on": ["s1"] if dependent else [], "constraints": [], "complete": True},
        {"id": "s3", "question": "Independent requested step", "depends_on": [], "constraints": [], "complete": True},
    ]
    return plan


def test_preview_precedes_confirmation_and_reuses_validated_results(tmp_path):
    async def scenario():
        graph = PreviewGraph(add_context=True)
        async with service(tmp_path, graph=graph, gateway=BiologicalGateway(plan={**PLAN, "literature": True})) as (client, runtime, gateway, graph, literature):
            created = await new_plan(client)
            ready = (await client.get(created["plan_url"])).json()
            assert ready["status"] == "awaiting_confirmation"
            assert ready["preview"]["status"] == "complete"
            assert ready["preview"]["pending_step_ids"] == []
            assert len(ready["preview"]["evidence"]["steps"]) == graph.calls == 2
            assert ready["plan"]["steps"][0]["resolved_entities"][0]["id"] == "INS"
            assert ready["plan"]["steps"][1]["purpose"] == "context"
            assert gateway.plans == len(graph.prepares) == 1
            assert gateway.syntheses == literature.calls == 0
            assert ready["graph_answer"] is None and ready["evidence"] is None
            assert "preview_cache" not in ready
            prior_events = runtime.store.events_after(created["run_id"], 0)
            assert ready["event_sequence"] == prior_events[-1]["sequence"]
            assert [event["type"] for event in prior_events].count("preview_step") == 2
            assert prior_events[-1]["type"] == "plan_ready"
            assert prior_events[-1]["payload"]["preview"] == ready["preview"]
            partial = next(event["payload"]["preview"] for event in prior_events if event["type"] == "preview_step")
            assert partial["status"] == "partial"
            assert partial["pending_step_ids"] == ["context"]
            replies = await asyncio.gather(*[client.post(f'/v2/plans/{created["plan_id"]}/confirm') for _ in range(4)])
            assert all(reply.status_code == 202 for reply in replies)
            done = await wait_state(client, created["run_id"], {"completed"})
            assert graph.calls == 2
            assert gateway.syntheses == literature.calls == 1
            assert done["evidence"]["preview_reuse"]["reused_step_ids"] == ["s1", "context"]
            assert done["evidence"]["preview_reuse"]["retrieved_step_ids"] == []
            confirmation_events = runtime.store.events_after(created["run_id"], prior_events[-1]["sequence"])
            stages = [event["payload"].get("stage") for event in confirmation_events if event["type"] == "progress"]
            assert "preparing_execution" in stages
            assert "generating_cypher" not in stages
            assert "querying_graph" not in stages
            replay = await client.get(created["events_url"])
            again = await client.get(created["events_url"])
            assert replay.content == again.content
            tail = await client.get(created["events_url"], headers={"Last-Event-ID": str(done["event_sequence"])})
            assert tail.content == b""
            assert graph.calls == 2 and gateway.syntheses == 1
            assert runtime.metrics.duration_counts["model_plan"] == runtime.metrics.duration_counts["preview"] == 1
            assert runtime.metrics.counts["preview_reused_steps"] == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["complete", "empty", "partial"])
def test_successful_preflight_outcome_is_reused_without_completeness_upgrade(tmp_path, status):
    async def scenario():
        graph = PreviewGraph(outcomes={"s1": [status]})
        async with service(tmp_path, graph=graph) as (client, runtime, gateway, *_):
            created = await new_plan(client)
            await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            done = await wait_state(client, created["run_id"], {"partial" if status == "partial" else "completed"})
            assert graph.calls == 1
            assert done["evidence"]["steps"][0]["status"] == status
            assert done["evidence"]["completeness"] == status
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["plan", "graph", "credentials", "limit", "expiry", "validation"])
def test_preview_reuse_requires_exact_fresh_plan_graph_and_access(tmp_path, change):
    async def scenario():
        graph = PreviewGraph()
        async with service(tmp_path, graph=graph) as (client, runtime, *_):
            created = await new_plan(client)
            run = runtime.store.get(created["run_id"])
            if change == "plan":
                plan = run["plan"]
                plan["steps"][0]["question"] = "Which cell types express GCG?"
                runtime.store.update(run["run_id"], plan=plan)
            elif change == "graph":
                graph.identity["identity_manifest_sha256"] = "new-release-identity"
            elif change == "credentials":
                runtime.settings.neo4j_password = "PRIVATE_NEW_ACCESS_SENTINEL"
            elif change == "limit":
                runtime.settings.max_nodes += 1
            elif change == "expiry":
                cache = run["preview_cache"]
                cache["step_completed_epochs"]["s1"] = time.time() - 301
                runtime.store.update(run["run_id"], preview_cache=cache)
            else:
                preview = run["preview"]
                preview["evidence"]["steps"][0]["validation"] = [{"valid": False}]
                runtime.store.update(run["run_id"], preview=preview)
            await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            done = await wait_state(client, run["run_id"], {"completed"})
            assert graph.calls == 2
            assert done["evidence"]["preview_reuse"]["reused_step_ids"] == []
            assert done["evidence"]["preview_reuse"]["retrieved_step_ids"] == ["s1"]
            assert "PRIVATE_NEW_ACCESS_SENTINEL" not in json.dumps(done)
    asyncio.run(scenario())


def test_failed_preview_retries_once_and_invalidates_dependent_reuse(tmp_path):
    async def scenario():
        graph = PreviewGraph(outcomes={"s1": [ConnectionError("PRIVATE_FAILURE"), "complete"]})
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=multi_plan(dependent=True))) as (client, runtime, gateway, *_):
            created = await new_plan(client)
            ready = (await client.get(created["plan_url"])).json()
            assert ready["preview"]["status"] == "partial"
            assert ready["preview"]["evidence"]["steps"][0]["status"] == "failed"
            assert "PRIVATE_FAILURE" not in json.dumps(ready)
            assert runtime.health.inference["claude"]["state"] == "healthy"
            await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            done = await wait_state(client, created["run_id"], {"completed"})
            assert graph.step_calls == {"s1": 2, "s2": 2, "s3": 1}
            assert graph.previous[-1]["s1"]["status"] == "complete"
            assert done["evidence"]["preview_reuse"]["reused_step_ids"] == ["s3"]
            assert done["evidence"]["preview_reuse"]["unreused_reasons"]["s2"] == "dependency_changed"
            await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            assert graph.calls == 5 and gateway.syntheses == 1
    asyncio.run(scenario())


def test_preview_timeout_preserves_successes_and_claude_health(tmp_path):
    async def scenario():
        plan = multi_plan()
        plan["steps"] = plan["steps"][:2]
        graph = PreviewGraph(block_step="s2")
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=plan), preview_timeout=0.03) as (client, runtime, gateway, *_):
            created = await new_plan(client)
            ready = (await client.get(created["plan_url"])).json()
            assert ready["preview"]["status"] == "partial"
            assert [step["status"] for step in ready["preview"]["evidence"]["steps"]] == ["complete", "failed"]
            assert ready["preview"]["error"]["category"] == "timeout"
            assert ready["preview"]["evidence"]["nodes"]
            assert gateway.syntheses == 0
            assert runtime.health.inference["claude"]["state"] == "healthy"
            graph.block_step = None
            await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            done = await wait_state(client, created["run_id"], {"completed"})
            assert graph.step_calls == {"s1": 1, "s2": 2}
            assert done["evidence"]["preview_reuse"]["reused_step_ids"] == ["s1"]
    asyncio.run(scenario())


@pytest.mark.parametrize("supersede", [False, True])
def test_cancel_or_revision_stops_preflight_even_when_upstream_suppresses_cancel(tmp_path, supersede):
    async def scenario():
        plan = multi_plan()
        plan["steps"] = plan["steps"][:2]
        graph = PreviewGraph(block_step="s2", suppress_cancel=True)
        async with service(tmp_path, graph=graph, gateway=BiologicalGateway(plan=plan)) as (client, runtime, gateway, *_):
            created = (await client.post("/v2/plans", json={"question": "Which cell types express INS?"})).json()
            await asyncio.wait_for(graph.blocked.wait(), 1)
            before = runtime.store.get(created["run_id"])["preview"]
            assert before["evidence"]["nodes"] and before["pending_step_ids"] == ["s2"]
            if supersede:
                graph.block_step = None
                response = await client.post(f'/v2/plans/{created["plan_id"]}/revise', json={"question": "Which cell types express GCG?", "include_context": False})
                assert response.status_code == 202
                replacement = response.json()
                await wait_state(client, replacement["run_id"], {"awaiting_confirmation"})
            else:
                response = await client.post(f'/v2/runs/{created["run_id"]}/cancel')
                assert response.status_code == 200
            await asyncio.sleep(0.02)
            old = runtime.store.get(created["run_id"])
            assert old["status"] == ("superseded" if supersede else "cancelled")
            assert old["preview"] == before
            assert gateway.syntheses == 0
            events = runtime.store.events_after(created["run_id"], 0)
            assert not any(event["type"] == "plan_ready" for event in events)
            assert events[-1]["type"] == "terminal"
            assert (await client.post(f'/v2/plans/{created["plan_id"]}/confirm')).status_code == 409
    asyncio.run(scenario())


def test_revision_replans_verbatim_same_session_and_invalidates_old_preview(tmp_path):
    async def scenario():
        graph = PreviewGraph(add_context=True)
        async with service(tmp_path, graph=graph, gateway=BiologicalGateway()) as (client, runtime, gateway, *_):
            created = await new_plan(client)
            old_preview = runtime.store.get(created["run_id"])["preview"]
            question = "Which cell types express GCG in T1D?"
            response = await client.post(f'/v2/plans/{created["plan_id"]}/revise', json={"question": question, "include_context": False})
            assert response.status_code == 202
            revised = response.json()
            ready = await wait_state(client, revised["run_id"], {"awaiting_confirmation"})
            assert revised["session_id"] == created["session_id"]
            assert revised["run_id"] != created["run_id"]
            assert ready["question"] == ready["plan"]["interpreted_question"] == question
            assert ready["include_context"] is ready["plan"]["include_context"] is False
            assert len(ready["plan"]["steps"]) == 1
            assert {node["id"] for node in ready["preview"]["evidence"]["nodes"]} == {"GCG"}
            old = runtime.store.get(created["run_id"])
            assert old["preview"] == old_preview
            assert old["replacement_run_id"] == revised["run_id"]
            assert (await client.post(f'/v2/plans/{created["plan_id"]}/confirm')).status_code == 409
            assert (await client.post(f'/v2/plans/{created["plan_id"]}/revise', json={"question": "Again"})).status_code == 409
            assert gateway.plans == len(graph.prepares) == 2
            assert gateway.syntheses == 0
            before = graph.calls
            await client.post(f'/v2/plans/{revised["plan_id"]}/confirm')
            await wait_state(client, revised["run_id"], {"completed"})
            assert graph.calls == before
    asyncio.run(scenario())


def test_store_revision_and_confirmation_are_atomic_competitors(tmp_path):
    store = Store(tmp_path)
    old = store.create("Which cell types express INS?")
    store.update(old["run_id"], status="awaiting_confirmation", stage="awaiting_confirmation", plan=PLAN)
    barrier = threading.Barrier(2)

    def confirm():
        barrier.wait()
        return store.confirm(old["run_id"])

    def revise():
        barrier.wait()
        try:
            return store.revise(old["plan_id"], "Which cell types express GCG?")
        except ValueError:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(confirm), pool.submit(revise)
            confirmed, revised = a.result(), b.result()
        assert bool(confirmed) != bool(revised)
        current = store.get(old["run_id"])
        assert current["status"] == ("queued" if confirmed else "superseded")
        assert store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == (1 if confirmed else 2)
    finally:
        store.close()


def test_preview_persists_across_restart_and_migration_is_additive(tmp_path):
    async def scenario():
        graph = PreviewGraph()
        async with service(tmp_path, graph=graph) as (client, runtime, *_):
            created = await new_plan(client)
            preview = runtime.store.get(created["run_id"])["preview"]
        async with service(tmp_path, graph=PreviewGraph()) as (client, runtime, gateway, graph, *_):
            assert runtime.store.get(created["run_id"])["preview"] == preview
            await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            await wait_state(client, created["run_id"], {"completed"})
            assert graph.calls == gateway.plans == 0
            assert gateway.syntheses == 1
    asyncio.run(scenario())
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert {"preview", "preview_cache", "include_context", "replacement_run_id"} <= {row[1] for row in db.execute("PRAGMA table_info(runs)")}


def test_prepare_plan_error_is_visible_without_claude_failure(tmp_path):
    async def scenario():
        graph = PreviewGraph()
        graph.preparation_error = ConnectionError("PRIVATE_RESOLUTION_ERROR")
        async with service(tmp_path, graph=graph) as (client, runtime, gateway, *_):
            created = await new_plan(client)
            ready = (await client.get(created["plan_url"])).json()
            assert ready["preview"]["status"] == "failed"
            assert ready["preview"]["evidence"]["steps"][0]["status"] == "failed"
            assert graph.calls == gateway.syntheses == 0
            assert runtime.health.inference["claude"]["state"] == "healthy"
            assert runtime.health.inference["neo4j"]["state"] == "degraded"
            assert "PRIVATE_RESOLUTION_ERROR" not in json.dumps(ready)
    asyncio.run(scenario())


def test_retried_step_cannot_push_independent_cached_evidence_over_run_budget(tmp_path):
    class BudgetedGraph(PreviewGraph):
        async def execute(self, step, previous, emit):
            result = await super().execute(step, previous, emit)
            size = 0 if result["status"] == "failed" else 8 if step["id"] == "s1" else 5
            if size + sum(item.get("materialized_bytes", 0) for item in previous.values()) > 10:
                result.update(status="partial", truncated=True, nodes=[], edges=[], rows=[])
                size = 0
            result["materialized_bytes"] = size
            return result

    async def scenario():
        plan = multi_plan()
        plan["steps"] = plan["steps"][:2]
        graph = BudgetedGraph(outcomes={"s1": ["failed", "complete"]})
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=plan), max_bytes=10) as (client, runtime, *_):
            created = await new_plan(client)
            await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            run = await wait_state(client, created["run_id"], {"partial"})
            assert graph.step_calls == {"s1": 2, "s2": 2}
            assert run["evidence"]["preview_reuse"]["unreused_reasons"]["s2"] == "materialization_budget_changed"
            assert sum(step["materialized_bytes"] for step in run["evidence"]["steps"]) <= 10
            assert run["evidence"]["steps"][1]["status"] == "partial"
    asyncio.run(scenario())


def test_pre_preview_database_migrates_without_changing_old_records(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, plan_id TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL,
                question TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_epoch REAL NOT NULL,
                plan TEXT, graph_answer TEXT, evidence TEXT, literature TEXT, error TEXT
            );
            INSERT INTO sessions VALUES ('old-session','old-time');
            INSERT INTO runs (run_id,plan_id,session_id,question,status,stage,created_at,updated_at,created_epoch,graph_answer)
                VALUES ('old-run','old-plan','old-session','Original question','completed','completed','old-time','old-time',1,'Original answer');
        """)
    store = Store(tmp_path)
    try:
        run = store.get("old-run")
        assert run["question"] == "Original question"
        assert run["graph_answer"] == "Original answer"
        assert run["status"] == "completed"
        assert run["preview"] is run["preview_cache"] is run["replacement_run_id"] is None
        assert run["include_context"] is True
    finally:
        store.close()


def test_legacy_unconfirmed_plan_requires_explicit_revision_before_validation(tmp_path):
    async def scenario():
        async with service(tmp_path, graph=PreviewGraph()) as (client, runtime, gateway, graph, literature):
            legacy = runtime.store.create("Which cell types express INS?")
            runtime.store.update(legacy["run_id"], status="awaiting_confirmation", stage="awaiting_confirmation", plan=PLAN)
            initial_events = runtime.store.events_after(legacy["run_id"], 0)
            for _ in range(2):
                restored = await client.get(f'/v2/runs/{legacy["run_id"]}')
                assert restored.json()["preview"] is None
            blocked = await client.post(f'/v2/plans/{legacy["plan_id"]}/confirm')
            assert blocked.status_code == 409
            assert blocked.json()["detail"] == "Revise this saved plan to validate initial evidence."
            assert gateway.plans == gateway.syntheses == graph.calls == literature.calls == 0
            assert runtime.store.events_after(legacy["run_id"], 0) == initial_events
            assert runtime.store.get(legacy["run_id"])["status"] == "awaiting_confirmation"
            revised = await client.post(f'/v2/plans/{legacy["plan_id"]}/revise', json={"question": legacy["question"]})
            assert revised.status_code == 202
            ready = await wait_state(client, revised.json()["run_id"], {"awaiting_confirmation"})
            assert ready["preview"]["evidence"]["nodes"]
            assert gateway.plans == graph.calls == 1
            assert gateway.syntheses == literature.calls == 0
            assert (await client.post(f'/v2/plans/{ready["plan_id"]}/confirm')).status_code == 202
            await wait_state(client, ready["run_id"], {"completed"})
            assert graph.calls == gateway.syntheses == 1
    asyncio.run(scenario())
