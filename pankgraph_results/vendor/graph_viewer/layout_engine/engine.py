import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import LayoutConfig
from .metrics import compute_metrics
from .optimizer import optimize_node_positions
from .router import route_edges


@dataclass
class LayoutResult:
    coords: Dict[str, Dict[str, Any]]
    edge_routes: Dict[str, Dict[str, object]]
    metadata: Dict[str, Any]


def _xy_to_coords(xy_json: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    coords = {}
    for node_id, value in xy_json.items():
        if not isinstance(node_id, str) or not isinstance(value, dict):
            raise ValueError("previous_layout.xy_json contains an invalid node entry.")
        start, end = value.get("start_xy"), value.get("end_xy")
        if not (
            isinstance(start, list)
            and isinstance(end, list)
            and len(start) == 2
            and len(end) == 2
            and all(
                isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number)
                for number in (*start, *end)
            )
        ):
            raise ValueError("previous_layout.xy_json contains non-finite rectangles.")
        coords[node_id] = {
            "x": (float(start[0]) + float(end[0])) / 2,
            "y": (float(start[1]) + float(end[1])) / 2,
            "width": abs(float(end[0]) - float(start[0])),
            "height": abs(float(end[1]) - float(start[1])),
            "Level": value.get("Level", "Neighbor"),
        }
    return coords


def _validated_previous(
    previous_layout: Any,
    current: Dict[str, Dict[str, Any]],
    fingerprint: str,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any], str]:
    if previous_layout is None:
        return {}, {}, "not_provided"
    if not isinstance(previous_layout, dict):
        return {}, {}, "invalid_object"
    if previous_layout.get("version") != 1:
        return {}, {}, "version_mismatch"
    if previous_layout.get("config_fingerprint") != fingerprint:
        return {}, {}, "config_mismatch"
    try:
        previous_coords = _xy_to_coords(previous_layout.get("xy_json") or {})
    except ValueError:
        return {}, {}, "invalid_coordinates"
    if not previous_coords or not set(previous_coords).issubset(current):
        return {}, {}, "node_set_mismatch"
    for node_id, old in previous_coords.items():
        point = current[node_id]
        if (
            abs(float(point.get("width", 120.0)) - float(old["width"])) > 0.01
            or abs(float(point.get("height", 44.0)) - float(old["height"])) > 0.01
        ):
            return {}, {}, "dimension_mismatch"
    routes = previous_layout.get("edge_routes") or {}
    if not isinstance(routes, dict):
        routes = {}
    return previous_coords, routes, "accepted"


def optimize_layout(
    graph: Dict[str, List[Dict[str, Any]]],
    initial_coords: Dict[str, Dict[str, Any]],
    fixed_y_ids: Set[str],
    config: LayoutConfig,
    layout_mode: str,
    track_policy: str = "",
    previous_layout: Optional[Dict[str, Any]] = None,
) -> LayoutResult:
    started = time.monotonic()
    coords = {
        str(node_id): dict(point)
        for node_id, point in sorted(initial_coords.items())
    }
    fingerprint = config.fingerprint(layout_mode, track_policy)
    previous_coords, previous_routes, previous_status = _validated_previous(
        previous_layout,
        coords,
        fingerprint,
    )
    for node_id, old in previous_coords.items():
        coords[node_id]["x"] = round(float(old["x"]), 3)
        coords[node_id]["y"] = round(float(old["y"]), 3)
        coords[node_id]["width"] = round(float(old["width"]), 3)
        coords[node_id]["height"] = round(float(old["height"]), 3)

    fixed_y = {
        node_id: float(coords[node_id]["y"])
        for node_id in fixed_y_ids
        if node_id in coords
    }
    optimized = optimize_node_positions(
        coords,
        graph.get("edges", []),
        fixed_y_ids,
        set(previous_coords),
        config,
    )
    routes = route_edges(
        optimized,
        graph.get("edges", []),
        config,
        previous_routes if previous_status == "accepted" else None,
    )
    metrics = compute_metrics(
        optimized,
        graph.get("edges", []),
        routes,
        fixed_y=fixed_y,
        previous_coords=previous_coords,
    )
    metrics["runtime_ms"] = round((time.monotonic() - started) * 1000, 3)
    metrics["track_policy"] = track_policy or "none"
    metrics["track_policy_violation_count"] = 0
    metrics["genomic_order_violation_count"] = 0
    return LayoutResult(
        coords=optimized,
        edge_routes=routes,
        metadata={
            "engine": "optimized_v1",
            "version": 1,
            "config_fingerprint": fingerprint,
            "previous_layout_status": previous_status,
            "track_policy": track_policy or "none",
            "metrics": metrics,
        },
    )
