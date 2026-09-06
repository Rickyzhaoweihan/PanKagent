"""Bounded, sequential live checks against an already-running vNext service.

Run on the service host with ``python -m pankagent_vnext.smoke``. This makes
three real plan/answer requests under the service's existing budget ledger.
It never starts or restarts a service and never prints answers or graph data.
The private JSON report distinguishes functional checks from latency targets;
exit status is nonzero for failed functional checks, not slow valid answers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx


QUESTIONS = (
    ("ins_cell_types", "Which cell types express INS?"),
    ("gcg_cell_types", "Which cell types express GCG?"),
    ("sst_cell_types", "Which cell types express SST?"),
)
TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled", "interrupted"}


class SmokeError(Exception):
    """A fixed, non-sensitive failure category suitable for aggregate reports."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


def category_for(exc: Exception) -> str:
    if isinstance(exc, SmokeError):
        return exc.category
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "deadline_exceeded"
    if isinstance(exc, httpx.ConnectError):
        return "connection_failed"
    if isinstance(exc, httpx.HTTPError):
        return "http_transport_error"
    return "invalid_response"


async def checked_json(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict:
    response = await client.request(method, url, **kwargs)
    if response.status_code >= 400:
        raise SmokeError(f"http_{response.status_code}")
    value = response.json()
    if not isinstance(value, dict):
        raise SmokeError("invalid_response")
    return value


async def sse_frames(response: httpx.Response):
    """Read standard SSE data frames without retaining raw frames or evidence."""
    data = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                frame = json.loads("\n".join(data))
                if not isinstance(frame, dict):
                    raise SmokeError("invalid_sse_frame")
                yield frame
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        frame = json.loads("\n".join(data))
        if not isinstance(frame, dict):
            raise SmokeError("invalid_sse_frame")
        yield frame


class SmokeRun:
    def __init__(self, client: httpx.AsyncClient, question_id: str, question: str):
        self.client, self.question_id, self.question = client, question_id, question
        self.started = time.monotonic()
        self.confirm_started = None
        self.run_id = None
        self.plan_id = None
        self.plan = None
        self.plan_ready = asyncio.Event()
        self.terminal = asyncio.Event()
        self.sequences: set[int] = set()
        self.stream_task = None
        self.result = {
            "question_id": question_id,
            "status": "not_started",
            "plan_ready_seconds": None,
            "first_progress_seconds": None,
            "post_confirm_graph_seconds": None,
            "total_seconds": None,
            "idempotent_confirmation": False,
            "sse": {"unique_events": 0, "duplicate_events": 0, "reconnections": 0},
            "citations": {"reference_ids": [], "unknown_reference_count": None, "application_validation": None},
            "functional_checks_passed": False,
        }

    def accept_frame(self, frame: dict):
        sequence = frame.get("sequence")
        if frame.get("version") != 2 or frame.get("run_id") != self.run_id or not isinstance(sequence, int) or sequence <= 0:
            raise SmokeError("invalid_sse_envelope")
        if sequence in self.sequences:
            self.result["sse"]["duplicate_events"] += 1
            return
        self.sequences.add(sequence)
        event_type, payload = frame.get("type"), frame.get("payload", {})
        if not isinstance(payload, dict):
            raise SmokeError("invalid_sse_payload")
        now = time.monotonic()
        if event_type == "progress" and self.result["first_progress_seconds"] is None:
            self.result["first_progress_seconds"] = now - self.started
        if event_type == "plan_ready":
            self.plan = payload.get("plan")
            if not isinstance(self.plan, dict):
                raise SmokeError("invalid_plan")
            self.result["plan_ready_seconds"] = now - self.started
            self.plan_ready.set()
        elif event_type == "graph_answer" and payload.get("delta") is False:
            if self.confirm_started is None:
                raise SmokeError("answer_before_confirmation")
            self.result["post_confirm_graph_seconds"] = now - self.confirm_started
            evidence = payload.get("evidence", {})
            steps = evidence.get("steps", [])
            known = {step.get("evidence_id", f"G{index + 1}") for index, step in enumerate(steps)}
            references = set(re.findall(r"\[G(\d+)\]", payload.get("answer", "")))
            references = {f"G{value}" for value in references}
            self.result["citations"] = {
                "reference_ids": sorted(references),
                "unknown_reference_count": len(references - known),
                "application_validation": evidence.get("answer_reference_validation", {}).get("valid"),
            }
            allowed = {"complete", "partial", "empty", "failed"}
            self.result["graph_step_statuses"] = [step.get("status") if step.get("status") in allowed else "unknown" for step in steps]
            self.result["graph_completeness"] = evidence.get("completeness") if evidence.get("completeness") in allowed else "unknown"
        elif event_type == "terminal":
            status = payload.get("status")
            self.result["status"] = status if status in TERMINAL_STATUSES else "unknown"
            self.terminal.set()

    async def consume(self):
        # One reconnect is enough to check replay while retaining a strict bound.
        for attempt in range(2):
            headers = {"Last-Event-ID": str(max(self.sequences))} if self.sequences else {}
            try:
                async with self.client.stream("GET", f"/v2/runs/{self.run_id}/events", headers=headers, timeout=None) as response:
                    if response.status_code >= 400:
                        raise SmokeError(f"http_{response.status_code}")
                    async for frame in sse_frames(response):
                        self.accept_frame(frame)
                        if self.terminal.is_set():
                            return
            except httpx.TransportError:
                if attempt:
                    raise
            if self.terminal.is_set():
                return
            if attempt == 0:
                self.result["sse"]["reconnections"] += 1
            else:
                raise SmokeError("sse_closed_before_terminal")

    async def wait_event(self, event: asyncio.Event):
        waiter = asyncio.create_task(event.wait())
        try:
            await asyncio.wait({waiter, self.stream_task}, return_when=asyncio.FIRST_COMPLETED)
            if self.stream_task.done():
                await self.stream_task
                if not event.is_set():
                    raise SmokeError("terminal_before_expected_stage")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def cancel(self):
        if self.run_id and not self.terminal.is_set():
            try:
                # Cancellation is best effort; it never restarts the shared service.
                await checked_json(self.client, "POST", f"/v2/runs/{self.run_id}/cancel", timeout=5)
                self.result["cancellation_requested"] = True
            except Exception:
                self.result["cancellation_requested"] = False

    async def replay_check(self, snapshot: dict):
        sequences = sorted(self.sequences)
        cursor = sequences[-2] if len(sequences) > 1 else 0
        replayed = []
        async with self.client.stream("GET", f"/v2/runs/{self.run_id}/events", headers={"Last-Event-ID": str(cursor)}, timeout=10) as response:
            if response.status_code >= 400:
                raise SmokeError(f"http_{response.status_code}")
            async for frame in sse_frames(response):
                if frame.get("version") != 2 or frame.get("run_id") != self.run_id:
                    raise SmokeError("invalid_replay_envelope")
                replayed.append(frame.get("sequence"))
        expected = [sequence for sequence in sequences if sequence > cursor]
        after = await checked_json(self.client, "GET", f"/v2/runs/{self.run_id}")
        self.result["sse"].update({
            "replay_after_sequence": cursor,
            "replay_expected_count": len(expected),
            "replay_received_count": len(replayed),
            "replay_sequences_match": replayed == expected,
            "replay_run_unchanged": snapshot.get("updated_at") == after.get("updated_at") and snapshot.get("status") == after.get("status"),
        })

    async def execute(self):
        created = await checked_json(self.client, "POST", "/v2/plans", json={"question": self.question})
        self.run_id, self.plan_id = created.get("run_id"), created.get("plan_id")
        if not self.run_id or not self.plan_id:
            raise SmokeError("invalid_plan_creation")
        # Opaque IDs permit correlating private server traces without saving content.
        self.result.update(run_id=self.run_id, plan_id=self.plan_id, status="planning")
        self.stream_task = asyncio.create_task(self.consume())
        try:
            await self.wait_event(self.plan_ready)
            if self.plan.get("clarification"):
                raise SmokeError("unexpected_clarification")
            if self.plan.get("literature"):
                raise SmokeError("unexpected_literature_plan")
            steps = self.plan.get("steps", [])
            if not isinstance(steps, list) or not 1 <= len(steps) <= 3:
                raise SmokeError("invalid_step_count")
            self.result["planned_step_count"] = len(steps)
            self.confirm_started = time.monotonic()
            first = await checked_json(self.client, "POST", f"/v2/plans/{self.plan_id}/confirm")
            second = await checked_json(self.client, "POST", f"/v2/plans/{self.plan_id}/confirm")
            self.result["idempotent_confirmation"] = first.get("run_id") == second.get("run_id") == self.run_id
            await self.wait_event(self.terminal)
            snapshot = await checked_json(self.client, "GET", f"/v2/runs/{self.run_id}")
            self.result["snapshot_status_matches"] = snapshot.get("status") == self.result["status"]
            await self.replay_check(snapshot)
            self.result["functional_checks_passed"] = all((
                self.result["status"] == "completed",
                self.result["post_confirm_graph_seconds"] is not None,
                self.result["idempotent_confirmation"],
                self.result["snapshot_status_matches"],
                self.result["citations"]["unknown_reference_count"] == 0,
                self.result["citations"]["application_validation"] is True,
                self.result["sse"]["replay_sequences_match"],
                self.result["sse"]["replay_run_unchanged"],
            ))
        finally:
            if self.stream_task and not self.stream_task.done():
                self.stream_task.cancel()
            if self.stream_task:
                await asyncio.gather(self.stream_task, return_exceptions=True)


def timing_summary(values: list[float]) -> dict:
    if not values:
        return {"samples": 0, "median": None, "p95": None}
    ordered = sorted(values)
    return {"samples": len(values), "median": round(statistics.median(values), 3), "p95": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3)}


