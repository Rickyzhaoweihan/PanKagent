"""Typed path roles for bounded simple graphs, independent of biological names.

Metapaths describe observed walks, never inferred relationships. Geometry uses
one preferred role per entity; all other paths and edges remain in the model.
"""
from collections import defaultdict, deque
from dataclasses import dataclass
import math
import time

from .engine import LayoutResult
from .metrics import compute_metrics
from .router import route_edges


@dataclass(frozen=True, order=True)
class MetapathStep:
    source_type: str
    relationship_type: str
    recorded_direction: str
    target_type: str


@dataclass(frozen=True, order=True)
class Metapath:
    root_type: str
    steps: tuple

    def as_dict(self):
        return {"path": [self.root_type, *(step.target_type for step in self.steps)],
                "steps": [vars(step) for step in self.steps]}


def infer_metapaths(graph, focus):
    """Enumerate shortest paths plus one-hop detours, with explicit work caps.

    A common two-hop role can outweigh a lone shortcut. Longer routes receive a
    length penalty so cycles cannot repeatedly push entities into deeper ranks.
    Unsupported, disconnected or over-budget graphs return None for fallback.
    """
    from .metapath import _kind, _order
    if not isinstance(graph, dict):
        return None
    raw, edges = graph.get("nodes"), graph.get("edges")
    if not isinstance(raw, list) or not isinstance(edges, list) or not 2 <= len(raw) <= 25 or not 1 <= len(edges) <= min(80, 3 * len(raw)):
        return None
    nodes, kinds, adjacency, eids = {}, {}, defaultdict(list), set()
    for node in raw:
        if not isinstance(node, dict):
            return None
        nid, kind = node.get("~id"), _kind(node)
        if not isinstance(nid, str) or not nid or nid in nodes or kind is None:
            return None
        nodes[nid], kinds[nid] = node, kind
    for edge in edges:
        if not isinstance(edge, dict):
            return None
        eid, a, b, rel = (edge.get(k) for k in ("~id", "~start", "~end", "~type"))
        if not all(isinstance(v, str) and v for v in (eid, a, b, rel)) or eid in eids or eid in nodes or a not in nodes or b not in nodes or a == b:
            return None
        eids.add(eid)
        adjacency[a].append((b, edge, "forward"))
        adjacency[b].append((a, edge, "reverse"))
    for nid in adjacency:
        adjacency[nid].sort(key=lambda item: (_order(nodes[item[0]]), item[1]["~id"]))
    selected = sorted(set(focus or ()) & nodes.keys())
    # Multiple core entities are valid search context. Select a deterministic
    # geometry anchor without asserting that they share a biological focus.
    root = min(selected or nodes, key=lambda nid: (-len({other for other, _, _ in adjacency[nid]}), _order(nodes[nid])))
    distances, queue = {root: 0}, deque([root])
    while queue:
        a = queue.popleft()
        for b, _, _ in adjacency[a]:
            if b not in distances:
                distances[b] = distances[a] + 1
                queue.append(b)
    if len(distances) != len(nodes) or max(distances.values()) > 6:
        return None
    # Count distinct node-path instances, not the number of parallel records.
    instances, node_paths = defaultdict(set), defaultdict(set)
    queue = deque([(root, (root,), ())])
    work = 0
    while queue:
        a, walk, steps = queue.popleft()
        for b, edge, direction in adjacency[a]:
            work += 1
            if work > 8192:
                return None
            if b in walk or len(steps) + 1 > min(6, distances[b] + 1):
                continue
            path = Metapath(kinds[root], steps + (MetapathStep(kinds[a], edge["~type"], direction, kinds[b]),))
            extended = walk + (b,)
            instances[path].add(extended)
            node_paths[b].add(path)
            queue.append((b, extended, path.steps))
    def priority(path):
        support = len({walk[1] for walk in instances[path]})
        return (-support / len(path.steps), -support, len(path.steps), path)
    roles = {nid: min(paths, key=priority) for nid, paths in node_paths.items()}
    return {"nodes": nodes, "edges": sorted(edges, key=lambda edge: edge["~id"]),
            "root": root, "roles": roles, "instances": instances,
            "distances": distances, "focus_ids": selected}



