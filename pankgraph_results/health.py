"""Cached observations: monitoring never submits scientific work."""
import asyncio
from datetime import datetime, timezone
import time


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch else None


class ResultsHealth:
    def __init__(self, runtime):
        self.runtime = runtime
        self.observations = {}
        self.task = None
        self.counters = {}
        self.durations = {}
        self.budget = None

    def count(self, name, amount=1):
        self.counters[name] = self.counters.get(name, 0) + amount

    def duration(self, name, value):
        total, count = self.durations.get(name, (0, 0))
        self.durations[name] = total + value, count + 1

    def record(self, name, state, latency=0, error=None, details=None):
        now = time.time()
        old = self.observations.get(name, {})
        self.observations[name] = {"state": state, "checked_at": iso(now), "checked_epoch": now, "latency_ms": round(latency * 1000, 2), "last_success": iso(now) if state == "healthy" else old.get("last_success"), "error_category": error, "details": details or {}}

    async def probe(self):
        async def one(name, call):
            started = time.monotonic()
            try:
                value = await asyncio.wait_for(call(), 6)
                state = value.get("state")
                if state not in {"healthy", "degraded", "unavailable", "unknown"}:
                    state = "healthy" if value.get("ok", value.get("ready", False)) else "unavailable"
                self.record(name, state, time.monotonic() - started, value.get("error_category"),
                    {k: value[k] for k in ("identity_verified", "graph_version", "read_access") if k in value})
            except Exception as exc:
                self.record(name, "unavailable", time.monotonic() - started, type(exc).__name__)
        async def agent():
            r = await self.runtime.http.get(self.runtime.settings.agent_url + "/health/ready")
            r.raise_for_status()
            return r.json()
        await asyncio.gather(one("neo4j", self.runtime.query.probe), one("agent", agent))
        try:
            await asyncio.to_thread(self.runtime.store.probe)
            self.record("result_storage", "healthy")
        except Exception as exc:
            self.record("result_storage", "unavailable", error=type(exc).__name__)
        try:
            self.budget = await asyncio.to_thread(self.runtime.gateway.budget.snapshot)
            self.record("budget", "healthy" if self.budget.get("remaining_usd", 0) > 0 else "unavailable",
                error=None if self.budget.get("remaining_usd", 0) > 0 else "budget_exhausted")
        except Exception as exc:
            self.record("budget", "unavailable", error=type(exc).__name__)

    async def loop(self):
        while True:
            await self.probe()
            await asyncio.sleep(30)

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def snapshot(self):
        now = time.time()
        observations = {}
        for name, observation in self.observations.items():
            value = {k: v for k, v in observation.items() if k != "checked_epoch"}
            value["age_seconds"] = round(now - observation["checked_epoch"], 2)
            if value["age_seconds"] > 90:
                value.update(state="unknown", error_category="stale_observation")
            observations[name] = value
        for name in ("agent", "neo4j", "result_storage", "layout_worker", "query_adapter", "resources", "synthesis", "budget"):
            observations.setdefault(name, {"state": "unknown", "checked_at": None, "age_seconds": None, "last_success": None, "error_category": None})
        ready = all(observations[name]["state"] == "healthy" for name in ("neo4j", "result_storage"))
        optional_ok = all(observations.get(name, {}).get("state") == "healthy" for name in ("agent", "budget"))
        return {"version": 1, "service": "pankgraph-results", "ready": ready, "state": "healthy" if ready and optional_ok else "degraded" if ready else "unavailable", "components": observations, "layout": self.runtime.layout.snapshot(), "resources": self.runtime.resources.snapshot(), "queue": {"active": self.runtime.active, "depth": max(0, len(self.runtime.tasks) - self.runtime.active), "capacity": self.runtime.settings.max_queue}, "budget": self.budget}

    def metrics(self):
        lines = []
        for name, value in sorted(self.counters.items()):
            lines.append(f"pank_results_{name}_total {value}")
        for name, (total, count) in sorted(self.durations.items()):
            lines.extend([f"pank_results_{name}_seconds_sum {total}", f"pank_results_{name}_seconds_count {count}"])
        lines += [f"pank_results_active {self.runtime.active}", f"pank_results_queue_depth {max(0, len(self.runtime.tasks) - self.runtime.active)}"]
        layout, resources = self.runtime.layout.snapshot(), self.runtime.resources.snapshot()
        for key in ("cache_hits", "timeouts", "fallbacks"):
            if isinstance(layout.get(key), (int, float)):
                lines.append(f"pank_results_layout_{key} {layout[key]}")
        for key in ("cache_hits", "active_fetches"):
            if isinstance(resources.get(key), (int, float)):
                lines.append(f"pank_results_resources_{key} {resources[key]}")
        for key in ("spent_usd", "reserved_usd", "remaining_usd", "input_tokens", "output_tokens"):
            if self.budget is not None:
                lines.append(f"pank_results_shared_budget_{key} {self.budget.get(key, 0)}")
        return "\n".join(lines) + "\n"