async def evaluate(base_url: str, run_timeout: float) -> dict:
    results = []
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False) as client:
        for question_id, question in QUESTIONS:
            run = SmokeRun(client, question_id, question)
            try:
                await asyncio.wait_for(run.execute(), timeout=run_timeout)
            except Exception as exc:
                run.result["failure_category"] = category_for(exc)
                if run.result["status"] not in TERMINAL_STATUSES:
                    run.result["status"] = "smoke_failed"
                await run.cancel()
            run.result["total_seconds"] = time.monotonic() - run.started
            run.result["sse"]["unique_events"] = len(run.sequences)
            for field in ("plan_ready_seconds", "first_progress_seconds", "post_confirm_graph_seconds", "total_seconds"):
                if run.result[field] is not None:
                    run.result[field] = round(run.result[field], 3)
            results.append(run.result)
    timings = {field: timing_summary([result[field] for result in results if result[field] is not None]) for field in ("plan_ready_seconds", "first_progress_seconds", "post_confirm_graph_seconds", "total_seconds")}
    targets = {"plan_ready_seconds": 10, "first_progress_seconds": 1, "post_confirm_graph_seconds": 10}
    return {
        "version": 1,
        "evaluation_type": "bounded_live_runtime_smoke",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "question_count": len(QUESTIONS),
        "per_run_deadline_seconds": run_timeout,
        "runs": results,
        "timings": timings,
        "p95_method": "nearest_rank; for three samples p95 is the largest observation",
        "median_targets_seconds": targets,
        "median_targets_met": {field: summary["samples"] == len(QUESTIONS) and summary["median"] is not None and summary["median"] <= targets[field] for field, summary in timings.items() if field in targets},
        "functional_checks_passed": all(result["functional_checks_passed"] for result in results),
        "scientific_accuracy_evaluated": False,
        "content_policy": "Aggregate timings/statuses/citation IDs only; no answers or graph evidence retained.",
    }


