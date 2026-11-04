import heapq
import math
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import LayoutConfig
from .geometry import (
    Point,
    Rect,
    expand_rect,
    rect_for,
    rects_overlap,
    route_segments,
    segment_intersects_rect,
    segment_length,
    segments_intersect,
    simplify_polyline,
)


def _edge_id(edge: Dict[str, Any], index: int) -> str:
    return str(edge.get("~id") or f"edge:{index}")


def _side_for(source: Dict[str, Any], target: Dict[str, Any]) -> str:
    dx = float(target["x"]) - float(source["x"])
    dy = float(target["y"]) - float(source["y"])
    if abs(dy) >= abs(dx):
        return "bottom" if dy > 0 else "top"
    return "right" if dx > 0 else "left"


def _base_port(point: Dict[str, Any], side: str, offset: float = 0.0) -> Point:
    x, y = float(point["x"]), float(point["y"])
    half_width = float(point.get("width", 120.0)) / 2
    half_height = float(point.get("height", 44.0)) / 2
    if side == "top":
        return (x + offset, y - half_height)
    if side == "bottom":
        return (x + offset, y + half_height)
    if side == "left":
        return (x - half_width, y + offset)
    return (x + half_width, y + offset)


def _port_assignments(
    coords: Dict[str, Dict[str, Any]],
    edges: List[Dict[str, Any]],
    config: LayoutConfig,
) -> Dict[Tuple[str, str], Point]:
    side_edges = defaultdict(list)
    normalized = []
    for index, edge in enumerate(edges):
        edge_id = _edge_id(edge, index)
        source_id = str(edge.get("~start", ""))
        target_id = str(edge.get("~end", ""))
        if source_id not in coords or target_id not in coords:
            continue
        source_side = _side_for(coords[source_id], coords[target_id])
        target_side = _side_for(coords[target_id], coords[source_id])
        normalized.append((edge_id, source_id, target_id, source_side, target_side))
        side_edges[(source_id, source_side)].append(edge_id)
        side_edges[(target_id, target_side)].append(edge_id)

    assignments = {}
    for edge_id, source_id, target_id, source_side, target_side in normalized:
        for node_id, side in ((source_id, source_side), (target_id, target_side)):
            ordered = sorted(side_edges[(node_id, side)])
            index = ordered.index(edge_id)
            raw_offset = (index - (len(ordered) - 1) / 2) * config.edge_spacing
            point = coords[node_id]
            extent = (
                float(point.get("width", 120.0)) / 2 - 4
                if side in {"top", "bottom"}
                else float(point.get("height", 44.0)) / 2 - 4
            )
            offset = max(-extent, min(extent, raw_offset))
            assignments[(edge_id, node_id)] = _base_port(point, side, offset)
    return assignments


def _segment_clear(
    segment: Tuple[Point, Point],
    obstacles: Dict[str, Rect],
    excluded: Set[str],
) -> bool:
    return not any(
        node_id not in excluded and segment_intersects_rect(segment, rect, allow_boundary=True)
        for node_id, rect in obstacles.items()
    )


def _route_clear(
    route: Dict[str, object],
    obstacles: Dict[str, Rect],
    excluded: Set[str],
) -> bool:
    return all(_segment_clear(segment, obstacles, excluded) for segment in route_segments(route))


def _crossing_cost(segment: Tuple[Point, Point], routed: Iterable[Dict[str, object]]) -> int:
    return sum(
        1
        for route in routed
        for other_segment in route_segments(route)
        if segments_intersect(segment, other_segment, include_endpoints=False)
    )


def _edge_label(edge: Dict[str, Any]) -> str:
    return str(edge.get("~type") or "").replace("_", " ").strip()


def _label_rect(anchor: Point, text: str, config: LayoutConfig) -> Rect:
    # Matches the compact Cytoscape edge-label treatment closely enough for
    # deterministic collision avoidance without browser font measurement.
    width = max(18.0, min(220.0, len(text) * 5.8 + config.label_padding * 2))
    height = 10.0 + config.label_padding * 2
    return (
        anchor[0] - width / 2,
        anchor[1] - height / 2,
        anchor[0] + width / 2,
        anchor[1] + height / 2,
    )


