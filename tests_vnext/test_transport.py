"""Hard limits cover JSON snapshots, errors, and each replayable SSE event."""

import asyncio
import json

from pankagent_vnext.transport import ANSWER_PREVIEW_BYTES, RESPONSE_BYTE_LIMIT, bounded_json_bytes, sse_event_bytes
from test_runtime import service


def test_normal_json_and_sse_event_are_unchanged():
    payload = {"run_id": "run-1", "status": "completed", "graph_answer": "Bounded answer [G1]."}
    raw = json.dumps(payload, ensure_ascii=False).encode()
    assert bounded_json_bytes(raw) == raw
    envelope = {"version": 2, "sequence": 3, "type": "graph_answer", "run_id": "run-1", "payload": {"delta": True, "text": "β cell"}}
    assert sse_event_bytes(envelope) == ("id: 3\nevent: graph_answer\ndata: " + json.dumps(envelope, ensure_ascii=False) + "\n\n").encode()


def test_oversized_plan_preview_preserves_review_identity_and_marks_omissions():
    envelope = {"version": 2, "run_id": "run-preview", "plan_id": "plan-preview", "sequence": 4,
                "type": "plan_ready", "status": "awaiting_confirmation", "payload": {
                    "plan": {"steps": [{"id": "primary", "title": "Check gene enrichment"}]},
                    "preview": {"status": "complete", "evidence": {"completeness": "complete",
                        "nodes": [{"id": "gene", "properties": {"raw": "x" * RESPONSE_BYTE_LIMIT}}]}}}}
    frame = sse_event_bytes(envelope)
    assert len(frame) <= RESPONSE_BYTE_LIMIT
    value = json.loads(next(line[6:] for line in frame.decode().splitlines() if line.startswith("data: ")))
    assert value["status"] == "awaiting_confirmation"
    assert value["payload"]["plan"]["steps"][0]["id"] == "primary"
    preview = value["payload"]["preview"]
    assert preview["delivery_status"] == "partial"
    assert preview["evidence"]["completeness"] == "partial"
    assert preview["evidence"]["nodes"] == []
    assert preview["evidence"]["omitted_counts"]["nodes"] == 1


def test_oversized_json_snapshot_is_explicitly_partial_with_identity_preserved(tmp_path):
    async def scenario():
        async with service(tmp_path) as (client, runtime, gateway, graph, literature):
            run = runtime.store.create("Which cell types express INS?")
            runtime.store.update(run["run_id"], status="completed", stage="completed", graph_answer="Supported graph answer [G1].",
                                 evidence={"graph_version": "test-release", "completeness": "complete", "truncated": False,
                                           "nodes": [{"id": "INS", "properties": {"description": "DO_NOT_DELIVER_THIS_RAW_BLOCK" * 400000}}], "edges": [],
                                           "steps": [{"step_id": "s1", "evidence_id": "G1", "status": "complete", "nodes": [{"id": "INS"}]}]})
            response = await client.get(f'/v2/runs/{run["run_id"]}')
            assert response.status_code == 200
            assert len(response.content) <= RESPONSE_BYTE_LIMIT
            assert int(response.headers["content-length"]) == len(response.content)
            body = response.json()
            assert {key: body[key] for key in ("run_id", "plan_id", "session_id", "status")} == {**{key: run[key] for key in ("run_id", "plan_id", "session_id")}, "status": "completed"}
            assert body["delivery_status"] == "partial" and body["truncated"] is True
            assert body["transport_truncation"]["reason"] == "response_size_limit"
            assert body["evidence"]["completeness"] == "partial"
            assert body["evidence"]["nodes"] == []
            assert body["evidence"]["omitted_counts"]["nodes"] == 1
            assert body["evidence"]["steps"][0]["step_id"] == "s1"
            assert "Delivery is partial" in body["graph_answer"]
            assert "DO_NOT_DELIVER_THIS_RAW_BLOCK" not in response.text
            # Transport truncation does not change stored evidence or call models.
            assert runtime.store.get(run["run_id"])["evidence"]["completeness"] == "complete"
            assert gateway.plans == graph.calls == literature.calls == 0
    asyncio.run(scenario())


def test_oversized_sse_delta_is_bounded_and_replays_identically(tmp_path):
    async def scenario():
        async with service(tmp_path) as (client, runtime, *_):
            run = runtime.store.create("Question")
            oversized = runtime.store.event(run["run_id"], "graph_answer", {"delta": True, "text": "β" * (RESPONSE_BYTE_LIMIT // 2 + 100)})
            runtime.store.update(run["run_id"], status="completed", stage="completed")
            terminal = runtime.store.event(run["run_id"], "terminal", {"status": "completed"})
            response = await client.get(f'/v2/runs/{run["run_id"]}/events')
            frames = [frame + "\n\n" for frame in response.text.split("\n\n") if frame]
            assert len(frames) == 2
            assert all(len(frame.encode()) <= RESPONSE_BYTE_LIMIT for frame in frames)
            event = json.loads(next(line[6:] for line in frames[0].splitlines() if line.startswith("data: ")))
            assert event["sequence"] == oversized["sequence"]
            assert event["type"] == "graph_answer" and event["run_id"] == run["run_id"]
            assert event["status"] == oversized["status"]
            assert event["payload"]["delta"] is True
            assert event["delivery_status"] == "partial"
            assert len(event["payload"]["text"].encode()) < ANSWER_PREVIEW_BYTES + 1024
            assert "Delivery is partial" in event["payload"]["text"]
            replay = await client.get(f'/v2/runs/{run["run_id"]}/events', headers={"Last-Event-ID": "0"})
            assert replay.content == response.content
            suffix = await client.get(f'/v2/runs/{run["run_id"]}/events', headers={"Last-Event-ID": str(oversized["sequence"])})
            assert f'id: {terminal["sequence"]}' in suffix.text
            assert "response_size_limit" not in suffix.text
    asyncio.run(scenario())


def test_json_validation_error_cannot_bypass_response_ceiling(tmp_path):
    async def scenario():
        async with service(tmp_path) as (client, runtime, gateway, *_):
            response = await client.post("/v2/plans", json={"question": "x" * (RESPONSE_BYTE_LIMIT + 1)})
            assert response.status_code == 422
            assert len(response.content) <= RESPONSE_BYTE_LIMIT
            assert response.json()["delivery_status"] == "partial"
            assert gateway.plans == 0
    asyncio.run(scenario())
