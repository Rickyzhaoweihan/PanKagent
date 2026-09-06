import math
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Set, Tuple

from .config import LayoutConfig
from .geometry import SpatialHash, rect_for


def _edge_endpoints(edge: Dict[str, Any]) -> Tuple[str, str]:
    return str(edge.get("~start", "")), str(edge.get("~end", ""))


def _adjacency(node_ids: Iterable[str], edges: Iterable[Dict[str, Any]]) -> Dict[str, Set[str]]:
    result = {node_id: set() for node_id in node_ids}
    for edge in edges:
        start, end = _edge_endpoints(edge)
        if start in result and end in result and start != end:
            result[start].add(end)
            result[end].add(start)
    return result


def _node_score(
    node_id: str,
    candidate_x: float,
    candidate_y: float,
    coords: Dict[str, Dict[str, Any]],
    neighbors: Dict[str, Set[str]],
    preferred: Dict[str, Tuple[float, float]],
) -> Tuple[float, float, float]:
    length = 0.0
    for neighbor_id in neighbors[node_id]:
        neighbor = coords[neighbor_id]
        length += math.hypot(candidate_x - float(neighbor["x"]), candidate_y - float(neighbor["y"]))
    preferred_x, preferred_y = preferred[node_id]
    displacement = math.hypot(candidate_x - preferred_x, candidate_y - preferred_y)
    return (round(length, 6), round(displacement, 6), round(abs(candidate_x), 6))


def optimize_node_positions(
    coords: Dict[str, Dict[str, Any]],
    edges: List[Dict[str, Any]],
    fixed_y_ids: Set[str],
    pinned_ids: Set[str],
    config: LayoutConfig,
) -> Dict[str, Dict[str, Any]]:
    optimized = {node_id: dict(point) for node_id, point in sorted(coords.items())}
    neighbors = _adjacency(optimized, edges)
    preferred = {
        node_id: (float(point["x"]), float(point["y"]))
        for node_id, point in optimized.items()
    }
    spatial = SpatialHash(cell_size=max(128.0, config.node_gap * 4))
    for node_id, point in optimized.items():
        spatial.insert(node_id, rect_for(point))

    deadline = time.monotonic() + config.time_budget_ms / 1000.0
    lane_neighbors = {}
    fixed_by_y = defaultdict(list)
    for node_id in fixed_y_ids:
        if node_id in optimized:
            fixed_by_y[round(float(optimized[node_id]["y"]), 6)].append(node_id)
    for lane_ids in fixed_by_y.values():
        ordered = sorted(lane_ids, key=lambda item: (preferred[item][0], item))
        for index, node_id in enumerate(ordered):
            lane_neighbors[node_id] = (
                ordered[index - 1] if index else None,
                ordered[index + 1] if index + 1 < len(ordered) else None,
            )

    def preserves_lane_order(node_id: str, candidate_x: float) -> bool:
        if node_id not in lane_neighbors:
            return True
        left_id, right_id = lane_neighbors[node_id]
        half_width = float(optimized[node_id].get("width", 120.0)) / 2
        if left_id:
            left = optimized[left_id]
            minimum = (
                float(left["x"])
                + float(left.get("width", 120.0)) / 2
                + config.node_gap
                + half_width
            )
            if candidate_x < minimum:
                return False
        if right_id:
            right = optimized[right_id]
            maximum = (
                float(right["x"])
                - float(right.get("width", 120.0)) / 2
                - config.node_gap
                - half_width
            )
            if candidate_x > maximum:
                return False
        return True

    movable = [
        node_id
        for node_id in optimized
        if node_id not in pinned_ids
    ]
    for _ in range(config.max_iterations):
        changed = False
        for node_id in movable:
            if time.monotonic() >= deadline:
                return optimized
            point = optimized[node_id]
            current = (float(point["x"]), float(point["y"]))
            neighbor_points = [optimized[neighbor_id] for neighbor_id in sorted(neighbors[node_id])]
            barycenter = current
            if neighbor_points:
                barycenter = (
                    sum(float(item["x"]) for item in neighbor_points) / len(neighbor_points),
                    sum(float(item["y"]) for item in neighbor_points) / len(neighbor_points),
                )

            candidate_xs = {
                current[0],
                preferred[node_id][0],
                barycenter[0],
                round((preferred[node_id][0] + barycenter[0]) / 2, 3),
            }
            candidate_ys = {current[1]} if node_id in fixed_y_ids else {
                current[1],
                preferred[node_id][1],
                barycenter[1],
                round((preferred[node_id][1] + barycenter[1]) / 2, 3),
            }
            probe = rect_for(point)
            for _, blocker in spatial.query((
                probe[0] - 400,
                probe[1] - 400,
                probe[2] + 400,
                probe[3] + 400,
            )):
                half_width = float(point.get("width", 120.0)) / 2
                half_height = float(point.get("height", 44.0)) / 2
                candidate_xs.add(round(blocker[0] - config.node_gap - half_width, 3))
                candidate_xs.add(round(blocker[2] + config.node_gap + half_width, 3))
                if node_id not in fixed_y_ids:
                    candidate_ys.add(round(blocker[1] - config.node_gap - half_height, 3))
                    candidate_ys.add(round(blocker[3] + config.node_gap + half_height, 3))

            spatial.remove(node_id)
            best = current
            best_score = _node_score(node_id, *current, optimized, neighbors, preferred)
            for candidate_x in sorted(candidate_xs):
                for candidate_y in sorted(candidate_ys):
                    if not preserves_lane_order(node_id, candidate_x):
                        continue
                    candidate_point = {**point, "x": candidate_x, "y": candidate_y}
                    candidate_rect = rect_for(candidate_point)
                    if spatial.overlaps(node_id, candidate_rect, config.node_gap):
                        continue
                    score = _node_score(
                        node_id,
                        candidate_x,
                        candidate_y,
                        optimized,
                        neighbors,
                        preferred,
                    )
                    if score < best_score:
                        best = (candidate_x, candidate_y)
                        best_score = score
            point["x"], point["y"] = round(best[0], 3), round(best[1], 3)
            optimized[node_id] = point
            spatial.insert(node_id, rect_for(point))
            changed = changed or best != current
        if not changed:
            break
    return optimized
