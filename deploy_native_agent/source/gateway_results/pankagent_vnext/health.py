"""Cached component observations; HTTP health reads never invoke a model."""

from __future__ import annotations

import asyncio
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any


STATES = {"healthy", "degraded", "unavailable", "unknown"}
ERRORS = {
    "authentication", "authorization", "rate_limited", "billing", "budget_exhausted",
    "timeout", "connection", "invalid_response", "query_validation", "graph_identity",
    "dependency_unavailable", "internal_error", "cancelled", "service_restarted",
    "not_configured", "service_ownership",
    "queue_full",
}
HARD_INFERENCE_ERRORS = {"authentication", "authorization", "billing", "budget_exhausted", "not_configured", "graph_identity"}
SAFE_DETAIL_KEYS = {
    "owner_state", "owner_epoch", "persistence_queue_depth", "audit_dropped", "model", "prompt_version", "replicas", "healthy_replicas", "total_replicas",
    "reachable", "authenticated", "auth_ok", "model_access", "generation_health",
    "graph_version", "graph_identity", "identity_verified", "read_access", "read_only",
    "database", "corpus_version", "source_policy", "service_version", "upstream_state",
    "storage", "durable", "remaining_usd", "spent_usd", "reserved_usd", "limit_usd",
    "budget_usd", "queue_depth", "active_queries", "capacity", "canaries_enabled",
    "replica_count", "prompt", "adapter_version", "provider_indicator", "api_component_status", "provider_component_id", "required",
    "database_role_enforced", "application_guard_and_read_transactions", "inference_verified",
    "database_auth_enabled", "read_only_enforcement", "identity_strength", "backends_up",
    "recent_generation_success", "recent_query_success", "last_inference_success", "result_cache_enabled",
}


def error_category(exc: BaseException) -> str:
    explicit = getattr(exc, "category", None)
    if explicit in ERRORS:
        return explicit
    name = type(exc).__name__.lower()
    for needle, category in (
        ("budget", "budget_exhausted"), ("timeout", "timeout"),
        ("authentication", "authentication"), ("permission", "authorization"),
        ("ratelimit", "rate_limited"), ("billing", "billing"),
        ("connect", "connection"), ("validation", "query_validation"),
        ("identity", "graph_identity"), ("protocol", "invalid_response"),
    ):
        if needle in name:
            return category
    status = getattr(exc, "status_code", None)
    return {401: "authentication", 403: "authorization", 402: "billing", 429: "rate_limited"}.get(status, "dependency_unavailable")


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch is not None else None


def normalize_error(value: Any) -> str | None:
    if isinstance(value, str) and value in ERRORS:
        return value
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    for needle, category in (("authentication", "authentication"), ("permission", "authorization"), ("ratelimit", "rate_limited"), ("timeout", "timeout"), ("connect", "connection"), ("identity", "graph_identity"), ("not_configured", "not_configured")):
        if needle in lowered:
            return category
    return "dependency_unavailable" if value else None