def _layered_branches(coords, edges, routes, ranks):
    """Use horizontal ports for layered branches even in very tall columns."""
    from collections import Counter
    from .geometry import rect_for, segment_intersects_rect
    degrees = Counter(nid for edge in edges for nid in (edge["~start"], edge["~end"]))
    pairs = Counter(tuple(sorted((edge["~start"], edge["~end"]))) for edge in edges)
    for edge in edges:
        start, end = edge["~start"], edge["~end"]
        if abs(ranks[start] - ranks[end]) != 1 or pairs[tuple(sorted((start, end)))] > 1:
            continue
        left, right = (start, end) if coords[start]["x"] < coords[end]["x"] else (end, start)
        a, b = coords[left], coords[right]
        source = [a["x"] + a["width"] / 2, a["y"]]
        target = [b["x"] - b["width"] / 2, b["y"]]
        if source[1] == target[1]:
            points = [source, target]
        else:
            # Reserve most of the inter-column corridor on the distinct branch.
            mid = source[0] + 24 if degrees[left] > degrees[right] else target[0] - 24
            points = [source, [mid, source[1]], [mid, target[1]], target]
        obstacles = [rect_for(point) for nid, point in coords.items() if nid not in (left, right)]
        if any(segment_intersects_rect((u, v), box, allow_boundary=True) for u, v in zip(points, points[1:]) for box in obstacles):
            continue
        if start != left:
            points.reverse()
        routes[edge["~id"]] = {"route_type": "polyline", "source_port": points[0],
                               "target_port": points[-1], "waypoints": points[1:-1]}


def _renderer_labels(coords, edges, routes, config):
    """Put polyline labels at Cytoscape's actual middle waypoint.

    Route metadata cannot hide labels in the retained viewer. A crowded center
    therefore receives an exterior shortcut lane rather than a hidden label.
    Collinear subdivisions preserve the route while balancing waypoint counts.
    """
    from .geometry import rect_for, rects_overlap, route_segments, segment_intersects_rect
    from .router import _label_rect
    obstacles = [rect_for(point) for point in coords.values()]
    labels = []
    top = min(rect[1] for rect in obstacles) - 48.0
    for edge in edges:
        route = routes[edge["~id"]]
        text = edge["~type"].replace("_", " ")
        anchor = route.get("label_anchor")
        other_segments = [segment for eid, other in routes.items() if eid != edge["~id"] for segment in route_segments(other)]
        def clear_label(box):
            return (not any(rects_overlap(box, obstacle) for obstacle in [*obstacles, *labels])
                    and not any(segment_intersects_rect(segment, box, allow_boundary=True) for segment in other_segments))
        if route["route_type"] == "bezier" and route.get("label_visible") and clear_label(_label_rect(anchor, text, config)):
            labels.append(_label_rect(anchor, text, config))
            continue
        points = [route["source_port"], *route.get("waypoints", []), route["target_port"]]
        choices = []
        for i, (a, b) in enumerate(zip(points, points[1:])):
            if abs(a[1] - b[1]) > 1e-6:
                continue
            for t in (.5, .35, .65):
                point = [round(a[0] + (b[0] - a[0]) * t, 3), a[1]]
                box = _label_rect(point, text, config)
                if clear_label(box):
                    choices.append((-(abs(b[0] - a[0])), abs(t - .5), i, point))
        if choices:
            _, _, index, anchor = min(choices)
        else:
            a, b = coords[edge["~start"]], coords[edge["~end"]]
            side = 1 if b["x"] > a["x"] else -1
            source_side, target_side = side, -side
            # Escape through gaps between columns before moving vertically;
            # climbing at a node center would hit its same-column siblings.
            source = [a["x"] + source_side * a["width"] / 2, a["y"]]
            target = [b["x"] + target_side * b["width"] / 2, b["y"]]
            aw = max(p["width"] for p in coords.values() if p["x"] == a["x"])
            bw = max(p["width"] for p in coords.values() if p["x"] == b["x"])
            ax = a["x"] + source_side * (aw / 2 + 24)
            bx = b["x"] + target_side * (bw / 2 + 24)
            points = [source, [ax, source[1]], [ax, top], [bx, top], [bx, target[1]], target]
            index, anchor = 2, [round((ax + bx) / 2, 3), top]
            route["source_port"], route["target_port"] = source, target
            top -= 40.0
        # Interior points before and after the desired middle waypoint.
        before, after = points[1:index+1], points[index+1:-1]
        def subdivisions(a, b, count):
            return [[round(a[0] + (b[0]-a[0])*i/(count+1), 3),
                     round(a[1] + (b[1]-a[1])*i/(count+1), 3)] for i in range(1, count+1)]
        if len(before) < len(after):
            before = [*before, *subdivisions(points[index], anchor, len(after)-len(before))]
        elif len(after) < len(before):
            after = [*subdivisions(anchor, points[index+1], len(before)-len(after)), *after]
        route.update(route_type="polyline", waypoints=[*before, anchor, *after],
                     label_anchor=anchor, label_visible=True, label_status="full",
                     label_placement="segment_waypoint_midpoint", endpoint_mode="explicit_ports")
        route.pop("control_points", None)
        labels.append(_label_rect(anchor, text, config))
    return True


