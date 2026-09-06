"""Bounded, data-only regular-graph layouts in an isolated process.

The upstream query handler is deliberately absent. Rendering may hide evidence
for readability, but never changes the scientific completeness of a run.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import signal
import sys
import time
from collections import Counter, OrderedDict, deque
from pathlib import Path

from .projection import project_evidence
from .vendor.graph_viewer.filtering import filter_graph
from .vendor.graph_viewer.layout_engine.config import LayoutConfig


UPSTREAM_COMMIT = "362025db24b1d37223c3c44ccf02a55eb2756a42"
LAYOUT_VERSION = "pankgraph-regular-2"
COORDINATE_SCALE = 1 / 3
# A fixed iteration count makes completed layouts deterministic. The parent
# process owns the hard wall-clock bound across optimization, routing and metrics.
CONFIG = LayoutConfig(max_iterations=1, time_budget_ms=3_600_000)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _initial_coords(graph: dict, focus: list[str]) -> dict:
    """Deterministic breadth-first grid using the existing compact visual scale."""
    nodes = {node["~id"]: node for node in graph["nodes"]}
    adjacency = {nid: set() for nid in nodes}
    for edge in graph["edges"]:
        adjacency[edge["~start"]].add(edge["~end"])
        adjacency[edge["~end"]].add(edge["~start"])
    order, seen = [], set()
    for seed in [*focus, *sorted(nodes, key=lambda nid: (-len(adjacency[nid]), nid))]:
        if seed not in nodes or seed in seen:
            continue
        queue = deque([seed])
        seen.add(seed)
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for neighbor in sorted(adjacency[nid], key=lambda item: (-len(adjacency[item]), item)):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
    dimensions = {}
    for nid in order:
        label = str(nodes[nid].get("display_label") or nid)
        # The renderer uses 6px text and 4px padding on each side. Scale the
        # entire upstream coordinate system rather than changing its geometry.
        width = max(20.0, min(80, len(label)) * 3.6 + 10)
        dimensions[nid] = (width / COORDINATE_SCALE, 16 / COORDINATE_SCALE)
    columns = max(1, math.ceil(math.sqrt(len(order))))
    spacing_x = max((width for width, _ in dimensions.values()), default=60) + 108
    spacing_y = 156
    rows = math.ceil(len(order) / columns)
    return {nid: {"x": round(((index % columns) - (columns - 1) / 2) * spacing_x, 3),
                  "y": round(((index // columns) - (rows - 1) / 2) * spacing_y, 3),
                  "width": dimensions[nid][0], "height": dimensions[nid][1],
                  "Level": "Core" if nid in focus else "Neighbor"}
            for index, nid in enumerate(order)}


def _xy(coords: dict) -> dict:
    result = {}
    for nid, point in sorted(coords.items()):
        x, y, width, height = (round(float(point[key]) * COORDINATE_SCALE, 3)
                               for key in ("x", "y", "width", "height"))
        result[nid] = {"x": x, "y": y, "width": width, "height": height, "Level": point["Level"],
                       "start_xy": [round(x - width / 2, 3), round(y - height / 2, 3)],
                       "end_xy": [round(x + width / 2, 3), round(y + height / 2, 3)]}
    return result


def _scale_routes(routes: dict, factor: float) -> dict:
    result = copy.deepcopy(routes)
    for route in result.values():
        for key in ("source_port", "target_port", "label_anchor"):
            if key in route:
                route[key] = [round(float(value) * factor, 3) for value in route[key]]
        for key in ("control_points", "waypoints"):
            if key in route:
                route[key] = [[round(float(value) * factor, 3) for value in point] for point in route[key]]
        if "label_clearance" in route:
            route["label_clearance"] = round(float(route["label_clearance"]) * factor, 3)
    return result


def _previous(value: dict | None, graph_version: str) -> tuple[dict | None, str]:
    if value is None:
        return None, "not_provided"
    if not isinstance(value, dict) or value.get("layout_version") != LAYOUT_VERSION:
        return None, "version_mismatch"
    if value.get("graph_version") != graph_version:
        return None, "graph_version_mismatch"
    if value.get("config_fingerprint") != CONFIG.fingerprint("kg_only"):
        return None, "config_mismatch"
    coords = value.get("xy_json")
    if not isinstance(coords, dict) or len(coords) > 100:
        return None, "invalid_coordinates"
    converted = {}
    try:
        for nid, point in coords.items():
            if not isinstance(nid, str) or not isinstance(point, dict):
                raise ValueError
            x, y, width, height = [point[key] for key in ("x", "y", "width", "height")]
            if any(isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number)
                   or abs(number) > 1_000_000 for number in (x, y, width, height)) or min(width, height) <= 0:
                raise ValueError
            converted[nid] = {"start_xy": [(x - width / 2) / COORDINATE_SCALE, (y - height / 2) / COORDINATE_SCALE],
                              "end_xy": [(x + width / 2) / COORDINATE_SCALE, (y + height / 2) / COORDINATE_SCALE],
                              "Level": point.get("Level", "Neighbor")}
        # Previous edge routes are recomputed. Node positions are sufficient for
        # stable expansion, and untrusted browser route geometry is not replayed.
    except (KeyError, TypeError, ValueError):
        return None, "invalid_coordinates"
    return {"version": 1, "config_fingerprint": value["config_fingerprint"], "xy_json": converted}, "candidate"


def _worker(payload: dict, emit=None) -> dict:
    """Run pinned primitives in stages so an expensive route cannot discard nodes.

    Orchestration is a versioned adapter; upstream source files remain unchanged.
    Completed route batches are checkpointed. Remaining edges have explicit,
    deterministic fallback curves rather than pretending obstacle avoidance.
    """
    from .vendor.graph_viewer.layout_engine.engine import _validated_previous
    from .vendor.graph_viewer.layout_engine.optimizer import optimize_node_positions
    from .vendor.graph_viewer.layout_engine.router import route_edges, _smooth_bezier, _parallel_curve_offsets
    from .vendor.graph_viewer.layout_engine.metrics import compute_metrics
    from .vendor.graph_viewer.layout_engine.geometry import rect_for, route_segments, segment_intersects_rect

    graph = payload["graph"]
    coords = payload["coords"]
    previous, _, previous_status = _validated_previous(payload.get("previous_layout"), coords, CONFIG.fingerprint("kg_only"))
    for nid, point in previous.items():
        coords[nid].update({key: round(float(point[key]), 3) for key in ("x", "y", "width", "height")})
    coords = optimize_node_positions(coords, graph["edges"], set(), set(previous), CONFIG)
    node_metrics = compute_metrics(coords, [], {}, previous_coords=previous)
    if node_metrics["node_overlap_count"]:
        return {"status": "fallback", "reason": "geometry_check_failed"}
    # Straight/bowed fallback geometry matches the conventional Cytoscape style.
    offsets = _parallel_curve_offsets(graph["edges"], CONFIG)
    fallback_routes = {}
    for edge in graph["edges"]:
        start, end = coords[edge["~start"]], coords[edge["~end"]]
        direction = 1 if end["x"] >= start["x"] else -1
        source = (start["x"] + direction * start["width"] / 2, start["y"])
        target = (end["x"] - direction * end["width"] / 2, end["y"])
        route = _smooth_bezier(source, target, offsets.get(edge["~id"], 0))
        route.update(fallback=True, route_status="fallback", label_visible=True,
                     label_status="fallback_unchecked", label_anchor=[(source[0]+target[0])/2, (source[1]+target[1])/2])
        fallback_routes[edge["~id"]] = route
    routes = {}

    def checkpoint(reason="routing_in_progress"):
        combined = {**fallback_routes, **routes}
        complete = len(routes) == len(graph["edges"])
        metrics = {"node_overlap_count": 0, "previous_node_displacement": node_metrics["previous_node_displacement"],
                   "optimized_route_count": len(routes), "fallback_route_count": len(graph["edges"])-len(routes),
                   "edge_node_intersection_count": 0 if complete else None,
                   "metrics_scope": "optimized_routes_only" if not complete else "all_routes"}
        value = {"status": "optimized" if complete else "partial", "reason": None if complete else reason,
                 "xy_json": _xy(coords), "edge_routes": _scale_routes(combined, COORDINATE_SCALE),
                 "details": {"previous_layout_status": previous_status, "metrics": metrics}}
        if emit:
            emit(value)
        return value

    last = checkpoint()

    class RouteDeadline(Exception):
        pass

    def expire(*_):
        raise RouteDeadline

    old_handler = signal.signal(signal.SIGALRM, expire)
    budget = 1.5 if len(coords) > 50 else 3.0
    signal.setitimer(signal.ITIMER_REAL, budget)
    try:
        edges = graph["edges"]
        obstacles = {nid: rect_for(point) for nid, point in coords.items()}
        batch_ends = sorted({min(size, len(edges)) for size in (1, 4, 12, *range(24, len(edges) + 24, 24)) if edges})
        for end in batch_ends:
            batch_edges = edges[:min(end, len(edges))]
            proposed = route_edges(coords, batch_edges, CONFIG, previous_routes=routes)
            # The upstream last-resort channel is not guaranteed to avoid nodes.
            # Only label obstacle-checked routes as optimized.
            for edge in batch_edges:
                eid = edge["~id"]
                route = proposed.get(eid)
                if route and not any(segment_intersects_rect(segment, rect, allow_boundary=True)
                                     for nid, rect in obstacles.items() if nid not in {edge["~start"], edge["~end"]}
                                     for segment in route_segments(route)):
                    route["route_status"] = "optimized"
                    routes[eid] = route
            last = checkpoint("unroutable_edges")
    except RouteDeadline:
        last = checkpoint("routing_budget_exceeded")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
    return last


class LayoutService:
    def __init__(self, max_nodes: int = 100, timeout_seconds: float = 5):
        if isinstance(max_nodes, bool) or not 1 <= max_nodes <= 100:
            raise ValueError("Layout node budget must be between 1 and 100")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("Layout timeout must be positive and at most 30 seconds")
        self.max_nodes, self.timeout_seconds = int(max_nodes), float(timeout_seconds)
        self._closed = False
        self._processes = set()
        self._busy = False
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._observations = {"state": "unknown", "last_success": None, "last_error": None,
                              "calls": 0, "cache_hits": 0, "fallbacks": 0, "timeouts": 0}

    def snapshot(self) -> dict:
        return {**copy.deepcopy(self._observations), "worker_active": len(self._processes),
                "queue_depth": 0, "max_nodes": self.max_nodes, "timeout_seconds": self.timeout_seconds,
                "cache_entries": len(self._cache), "layout_version": LAYOUT_VERSION,
                "upstream_commit": UPSTREAM_COMMIT, "closed": self._closed}

    async def close(self) -> None:
        self._closed = True
        for process in list(self._processes):
            if process.returncode is None:
                process.kill()
            await process.wait()
        self._cache.clear()

    async def _run_worker(self, payload: dict) -> dict:
        # Spawn instead of a thread: cancellation and timeout actually stop CPU
        # work, including the upstream route/metric passes without their own cap.
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TMPDIR"}}
        package_root = str(Path(__file__).resolve().parent.parent)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pankgraph_results.layout", "--worker",
            cwd=package_root, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=4_000_000)
        self._processes.add(process)
        checkpoint = None
        try:
            encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()

            async def exchange():
                nonlocal checkpoint
                process.stdin.write(encoded)
                await process.stdin.drain()
                process.stdin.close()
                total = 0
                while line := await process.stdout.readline():
                    total += len(line)
                    if total > 8_000_000:
                        raise ValueError("Layout output exceeded process bound")
                    checkpoint = json.loads(line)
                await process.wait()
                if process.returncode or not checkpoint:
                    return {"status": "fallback", "reason": "worker_failed"}
                return checkpoint

            return await asyncio.wait_for(exchange(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            if checkpoint and checkpoint.get("xy_json"):
                return {**checkpoint, "status": "partial", "reason": "deadline_exceeded"}
            return {"status": "fallback", "reason": "deadline_exceeded"}
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            self._processes.discard(process)

    async def layout(self, evidence: dict, focus_ids: list[str] | None = None,
                     previous_layout: dict | None = None) -> dict:
        if self._closed:
            raise RuntimeError("Layout service is closed")
        started = time.monotonic()
        projected = project_evidence(evidence, focus_ids)
        graph, core, display = filter_graph([projected["combined_query_result"]], projected["core_nodes"],
                                            max_nodes=self.max_nodes, layout_mode="kg_only")
        core = sorted(set(core).intersection(node["~id"] for node in graph["nodes"]))
        coords = _initial_coords(graph, core)
        previous, previous_status = _previous(previous_layout, projected["graph_version"])
        # Bound dense graphs separately: all selected nodes remain visible while
        # at most 400 relationships are drawn. Prefer focus edges and type diversity.
        input_edges = graph["edges"]
        if len(input_edges) > 400:
            types, selected = set(), []
            ordered = sorted(input_edges, key=lambda edge: (not ({edge["~start"], edge["~end"]} & set(core)), edge["~id"]))
            for edge in ordered:
                if edge["~type"] not in types:
                    selected.append(edge)
                    types.add(edge["~type"])
                    if len(selected) == 400:
                        break
            chosen = {edge["~id"] for edge in selected}
            selected.extend(edge for edge in ordered if edge["~id"] not in chosen)
            graph["edges"] = selected[:400]
        key = _digest({"graph": graph, "core": core, "previous": previous, "version": LAYOUT_VERSION,
                       "graph_version": projected["graph_version"], "max_nodes": self.max_nodes})
        self._observations["calls"] += 1
        cached = self._cache.get(key)
        if cached:
            computed = copy.deepcopy(cached)
            self._cache.move_to_end(key)
            self._observations["cache_hits"] += 1
            cache_hit = True
        else:
            cache_hit = False
            if not graph["nodes"]:
                computed = {"status": "empty", "xy_json": {}, "edge_routes": {}}
            elif self._busy:
                computed = {"status": "fallback", "reason": "worker_busy"}
            else:
                self._busy = True
                try:
                    worker_graph = {"nodes": [{"~id": node["~id"]} for node in graph["nodes"]],
                                    "edges": [{key: edge[key] for key in ("~id", "~start", "~end", "~type")} for edge in graph["edges"]]}
                    computed = await self._run_worker({"graph": worker_graph, "coords": coords, "previous_layout": previous})
                except asyncio.CancelledError:
                    raise
                except (OSError, ValueError, TypeError):
                    computed = {"status": "fallback", "reason": "worker_unavailable"}
                finally:
                    self._busy = False
            if computed.get("status") == "fallback":
                computed.update(xy_json=_xy(coords), edge_routes={})
            if computed.get("status") != "fallback":
                self._cache[key] = copy.deepcopy(computed)
                while len(self._cache) > 8:
                    self._cache.popitem(last=False)
        status = computed["status"]
        fallback = status in {"fallback", "partial"}
        self._observations["state"] = "degraded" if fallback else "healthy"
        self._observations["last_error"] = computed.get("reason")
        self._observations["fallbacks"] += int(fallback)
        self._observations["timeouts"] += int(computed.get("reason") == "deadline_exceeded")
        if not fallback:
            self._observations["last_success"] = time.time()
        full = projected["full_evidence"]
        display.update(filtered_edge_count=len(graph["edges"]), hidden_edge_count=full["edge_count"] - len(graph["edges"]),
                       self_loop_count=sum(edge["~start"] == edge["~end"] for edge in projected["combined_query_result"]["edges"]),
                       edge_budget=400, display_complete=len(graph["nodes"]) == full["node_count"] and len(graph["edges"]) == full["edge_count"],
                       scientific_completeness=full["scientific_completeness"], evidence_unchanged=True)
        details = computed.get("details", {})
        layout_info = {"status": status, "mode": "kg_only", "engine": "deterministic_grid" if status == "fallback" else "optimized_v1",
                       "layout_version": LAYOUT_VERSION, "version": 1, "upstream_commit": UPSTREAM_COMMIT,
                       "config_fingerprint": CONFIG.fingerprint("kg_only"), "coordinate_scale": COORDINATE_SCALE,
                       "runtime_ms": round((time.monotonic() - started) * 1000, 2), "cache_hit": cache_hit,
                       "fallback_reason": computed.get("reason"),
                       "previous_layout_status": (details.get("previous_layout_status", previous_status)
                                                  if previous_status in {"candidate", "not_provided"} else previous_status),
                       "metrics": details.get("metrics", {})}
        return {"combined_query_result": graph, "core_nodes": core, "xy_json": computed["xy_json"],
                "edge_routes": computed["edge_routes"], "graph_version": projected["graph_version"],
                "layout_version": LAYOUT_VERSION, "version": 1, "config_fingerprint": CONFIG.fingerprint("kg_only"),
                "full_evidence": full, "display": display, "layout": layout_info,
                "metadata": {**projected["metadata"], **display, "layout": layout_info}}


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(2)
    try:
        data = sys.stdin.buffer.read(4_000_001)
        if len(data) > 4_000_000:
            raise ValueError("Layout input exceeds process bound")
        def emit(value):
            sys.stdout.write(json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        output = _worker(json.loads(data), emit=emit)
        emit(output)
    except Exception:
        sys.stdout.write('{"status":"fallback","reason":"worker_failed"}\n')