def _route_label_anchor(route: Dict[str, object]) -> Point:
    if route.get("route_type") == "bezier":
        points = [tuple(point) for point in route.get("control_points", [])]
        source = tuple(route["source_port"])
        target = tuple(route["target_port"])
        if len(points) == 2:
            first, second = points
            t = 0.5
            inverse = 1.0 - t
            return (
                inverse ** 3 * source[0] + 3 * inverse ** 2 * t * first[0]
                + 3 * inverse * t ** 2 * second[0] + t ** 3 * target[0],
                inverse ** 3 * source[1] + 3 * inverse ** 2 * t * first[1]
                + 3 * inverse * t ** 2 * second[1] + t ** 3 * target[1],
            )
    segments = route_segments(route)
    if not segments:
        return (0.0, 0.0)
    segment = max(segments, key=segment_length)
    return ((segment[0][0] + segment[1][0]) / 2, (segment[0][1] + segment[1][1]) / 2)


def _label_is_safe(
    rect: Rect,
    obstacles: Dict[str, Rect],
    routed: Iterable[Dict[str, object]],
) -> bool:
    if any(rects_overlap(rect, obstacle) for obstacle in obstacles.values()):
        return False
    return not any(
        segment_intersects_rect(segment, rect, allow_boundary=True)
        for route in routed
        for segment in route_segments(route)
    )


def _annotate_label(
    route: Dict[str, object],
    edge: Dict[str, Any],
    obstacles: Dict[str, Rect],
    routed: Iterable[Dict[str, object]],
    config: LayoutConfig,
) -> Tuple[Dict[str, object], Optional[Rect]]:
    label = _edge_label(edge)
    anchor = _route_label_anchor(route)
    route["label_anchor"] = [round(anchor[0], 3), round(anchor[1], 3)]
    route["label_clearance"] = round(config.label_clearance, 3)
    route["label_rotation"] = 0
    if not label:
        route["label_visible"] = False
        route["label_status"] = "empty"
        return route, None
    rect = _label_rect(anchor, label, config)
    if _label_is_safe(rect, obstacles, routed):
        route["label_visible"] = True
        route["label_status"] = "full"
        return route, expand_rect(rect, config.label_clearance)
    route["label_visible"] = False
    route["label_status"] = "hidden_no_safe_route"
    return route, None


def _parallel_curve_offsets(edges: List[Dict[str, Any]], config: LayoutConfig) -> Dict[str, float]:
    groups = defaultdict(list)
    for index, edge in enumerate(edges):
        source, target = str(edge.get("~start", "")), str(edge.get("~end", ""))
        groups[tuple(sorted((source, target)))].append(_edge_id(edge, index))
    offsets = {}
    for edge_ids in groups.values():
        ordered = sorted(edge_ids)
        for index, edge_id in enumerate(ordered):
            # Leave room for both the stroke and an edge label; the configured
            # spacing is the visible minimum, not merely a port delta.
            offsets[edge_id] = (index - (len(ordered) - 1) / 2) * config.edge_spacing * 3
    return offsets


def _smooth_bezier(source: Point, target: Point, curve_offset: float) -> Dict[str, object]:
    dx, dy = target[0] - source[0], target[1] - source[1]
    length = max(math.hypot(dx, dy), 1.0)
    normal = (-dy / length, dx / length)
    # A slight deterministic bow improves visual flow even for non-parallel
    # edges, while parallel edges separate by the configured spacing.
    bow = min(14.0, max(2.0, length * 0.04)) + curve_offset
    first = (source[0] + dx * 0.32 + normal[0] * bow, source[1] + dy * 0.32 + normal[1] * bow)
    second = (target[0] - dx * 0.32 + normal[0] * bow, target[1] - dy * 0.32 + normal[1] * bow)
    return {
        "route_type": "bezier",
        "source_port": [round(source[0], 3), round(source[1], 3)],
        "target_port": [round(target[0], 3), round(target[1], 3)],
        "control_points": [[round(first[0], 3), round(first[1], 3)], [round(second[0], 3), round(second[1], 3)]],
        "tangent_mode": "cubic_smooth",
    }


