"""Deterministic one-focus, relationship-grouped mind-map coordinates."""

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple


def graph_node_ids(graph: Dict[str, Any]) -> set[str]:
    return {
        str(node.get("~id"))
        for node in graph.get("nodes", [])
        if node.get("~id") is not None
    }


def infer_focus_node(
    graph_batches: List[Dict[str, Any]],
    requested_core_nodes: Any = None,
    requested_focus_node: Optional[str] = None,
) -> Optional[str]:
    if requested_focus_node:
        return requested_focus_node

    if isinstance(requested_core_nodes, list):
        normalized = [str(node_id) for node_id in requested_core_nodes if node_id is not None]
        if len(set(normalized)) == 1:
            return normalized[0]

    nonempty_node_sets = [graph_node_ids(batch) for batch in graph_batches if graph_node_ids(batch)]
    if len(nonempty_node_sets) > 1:
        shared = set.intersection(*nonempty_node_sets)
        if len(shared) == 1:
            return next(iter(shared))

    if len(nonempty_node_sets) != 1:
        return None
    graph = next(batch for batch in graph_batches if graph_node_ids(batch))
    node_ids = nonempty_node_sets[0]
    if len(node_ids) < 3:
        return None
    degree = {node_id: 0 for node_id in node_ids}
    for edge in graph.get("edges", []):
        start = str(edge.get("~start", ""))
        end = str(edge.get("~end", ""))
        if start in degree and end in degree and start != end:
            degree[start] += 1
            degree[end] += 1
    star_centers = sorted(node_id for node_id, value in degree.items() if value == len(node_ids) - 1)
    return star_centers[0] if len(star_centers) == 1 else None


def center_spread_distances(
    graph: Dict[str, Any],
    focus_node: str,
) -> Tuple[Dict[str, int], Dict[str, str], Dict[str, str]]:
    node_ids = graph_node_ids(graph)
    neighbor_edge_types = {node_id: defaultdict(set) for node_id in node_ids}
    for edge in graph.get("edges", []):
        start = str(edge.get("~start", ""))
        end = str(edge.get("~end", ""))
        if start not in neighbor_edge_types or end not in neighbor_edge_types or start == end:
            continue
        edge_type = str(edge.get("~type") or "RELATED_TO")
        neighbor_edge_types[start][end].add(edge_type)
        neighbor_edge_types[end][start].add(edge_type)
    adjacency = {
        node_id: sorted(
            (neighbor_id, " + ".join(sorted(edge_types)))
            for neighbor_id, edge_types in neighbors.items()
        )
        for node_id, neighbors in neighbor_edge_types.items()
    }

    distances = {focus_node: 0}
    parents = {}
    branches = {}
    queue = [focus_node]
    for node_id in queue:
        for neighbor_id, edge_type in adjacency.get(node_id, []):
            if neighbor_id in distances:
                continue
            distances[neighbor_id] = distances[node_id] + 1
            parents[neighbor_id] = node_id
            branches[neighbor_id] = edge_type if node_id == focus_node else branches[node_id]
            queue.append(neighbor_id)
    return distances, parents, branches


def is_center_spread_candidate(
    graph: Dict[str, Any],
    focus_node: Optional[str],
    max_depth: int = 2,
    minimum_branches: int = 2,
) -> bool:
    node_ids = graph_node_ids(graph)
    if not focus_node or focus_node not in node_ids or len(node_ids) < 3:
        return False
    distances, _parents, _branches = center_spread_distances(graph, focus_node)
    direct_branches = {
        _branches[node_id]
        for node_id, depth in distances.items()
        if depth == 1 and node_id in _branches
    }
    return (
        len(distances) == len(node_ids)
        and max(distances.values(), default=0) <= max_depth
        and len(direct_branches) >= minimum_branches
    )


def center_spread_layout(
    graph: Dict[str, Any],
    focus_node: str,
    initial_coords: Dict[str, Dict[str, Any]],
    *,
    column_gap: float = 300.0,
    node_gap: float = 34.0,
    branch_gap: float = 70.0,
    collision_gap: float = 16.0,
) -> Optional[Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]]:
    coords = {str(node_id): dict(point) for node_id, point in initial_coords.items()}
    if focus_node not in coords:
        return None
    distances, parents, branch_by_node = center_spread_distances(graph, focus_node)
    if len(distances) != len(coords):
        return None

    coords[focus_node]["x"] = 0.0
    coords[focus_node]["y"] = 0.0
    coords[focus_node]["Level"] = "Core"
    max_node_width = max(float(point.get("width", 120.0)) for point in coords.values())
    resolved_column_gap = max(
        column_gap,
        float(coords[focus_node].get("width", 120.0)) / 2
        + max_node_width / 2
        + collision_gap
        + 24,
    )

    branch_nodes = defaultdict(list)
    for node_id, branch_name in branch_by_node.items():
        coords[node_id]["Level"] = "Neighbor"
        branch_nodes[branch_name].append(node_id)

    side_branches = {"right": [], "left": []}
    side_load = {"right": 0, "left": 0}
    for branch_name in sorted(branch_nodes):
        nodes = branch_nodes[branch_name]
        side = min(("right", "left"), key=lambda item: (side_load[item], item != "right"))
        side_branches[side].append(branch_name)
        side_load[side] += len(nodes)

    branch_metadata = []
    for side in ("right", "left"):
        estimates = []
        for branch_name in side_branches[side]:
            nodes = branch_nodes[branch_name]
            max_depth_count = max(
                sum(1 for node_id in nodes if distances[node_id] == depth)
                for depth in {distances[node_id] for node_id in nodes}
            )
            max_height = max(float(coords[node_id].get("height", 44.0)) for node_id in nodes)
            estimates.append(max_height * max_depth_count + node_gap * max(0, max_depth_count - 1))
        total_height = sum(estimates) + branch_gap * max(0, len(estimates) - 1)
        cursor_y = -total_height / 2
        direction = 1 if side == "right" else -1

        for branch_name, branch_height in zip(side_branches[side], estimates):
            branch_center_y = cursor_y + branch_height / 2
            nodes = branch_nodes[branch_name]
            for depth in sorted({distances[node_id] for node_id in nodes}):
                depth_nodes = sorted(
                    (node_id for node_id in nodes if distances[node_id] == depth),
                    key=lambda node_id: (parents.get(node_id, ""), node_id),
                )
                heights = [float(coords[node_id].get("height", 44.0)) for node_id in depth_nodes]
                column_height = sum(heights) + node_gap * max(0, len(depth_nodes) - 1)
                y = branch_center_y - column_height / 2
                for node_id, height in zip(depth_nodes, heights):
                    coords[node_id]["x"] = round(direction * resolved_column_gap * depth, 3)
                    coords[node_id]["y"] = round(y + height / 2, 3)
                    y += height + node_gap
            branch_metadata.append(
                {
                    "relationship_type": branch_name,
                    "side": side,
                    "node_count": len(nodes),
                    "max_depth": max(distances[node_id] for node_id in nodes),
                }
            )
            cursor_y += branch_height + branch_gap

    return coords, branch_metadata