class Metrics:
    def __init__(self):
        self.counts: Counter = Counter()
        self.durations: dict[str, list[float]] = defaultdict(list)
        self.duration_counts: Counter = Counter()
        self.duration_sums: dict[str, float] = defaultdict(float)

    def count(self, name: str, count: int = 1) -> None:
        self.counts[name] += count

    def observe(self, stage: str, seconds: float) -> None:
        self.duration_counts[stage] += 1
        self.duration_sums[stage] += seconds
        values = self.durations[stage]
        values.append(seconds)
        if len(values) > 10000:
            del values[:-10000]

    def render(self, runtime: dict, budget: dict) -> str:
        lines = []
        for name, count in sorted(self.counts.items()):
            lines.append(f'pankagent_events_total{{kind="{name}"}} {count}')
        for stage, values in sorted(self.durations.items()):
            ordered = sorted(values)
            lines.extend([
                f'pankagent_stage_seconds_count{{stage="{stage}"}} {self.duration_counts[stage]}',
                f'pankagent_stage_seconds_sum{{stage="{stage}"}} {self.duration_sums[stage]:.6f}',
                f'pankagent_stage_seconds{{stage="{stage}",quantile="0.5"}} {ordered[(len(ordered)-1)//2]:.6f}',
                f'pankagent_stage_seconds{{stage="{stage}",quantile="0.95"}} {ordered[max(0, int(len(ordered)*0.95+0.999)-1)]:.6f}',
            ])
        for field in ("queue_depth", "active_queries", "capacity"):
            lines.append(f"pankagent_{field} {runtime.get(field, 0)}")
        lines.extend(["pankagent_result_cache_enabled 0", "pankagent_result_cache_hits_total 0"])
        for field in ("remaining_usd", "spent_usd", "reserved_usd", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            if isinstance(budget.get(field), (int, float)):
                lines.append(f"pankagent_{field} {budget[field]}")
        return "\n".join(lines) + "\n"


class HealthMonitor:
    def __init__(self, settings, gateway, graph, literature, store, runtime_snapshot, io=None):
        self.settings = settings
        self.gateway, self.graph, self.literature, self.store = gateway, graph, literature, store
        self.runtime_snapshot = runtime_snapshot
        self.io = io
        self._budget = {}
        self._storage = {"state": "unknown"}
        self.observations = {name: self._unknown() for name in ("cypher", "neo4j", "claude", "claude_provider", "hirn", "runtime")}
        self.inference: dict[str, dict] = {}
        self.tasks: list[asyncio.Task] = []

    @staticmethod
    def _unknown() -> dict:
        return {"state": "unknown", "checked_epoch": None, "latency_ms": None, "last_success_epoch": None, "error_category": None, "details": {}}

    def record_inference(self, name: str, success: bool, category: str | None = None) -> None:
        previous = self.inference.get(name, {})
        now = time.time()
        self.inference[name] = {
            "state": "healthy" if success else "unavailable" if category in HARD_INFERENCE_ERRORS else "degraded", "checked_at": _iso(now),
            "last_success": _iso(now) if success else previous.get("last_success"),
            "error_category": category if category in ERRORS else None,
        }

    def _record(self, name: str, result: dict, seconds: float) -> None:
        old = self.observations[name]
        state = result.get("state")
        if state not in STATES:
            state = "healthy" if result.get("ok") is True else "unavailable" if result.get("ok") is False else "unknown"
        detail_source = {**result, **(result.get("details") or {})}
        details = {key: value for key, value in detail_source.items() if key in SAFE_DETAIL_KEYS and isinstance(value, (str, int, float, bool, list, dict, type(None)))}
        now = time.time()
        self.observations[name] = {
            "state": state, "checked_epoch": now, "latency_ms": round(seconds * 1000, 1),
            "last_success_epoch": now if state == "healthy" else old["last_success_epoch"],
            "error_category": normalize_error(result.get("error_category")),
            "details": details,
        }

    async def _probe(self, name: str, call) -> None:
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(call(), timeout=8)
            if not isinstance(result, dict):
                result = {"state": "unavailable", "error_category": "invalid_response"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = {"state": "unavailable", "error_category": error_category(exc)}
        if name == "graph":
            for component in ("cypher", "neo4j"):
                observation = result.get(component, result.get("components", {}).get(component, result))
                self._record(component, observation if isinstance(observation, dict) else {"state": "unknown"}, time.monotonic() - started)
        else:
            self._record(name, result, time.monotonic() - started)

    def budget_snapshot(self) -> dict:
        return dict(self._budget)

    def _read_persistence(self):
        storage = self.store.probe()
        budget = self.gateway.budget.snapshot()
        if "remaining_usd" not in budget and "remaining" in budget:
            budget = {**budget, "remaining_usd": budget["remaining"]}
        return storage, budget

    async def refresh_persistence(self):
        started = time.monotonic()
        try:
            if self.io is not None:
                self._storage, self._budget = await self.io.call(self._read_persistence)
            else:
                self._storage, self._budget = await asyncio.to_thread(self._read_persistence)
        except Exception:
            self._storage, self._budget = {"state": "unavailable", "error_category": "internal_error"}, {}
        self._record("runtime", self._runtime_value(), time.monotonic() - started)

    def _runtime_value(self):
        result = {**self._storage, **self.runtime_snapshot()}
        result.update({key: value for key, value in self._budget.items() if key in SAFE_DETAIL_KEYS})
        result.update(canaries_enabled=False, result_cache_enabled=False)
        if result.get("owner_state") in {"lost", "released"}:
            result.update(state="unavailable", error_category="service_ownership")
        elif not self._budget:
            result.update(state="unknown", error_category="internal_error")
        elif self._budget.get("remaining_usd", 0) <= 0:
            result.update(state="unavailable", error_category="budget_exhausted")
        elif result.get("queue_depth", 0) >= getattr(self.settings, "max_queue", 8):
            result.update(state="unavailable", error_category="queue_full")
        return result

    def refresh_runtime(self):
        # Live queue/lease state is cheap; preserve the time of the actual disk
        # observation. Reading health cannot make stale storage look fresh.
        value = self._runtime_value()
        old = self.observations["runtime"]
        self._record("runtime", value, (old.get("latency_ms") or 0) / 1000)
        self.observations["runtime"]["checked_epoch"] = old["checked_epoch"]
        self.observations["runtime"]["last_success_epoch"] = old["last_success_epoch"]

    async def _provider(self) -> dict:
        url = getattr(self.settings, "provider_status_url", "")
        if not url:
            return {"state": "unknown"}
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        # Pin the official API component, never infer API health from other products.
        components = payload.get("components", []) if isinstance(payload, dict) else []
        matches = [item for item in components if isinstance(item, dict)
                   and item.get("id") == "k8w3r06qmzrp"] if isinstance(components, list) else []
        status = matches[0].get("status") if len(matches) == 1 else None
        mapping = {"operational": "healthy", "degraded_performance": "degraded",
                   "partial_outage": "degraded", "major_outage": "unavailable",
                   "under_maintenance": "degraded"}
        if not isinstance(status, str) or status not in mapping:
            return {"state": "unknown"}
        return {"state": mapping[status], "api_component_status": status,
                "provider_component_id": "k8w3r06qmzrp"}

    async def refresh(self, online: bool = False) -> None:
        await self.refresh_persistence()
        graph_probes = [self._probe("neo4j", self.graph.probe), self._probe("cypher", self.graph.probe_cypher)] if hasattr(self.graph, "probe_cypher") else [self._probe("graph", self.graph.probe)]
        probes = [*graph_probes, self._probe("hirn", self.literature.probe)]
        if online:
            probes.extend([self._probe("claude", self.gateway.probe), self._probe("claude_provider", self._provider)])
        await asyncio.gather(*probes)

    async def _local_loop(self):
        while True:
            await self.refresh()
            await asyncio.sleep(self.settings.health_interval)

    async def _online_loop(self):
        while True:
            await asyncio.gather(self._probe("claude", self.gateway.probe), self._probe("claude_provider", self._provider))
            await asyncio.sleep(self.settings.claude_health_interval)

    def start(self):
        self.tasks = [asyncio.create_task(self._local_loop()), asyncio.create_task(self._online_loop())]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def snapshot(self) -> dict:
        self.refresh_runtime()
        now = time.time()
        components = {}
        for name, observation in self.observations.items():
            age = now - observation["checked_epoch"] if observation["checked_epoch"] is not None else None
            interval = self.settings.claude_health_interval if name in {"claude", "claude_provider"} else self.settings.health_interval
            stale = age is None or age > max(10, interval * 3)
            state = "unknown" if stale else observation["state"]
            recent = self.inference.get(name)
            category = observation["error_category"]
            if recent:
                recent = dict(recent)
                inference_age = now - datetime.fromisoformat(recent["checked_at"]).timestamp()
                recent["age_seconds"] = round(inference_age, 1)
                recent["stale"] = inference_age > max(10, interval * 3)
                # Missing recent activity must stay unknown, and access checks
                # cannot erase a known billing/authentication inference failure.
                if recent["stale"] and recent["state"] != "unavailable":
                    recent["state"] = "unknown"
                if recent["state"] == "unavailable" and state in {"healthy", "degraded"}:
                    state, category = "unavailable", recent["error_category"]
                elif recent["state"] == "degraded" and state == "healthy":
                    state, category = "degraded", recent["error_category"]
            components[name] = {
                "state": state, "checked_at": _iso(observation["checked_epoch"]),
                "age_seconds": round(age, 1) if age is not None else None,
                "stale": stale, "latency_ms": observation["latency_ms"],
                "last_success": _iso(observation["last_success_epoch"]),
                "error_category": category, "details": observation["details"],
                "recent_inference": recent or {"state": "unknown", "checked_at": None, "last_success": None, "error_category": None, "age_seconds": None, "stale": True},
            }
        required = ("cypher", "neo4j", "claude", "runtime")
        ready = all(components[name]["state"] in {"healthy", "degraded"} for name in required)
        state = "unavailable" if not ready else "degraded" if any(item["state"] != "healthy" for name, item in components.items() if name != "claude_provider") else "healthy"
        return {"version": 2, "state": state, "ready": ready, "checked_at": _iso(now), "components": components}