def _orthogonal_candidates(
    source: Point,
    target: Point,
    obstacles: Dict[str, Rect],
    excluded: Set[str],
    routed: Iterable[Dict[str, object]],
    spacing: float,
) -> Optional[List[Point]]:
    remaining = [rect for node_id, rect in obstacles.items() if node_id not in excluded]
    xs = {source[0], target[0]}
    ys = {source[1], target[1]}
    for left, top, right, bottom in remaining:
        xs.update((left, right))
        ys.update((top, bottom))
    if remaining:
        xs.update((min(rect[0] for rect in remaining) - spacing, max(rect[2] for rect in remaining) + spacing))
        ys.update((min(rect[1] for rect in remaining) - spacing, max(rect[3] for rect in remaining) + spacing))

    candidates = [
        [source, (source[0], target[1]), target],
        [source, (target[0], source[1]), target],
    ]
    candidates.extend([source, (x, source[1]), (x, target[1]), target] for x in sorted(xs))
    candidates.extend([source, (source[0], y), (target[0], y), target] for y in sorted(ys))

    best = None
    best_score = None
    routed_list = list(routed)
    for candidate in candidates:
        simplified = simplify_polyline(candidate)
        segments = list(zip(simplified, simplified[1:]))
        if not all(_segment_clear(segment, obstacles, excluded) for segment in segments):
            continue
        crossing = sum(_crossing_cost(segment, routed_list) for segment in segments)
        length = sum(segment_length(segment) for segment in segments)
        bends = max(0, len(segments) - 1)
        score = (crossing, bends, round(length, 6), tuple(simplified))
        if best_score is None or score < best_score:
            best, best_score = simplified, score
    return best


def _visibility_route(
    source: Point,
    target: Point,
    obstacles: Dict[str, Rect],
    excluded: Set[str],
    routed: Iterable[Dict[str, object]],
) -> Optional[List[Point]]:
    points = {source, target}
    obstacle_xs = set()
    obstacle_ys = set()
    for node_id, rect in obstacles.items():
        if node_id in excluded:
            continue
        left, top, right, bottom = rect
        obstacle_xs.update((left, right))
        obstacle_ys.update((top, bottom))
        points.update(((left, top), (right, top), (right, bottom), (left, bottom)))
    points.update(
        {
            (source[0], target[1]),
            (target[0], source[1]),
            *((x, source[1]) for x in obstacle_xs),
            *((x, target[1]) for x in obstacle_xs),
            *((source[0], y) for y in obstacle_ys),
            *((target[0], y) for y in obstacle_ys),
        }
    )

    by_x = defaultdict(list)
    by_y = defaultdict(list)
    for point in sorted(points):
        by_x[round(point[0], 6)].append(point)
        by_y[round(point[1], 6)].append(point)

    graph = defaultdict(list)
    for groups, sort_index in ((by_x, 1), (by_y, 0)):
        for group in groups.values():
            ordered = sorted(set(group), key=lambda point: (point[sort_index], point[1 - sort_index]))
            for left, right in zip(ordered, ordered[1:]):
                segment = (left, right)
                if _segment_clear(segment, obstacles, excluded):
                    length = segment_length(segment)
                    crossing = _crossing_cost(segment, routed)
                    graph[left].append((right, length, crossing))
                    graph[right].append((left, length, crossing))

    queue = [(0.0, 0, source, None, (source,))]
    best = {}
    while queue:
        cost, bends, point, direction, path = heapq.heappop(queue)
        state = (point, direction)
        if best.get(state, math.inf) <= cost:
            continue
        best[state] = cost
        if point == target:
            simplified = simplify_polyline(path)
            if all(
                _segment_clear(segment, obstacles, excluded)
                for segment in zip(simplified, simplified[1:])
            ):
                return simplified
            return list(path)
        for neighbor, length, crossing in sorted(graph.get(point, ())):
            next_direction = "h" if abs(neighbor[1] - point[1]) < 1e-7 else "v"
            bend = 1 if direction and next_direction != direction else 0
            next_cost = cost + length + crossing * 10000 + bend * 40
            heapq.heappush(
                queue,
                (next_cost, bends + bend, neighbor, next_direction, (*path, neighbor)),
            )
    return None


