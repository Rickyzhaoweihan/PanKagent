import hashlib
import json
import math
from itertools import combinations
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .geometry import (
    Rect,
    overlap_area,
    rect_for,
    rects_overlap,
    route_segments,
    segment_intersects_rect,
    segment_length,
    segments_intersect,
)


def _label_rect(route: Dict[str, object], edge: Dict[str, Any]) -> Optional[Rect]:
    if not route.get("label_visible"):
        return None
    anchor = route.get("label_anchor")
    if not (
        isinstance(anchor, list)
        and len(anchor) == 2
        and all(isinstance(value, (int, float)) and math.isfinite(value) for value in anchor)
    ):
        return None
    text = str(edge.get("~type") or "").replace("_", " ").strip()
    if not text:
        return None
    padding = 4.0
    width = max(18.0, min(220.0, len(text) * 5.8 + padding * 2))
    height = 10.0 + padding * 2
    return (anchor[0] - width / 2, anchor[1] - height / 2, anchor[0] + width / 2, anchor[1] + height / 2)


def _edge_nodes(edges: Iterable[Dict[str, Any]]) -> Dict[str, Set[str]]:
    result = {}
    for index, edge in enumerate(edges):
        edge_id = str(edge.get("~id") or f"edge:{index}")
        result[edge_id] = {str(edge.get("~start", "")), str(edge.get("~end", ""))}
    return result


def compute_metrics(
    coords: Dict[str, Dict[str, Any]],
    edges: List[Dict[str, Any]],
    routes: Dict[str, Dict[str, object]],
    fixed_y: Optional[Dict[str, float]] = None,
    previous_coords: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    fixed_y = fixed_y or {}
    previous_coords = previous_coords or {}
    overlaps = []
    for left_id, right_id in combinations(sorted(coords), 2):
        left, right = rect_for(coords[left_id]), rect_for(coords[right_id])
        if rects_overlap(left, right):
            overlaps.append(overlap_area(left, right))

    edge_nodes = _edge_nodes(edges)
    edge_by_id = {
        str(edge.get("~id") or f"edge:{index}"): edge
        for index, edge in enumerate(edges)
    }
    edge_node_intersections = 0
    for edge_id, route in routes.items():
        excluded = edge_nodes.get(edge_id, set())
        for node_id, point in coords.items():
            if node_id in excluded:
                continue
            if any(segment_intersects_rect(segment, rect_for(point), allow_boundary=True) for segment in route_segments(route)):
                edge_node_intersections += 1

    crossings = 0
    for left_id, right_id in combinations(sorted(routes), 2):
        if edge_nodes.get(left_id, set()).intersection(edge_nodes.get(right_id, set())):
            continue
        if any(
            segments_intersect(left, right, include_endpoints=False)
            for left in route_segments(routes[left_id])
            for right in route_segments(routes[right_id])
                ):
            crossings += 1

    label_node_intersections = 0
    label_edge_intersections = 0
    label_hidden_count = 0
    for edge_id, route in routes.items():
        if route.get("label_status") == "hidden_no_safe_route":
            label_hidden_count += 1
        label_rect = _label_rect(route, edge_by_id.get(edge_id, {}))
        if not label_rect:
            continue
        endpoints = edge_nodes.get(edge_id, set())
        label_node_intersections += sum(
            1
            for node_id, point in coords.items()
            if node_id not in endpoints and rects_overlap(label_rect, rect_for(point))
        )
        label_edge_intersections += sum(
            1
            for other_id, other in routes.items()
            if other_id != edge_id
            and any(segment_intersects_rect(segment, label_rect, allow_boundary=True) for segment in route_segments(other))
        )

    bends = []
    nonfallback_bends = []
    lengths = []
    stretches = []
    for route in routes.values():
        segments = route_segments(route)
        route_bends = 0 if route.get("route_type") == "bezier" else max(0, len(segments) - 1)
        bends.append(route_bends)
        if not route.get("fallback"):
            nonfallback_bends.append(route_bends)
        length = sum(segment_length(segment) for segment in segments)
        lengths.append(length)
        if segments:
            direct = math.hypot(
                segments[-1][1][0] - segments[0][0][0],
                segments[-1][1][1] - segments[0][0][1],
            )
            stretches.append(length / direct if direct > 1e-7 else 1.0)

    displacement = 0.0
    for node_id, old in previous_coords.items():
        if node_id in coords:
            displacement += math.hypot(
                float(coords[node_id]["x"]) - float(old["x"]),
                float(coords[node_id]["y"]) - float(old["y"]),
            )

    normalized = {
        "coords": {
            node_id: {
                key: round(float(value), 3) if isinstance(value, (int, float)) else value
                for key, value in sorted(point.items())
            }
            for node_id, point in sorted(coords.items())
        },
        "routes": routes,
    }
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "node_overlap_count": len(overlaps),
        "node_overlap_area": round(sum(overlaps), 3),
        "fixed_lane_violation_count": sum(
            1
            for node_id, expected_y in fixed_y.items()
            if node_id in coords and abs(float(coords[node_id]["y"]) - expected_y) > 1e-6
        ),
        "edge_node_intersection_count": edge_node_intersections,
        "edge_crossing_count": crossings,
        "edge_label_node_intersection_count": label_node_intersections,
        "edge_label_edge_intersection_count": label_edge_intersections,
        "edge_label_hidden_count": label_hidden_count,
        "smooth_cubic_edge_count": sum(
            1
            for route in routes.values()
            if route.get("route_type") == "bezier" and len(route.get("control_points", [])) == 2
        ),
        "total_bend_count": sum(bends),
        "max_bends_per_edge": max(bends, default=0),
        "max_bends_nonfallback": max(nonfallback_bends, default=0),
        "fallback_route_count": sum(1 for route in routes.values() if route.get("fallback")),
        "total_route_length": round(sum(lengths), 3),
        "median_route_stretch": round(median(stretches), 6) if stretches else 0.0,
        "previous_node_displacement": round(displacement, 6),
        "layout_hash": digest,
    }