def write_private_report(report: dict, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8794", help="Existing loopback vNext service URL on this host.")
    parser.add_argument("--run-timeout", type=float, default=120, help="Overall seconds per question (maximum 180); cancellation gets at most five additional seconds.")
    parser.add_argument("--output", type=Path, help="New private aggregate JSON path; existing files are never overwritten.")
    args = parser.parse_args(argv)
    parsed = urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        parser.error("--base-url must be a loopback HTTP(S) URL without credentials or a path.")
    if not 1 <= args.run_timeout <= 180:
        parser.error("--run-timeout must be between 1 and 180 seconds.")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output or Path("var/vnext/evaluations") / f"smoke-{timestamp}.json"
    if output.exists():
        parser.error("--output already exists; choose a new report path.")
    report = asyncio.run(evaluate(args.base_url.rstrip("/"), args.run_timeout))
    write_private_report(report, output)
    print(json.dumps({
        "report": str(output.resolve()),
        "completed_runs": sum(run["status"] == "completed" for run in report["runs"]),
        "functional_checks_passed": report["functional_checks_passed"],
        "plan_median_seconds": report["timings"]["plan_ready_seconds"]["median"],
        "graph_median_seconds": report["timings"]["post_confirm_graph_seconds"]["median"],
        "median_targets_met": report["median_targets_met"],
    }))
    return 0 if report["functional_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