def _valid_previous_route(
    route: Any,
    obstacles: Dict[str, Rect],
    excluded: Set[str],
) -> bool:
    if not isinstance(route, dict) or route.get("route_type") not in {"bezier", "polyline"}:
        return False
    points = []
    for key in ("source_port", "target_port"):
        point = route.get(key)
        if not (
            isinstance(point, list)
            and len(point) == 2
            and all(isinstance(value, (int, float)) and math.isfinite(value) for value in point)
        ):
            return False
        points.append(point)
    middle_key = "control_points" if route["route_type"] == "bezier" else "waypoints"
    middle = route.get(middle_key, [])
    if not isinstance(middle, list) or len(middle) > 8:
        return False
    if any(
        not isinstance(point, list)
        or len(point) != 2
        or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in point)
        for point in middle
    ):
        return False
    return _route_clear(route, obstacles, excluded)


def _outer_channel_route(
    source_id: str,
    target_id: str,
    coords: Dict[str, Dict[str, Any]],
    obstacles: Dict[str, Rect],
    routed: Iterable[Dict[str, object]],
    spacing: float,
) -> Optional[List[Point]]:
    """Find a sparse, obstacle-free route around the outside perimeter."""
    left = min(rect[0] for rect in obstacles.values()) - spacing
    top = min(rect[1] for rect in obstacles.values()) - spacing
    right = max(rect[2] for rect in obstacles.values()) + spacing
    bottom = max(rect[3] for rect in obstacles.values()) + spacing
    channels = {"left": left, "top": top, "right": right, "bottom": bottom}
    corners = {
        ("top", "left"): (left, top),
        ("top", "right"): (right, top),
        ("bottom", "left"): (left, bottom),
        ("bottom", "right"): (right, bottom),
    }
    side_order = ("top", "right", "bottom", "left")

    def escape(point: Dict[str, Any], side: str) -> Tuple[Point, Point]:
        port = _base_port(point, side)
        if side in {"top", "bottom"}:
            return port, (port[0], channels[side])
        return port, (channels[side], port[1])

    def perimeter_paths(source_side: str, source_escape: Point, target_side: str, target_escape: Point):
        if source_side == target_side:
            return [[source_escape, target_escape]]
        source_index = side_order.index(source_side)
        target_index = side_order.index(target_side)
        paths = []
        for step in (1, -1):
            side = source_side
            index = source_index
            points = [source_escape]
            while index != target_index:
                next_index = (index + step) % len(side_order)
                next_side = side_order[next_index]
                corner = corners.get((side, next_side)) or corners.get((next_side, side))
                points.append(corner)
                side = next_side
                index = next_index
            points.append(target_escape)
            paths.append(points)
        return paths

    excluded = {source_id, target_id}
    routed_list = list(routed)
    candidates = []
    for source_side in side_order:
        source_port, source_escape = escape(coords[source_id], source_side)
        for target_side in side_order:
            target_port, target_escape = escape(coords[target_id], target_side)
            for perimeter in perimeter_paths(
                source_side,
                source_escape,
                target_side,
                target_escape,
            ):
                candidate = simplify_polyline(
                    [source_port, *perimeter, target_port]
                )
                segments = list(zip(candidate, candidate[1:]))
                if not all(_segment_clear(segment, obstacles, excluded) for segment in segments):
                    continue
                crossings = sum(
                    _crossing_cost(segment, routed_list)
                    for segment in segments
                )
                length = sum(segment_length(segment) for segment in segments)
                bends = max(0, len(segments) - 1)
                candidates.append(
                    ((crossings, bends, round(length, 6), tuple(candidate)), candidate)
                )
    return min(candidates, default=(None, None), key=lambda item: item[0])[1]