def path_layout(graph, focus, dimensions, config):
    started = time.monotonic()
    model = infer_metapaths(graph, focus)
    if model is None or not isinstance(dimensions, dict) or set(dimensions) != set(model["nodes"]):
        return None
    for point in dimensions.values():
        if not isinstance(point, dict) or any(isinstance(point.get(k), bool) or not isinstance(point.get(k), (int, float)) or not math.isfinite(point[k]) or point[k] <= 0 for k in ("width", "height")):
            return None
    from .metapath import _order
    nodes, edges, root, roles = (model[k] for k in ("nodes", "edges", "root", "roles"))
    ranks = {root: 0, **{nid: len(role.steps) for nid, role in roles.items()}}
    columns = defaultdict(list)
    for nid in nodes:
        columns[ranks[nid]].append(nid)
    for rank in columns:
        columns[rank].sort(key=lambda nid: (roles.get(nid, Metapath("", ())), _order(nodes[nid])))
    neighbors = defaultdict(set)
    for edge in edges:
        a, b = edge["~start"], edge["~end"]
        neighbors[a].add(b); neighbors[b].add(a)
    # Barycentric sweeps within each role reduce crossings without intermixing
    # semantic groups or depending on the order of input records.
    for sweep in range(4):
        order = {nid: i for ids in columns.values() for i, nid in enumerate(ids)}
        for rank in sorted(columns, reverse=bool(sweep % 2)):
            def key(nid):
                prior = [order[b] for b in neighbors[nid] if ranks[b] != rank]
                return (roles.get(nid, Metapath("", ())), sum(prior) / len(prior) if prior else order[nid], _order(nodes[nid]))
            columns[rank].sort(key=key)
    label_width = max(min(220, len(edge["~type"].replace("_", " ")) * 5.8 + 8) for edge in edges)
    gap = max(config.layer_spacing, label_width + 2 * config.label_clearance + 40)
    coords, x = {}, 0.0
    for rank, ids in sorted(columns.items()):
        width = max(dimensions[nid]["width"] for nid in ids)
        heights = [dimensions[nid]["height"] for nid in ids]
        row_gap = max(config.node_gap, 46.0)
        total = sum(heights) + row_gap * (len(ids) - 1)
        y = -total / 2
        for nid, height in zip(ids, heights):
            coords[nid] = {**dimensions[nid], "x": round(x + width / 2, 3), "y": round(y + height / 2, 3)}
            y += height + row_gap
        x += width + gap
    offset = coords[root]["x"]
    for point in coords.values():
        point["x"] = round(point["x"] - offset, 3)
    from .cycle_layout import cycle_geometry
    cycle = cycle_geometry(model, dimensions, config)
    geometry = {"geometry_strategy": "layered_roles"}
    if cycle is not None:
        coords, routes, geometry = cycle
    else:
        routes = route_edges(coords, edges, config)
        _layered_branches(coords, edges, routes, ranks)
        if not _renderer_labels(coords, edges, routes, config):
            return None
    metrics = compute_metrics(coords, edges, routes)
    if len(routes) != len(edges) or metrics["node_overlap_count"] or metrics["edge_node_intersection_count"]:
        return None
    paths = []
    for path, walks in sorted(model["instances"].items()):
        paths.append({**path.as_dict(), "instance_count": len(walks),
                      "node_ids": sorted({walk[-1] for walk in walks}),
                      "preferred_node_ids": sorted(nid for nid, role in roles.items() if role == path),
                      "scope": "displayed_graph"})
    metrics["runtime_ms"] = round((time.monotonic() - started) * 1000, 3)
    return LayoutResult(coords, routes, {"engine": "metapath_layered", "version": 1,
        "config_fingerprint": config.fingerprint("metapath_geometry_v2"),
        "previous_layout_status": "not_provided", "track_policy": "none",
        "metapath_details": {"root_id": root, "scope": "displayed_graph",
                             "strategy": "supported_path_roles", **geometry, "metapaths": paths,
                             "focus_ids": model["focus_ids"], "root_role": "geometry_anchor",
                             "node_ranks": dict(sorted(ranks.items())),
                             "cross_edge_ids": [edge["~id"] for edge in edges if abs(ranks[edge["~start"]] - ranks[edge["~end"]]) != 1],
                             "path_limit": 6, "work_limit": 8192}, "metrics": metrics})