def route_edges(
    coords: Dict[str, Dict[str, Any]],
    edges: List[Dict[str, Any]],
    config: LayoutConfig,
    previous_routes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, object]]:
    previous_routes = previous_routes or {}
    obstacles = {
        node_id: expand_rect(rect_for(point), config.edge_clearance)
        for node_id, point in coords.items()
    }
    ports = _port_assignments(coords, edges, config)
    curve_offsets = _parallel_curve_offsets(edges, config)
    routes: Dict[str, Dict[str, object]] = {}
    label_obstacles: Dict[str, Rect] = {}
    normalized = sorted(
        ((_edge_id(edge, index), edge) for index, edge in enumerate(edges)),
        key=lambda item: item[0],
    )
    for edge_id, edge in normalized:
        source_id = str(edge.get("~start", ""))
        target_id = str(edge.get("~end", ""))
        if source_id not in coords or target_id not in coords or source_id == target_id:
            continue
        excluded = {source_id, target_id}
        previous = previous_routes.get(edge_id)
        active_obstacles = {**obstacles, **label_obstacles}
        if _valid_previous_route(previous, active_obstacles, excluded):
            route, label_rect = _annotate_label(previous, edge, obstacles, routes.values(), config)
            routes[edge_id] = route
            if label_rect:
                label_obstacles[f"label:{edge_id}"] = label_rect
            continue

        source = ports[(edge_id, source_id)]
        target = ports[(edge_id, target_id)]
        direct = _smooth_bezier(source, target, curve_offsets.get(edge_id, 0.0))
        if (
            _route_clear(direct, active_obstacles, excluded)
            and not any(_crossing_cost(segment, routes.values()) for segment in route_segments(direct))
        ):
            route, label_rect = _annotate_label(direct, edge, obstacles, routes.values(), config)
            routes[edge_id] = route
            if label_rect:
                label_obstacles[f"label:{edge_id}"] = label_rect
            continue

        visibility = _orthogonal_candidates(
            source,
            target,
            active_obstacles,
            excluded,
            routes.values(),
            config.edge_spacing,
        )
        if visibility is None:
            visibility = _visibility_route(
                source,
                target,
                active_obstacles,
                excluded,
                routes.values(),
            )
        if visibility:
            routed_edge = {
                "route_type": "polyline",
                "source_port": [round(source[0], 3), round(source[1], 3)],
                "target_port": [round(target[0], 3), round(target[1], 3)],
                "waypoints": [
                    [round(point[0], 3), round(point[1], 3)]
                    for point in visibility[1:-1]
                ],
            }
            if max(0, len(visibility) - 2) > 4:
                routed_edge["fallback"] = True
            route, label_rect = _annotate_label(routed_edge, edge, obstacles, routes.values(), config)
            routes[edge_id] = route
            if label_rect:
                label_obstacles[f"label:{edge_id}"] = label_rect
            continue

        outer = _outer_channel_route(
            source_id,
            target_id,
            coords,
            active_obstacles,
            routes.values(),
            config.edge_spacing,
        )
        if outer:
            route = {
                "route_type": "polyline",
                "source_port": [round(outer[0][0], 3), round(outer[0][1], 3)],
                "target_port": [round(outer[-1][0], 3), round(outer[-1][1], 3)],
                "waypoints": [
                    [round(point[0], 3), round(point[1], 3)]
                    for point in outer[1:-1]
                ],
                "fallback": True,
            }
            route, label_rect = _annotate_label(route, edge, obstacles, routes.values(), config)
            routes[edge_id] = route
            if label_rect:
                label_obstacles[f"label:{edge_id}"] = label_rect
            continue

        # Deterministic, bounded fallback outside all obstacles.
        right_channel = max(rect[2] for rect in obstacles.values()) + config.edge_spacing
        route = {
            "route_type": "polyline",
            "source_port": [round(source[0], 3), round(source[1], 3)],
            "target_port": [round(target[0], 3), round(target[1], 3)],
            "waypoints": [
                [round(right_channel, 3), round(source[1], 3)],
                [round(right_channel, 3), round(target[1], 3)],
            ],
            "fallback": True,
        }
        route, label_rect = _annotate_label(route, edge, obstacles, routes.values(), config)
        routes[edge_id] = route
        if label_rect:
            label_obstacles[f"label:{edge_id}"] = label_rect
    return routes
