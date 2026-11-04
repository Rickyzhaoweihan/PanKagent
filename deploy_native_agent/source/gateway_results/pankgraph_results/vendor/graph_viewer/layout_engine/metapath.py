"""Bounded, evidence-preserving mind-map sections for simple typed graphs.

Only presentation is returned. A type tree supplies the rooted hierarchy;
maximal type/relationship/direction paths define visually separate sections.
Unsupported or ambiguous instances retain the caller's network layout.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, Optional

from .config import LayoutConfig
from .engine import LayoutResult
from .metrics import compute_metrics


TEMPLATE = "metapath_sections_v1"
ROW_GAP = 26.0
NODE_HEIGHT = 16.0
LEVEL_GAP = 58.0
PANEL_GAP = 28.0
SECTION_GAP = 56.0
BRANCH_INSET = 42.0
MAX_SECTIONS = 8
_GENERIC = {"ontology", "coding element", "coding elements"}
# Inherited labels in the recorded variant lineage describe one entity, not
# mutually incompatible semantic kinds. Do not collapse unrelated labels.
_TYPE_PARENTS = {"snv": "sequence variant", "sequence snp": "sequence variant",
                 "insertion": "sequence variant", "deletion": "sequence variant",
                 "indel": "sequence variant", "sequence variant": "variants"}


def _text(value):
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _kind(node):
    labels = node.get("~labels", [])
    if not isinstance(labels, list) or not all(isinstance(item, str) and item.strip() for item in labels):
        return None
    semantic = {" ".join(item.casefold().replace("_", " ").split()) for item in labels}
    specific = semantic - _GENERIC
    for label in tuple(specific):
        parent = _TYPE_PARENTS.get(label)
        while parent:
            specific.discard(parent)
            parent = _TYPE_PARENTS.get(parent)
    # Generic projection aliases must not merge distinct biological types or
    # conceal nodes having two incompatible semantic labels.
    semantic = specific
    return next(iter(semantic)) if len(semantic) == 1 else None


def _order(node):
    props = node.get("~properties", {})
    props = props if isinstance(props, dict) else {}
    return ((_text(props.get("id")) or node["~id"]).casefold(), node["~id"])


def _parse(graph):
    if not isinstance(graph, dict):
        return None
    raw_nodes, edges = graph.get("nodes"), graph.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(edges, list) or not 4 <= len(raw_nodes) <= 100:
        return None
    nodes, kinds = {}, {}
    for node in raw_nodes:
        if not isinstance(node, dict) or not isinstance(node.get("~id"), str) or not node["~id"] or node["~id"] in nodes:
            return None
        kind = _kind(node)
        if not kind:
            return None
        nodes[node["~id"]], kinds[node["~id"]] = node, kind
    types = set(kinds.values())
    if not 2 <= len(types) < 5:
        return None
    adjacency, type_adjacency = defaultdict(list), defaultdict(set)
    seen_ids, seen_pairs = set(), set()
    for edge in edges:
        if not isinstance(edge, dict):
            return None
        eid, start, end, relation = (edge.get(key) for key in ("~id", "~start", "~end", "~type"))
        if not all(isinstance(item, str) and item for item in (eid, start, end, relation)):
            return None
        pair = tuple(sorted((start, end)))
        if eid in seen_ids or eid in nodes or pair in seen_pairs or start == end or start not in nodes or end not in nodes:
            return None
        if kinds[start] == kinds[end]:
            return None
        seen_ids.add(eid)
        seen_pairs.add(pair)
        adjacency[start].append((end, edge))
        adjacency[end].append((start, edge))
        type_adjacency[kinds[start]].add(kinds[end])
        type_adjacency[kinds[end]].add(kinds[start])
    # An undirected semantic type tree is deliberately narrower than arbitrary
    # small-type graphs: no cyclic type walks, same-type edges, or parallel pairs.
    if sum(map(len, type_adjacency.values())) != 2 * (len(types) - 1):
        return None
    return nodes, edges, kinds, adjacency, type_adjacency


def _rooted(parsed, root):
    nodes, edges, kinds, adjacency, type_adjacency = parsed
    root_type = kinds[root]
    levels, queue = {root_type: 0}, deque([root_type])
    while queue:
        kind = queue.popleft()
        for other in sorted(type_adjacency[kind]):
            if other not in levels:
                levels[other] = levels[kind] + 1
                queue.append(other)
    if len(levels) != len(set(kinds.values())) or max(levels.values()) < 1:
        return None
    parents, children, oriented = defaultdict(list), defaultdict(list), {}
    for edge in edges:
        a, b = edge["~start"], edge["~end"]
        parent, child = (a, b) if levels[kinds[a]] < levels[kinds[b]] else (b, a)
        parents[child].append(parent)
        children[parent].append(child)
        oriented[edge["~id"]] = (parent, child)
    for nid in nodes:
        if nid != root and not parents[nid]:
            return None
        # Multiple parents are safe only for terminal entities. Those entities
        # receive one shared anchor, never copied into each donor branch.
        if len(parents[nid]) > 1 and children[nid]:
            return None
    ordered = sorted(nodes, key=lambda nid: (levels[kinds[nid]], _order(nodes[nid])))
    prefixes = {root: ()}
    incoming = defaultdict(list)
    for edge in edges:
        parent, child = oriented[edge["~id"]]
        incoming[child].append((parent, edge))
    for nid in ordered:
        if nid == root:
            continue
        candidates = set()
        for parent, edge in incoming[nid]:
            if parent not in prefixes:
                return None
            direction = "forward" if edge["~start"] == parent else "reverse"
            candidates.add(prefixes[parent] + ((edge["~type"], direction, kinds[nid]),))
        if len(candidates) != 1:
            return None
        prefixes[nid] = candidates.pop()
    signatures = {}
    for nid in reversed(ordered):
        signatures[nid] = set().union(*(signatures[child] for child in children[nid])) if children[nid] else {prefixes[nid]}
        # A shared root is expected. Any other shared internal prefix would
        # need duplication or a new cross-section bridge; keep network layout.
        if nid != root and len(signatures[nid]) != 1:
            return None
    if not 1 <= len(signatures[root]) <= MAX_SECTIONS:
        return None
    sections = []
    for signature in sorted(signatures[root], key=lambda path: (len(path), path)):
        members = {nid for nid in nodes if nid != root and signatures[nid] == {signature}}
        roots = sorted((nid for nid in children[root] if nid in members), key=lambda nid: _order(nodes[nid]))
        shared = sorted((nid for nid in members if len(parents[nid]) > 1), key=lambda nid: _order(nodes[nid]))
        if any(sum(child in shared for child in children[nid]) > 1 for nid in members):
            return None  # A terminal mesh needs separated ports/labels.
        tree_children = {nid: sorted((child for child in children[nid] if child not in shared), key=lambda child: _order(nodes[child])) for nid in members}
        weights = {}
        for nid in reversed(ordered):
            if nid in members and nid not in shared:
                weights[nid] = sum(weights[child] for child in tree_children[nid]) or 1
        sections.append({"signature": signature, "members": members, "roots": roots,
                         "shared": shared, "children": tree_children, "weights": weights})
    return {"nodes": nodes, "edges": edges, "kinds": kinds, "root": root,
            "levels": levels, "parents": parents, "oriented": oriented, "sections": sections}


def _detect(graph, focus):
    parsed = _parse(graph)
    if parsed is None:
        return None
    nodes, edges, kinds, adjacency, _ = parsed
    by_type = defaultdict(list)
    for nid in nodes:
        by_type[kinds[nid]].append(nid)
    candidates = [ids[0] for ids in by_type.values() if len(ids) == 1]
    candidates.sort(key=lambda nid: (len(adjacency[nid]) != len(nodes) - 1, nid not in focus, -len(adjacency[nid]), _order(nodes[nid])))
    for root in candidates:
        detected = _rooted(parsed, root)
        if detected is not None:
            return detected
    return None


def _split_weighted(items, count, weights):
    panels, remaining = [], list(items)
    for left in range(count, 0, -1):
        if left == 1:
            panels.append(remaining)
            break
        target = sum(weights[item] for item in remaining) / left
        size, used = 0, 0
        while size < len(remaining) - left + 1:
            weight = weights[remaining[size]]
            if size and abs(used - target) <= abs(used + weight - target):
                break
            size, used = size + 1, used + weight
        panels.append(remaining[:max(1, size)])
        remaining = remaining[max(1, size):]
    return panels


def _section_plan(section, count, widths, levels, kinds, spacing):
    panels = _split_weighted(section["roots"], count, section["weights"])
    columns = defaultdict(float)
    for nid in section["members"] - set(section["shared"]):
        level = levels[kinds[nid]]
        columns[level] = max(columns[level], widths[nid])
    # A root-child subtree is never split across wrapped panels.
    panel_width = (sum(columns.values()) + (len(columns) - 1) * spacing["level_gap"]
                   + spacing["branch_inset"] + spacing["panel_tail"])
    rows = max(sum(section["weights"][nid] for nid in panel) for panel in panels)
    shared_count = len(section["shared"])
    height = max(rows, shared_count, 1) * ROW_GAP + 56.0
    width = count * panel_width + (count - 1) * PANEL_GAP
    if shared_count:
        width += spacing["level_gap"] + max(widths[nid] for nid in section["shared"])
    return {"panels": panels, "columns": dict(columns), "panel_width": panel_width,
            "rows": rows, "width": width, "height": height}


def _layout_score(plans, split, root_width):
    left, right = plans[:split], plans[split:]
    side_height = lambda items: sum(item["height"] for item in items) + SECTION_GAP * max(0, len(items) - 1)
    width = root_width + 120.0 + max([item["width"] for item in left] or [0]) + max([item["width"] for item in right] or [0])
    height = max(NODE_HEIGHT, side_height(left), side_height(right))
    # Normalize spacing to the largest incoming node height.
    # Optimize fitted readability in a typical 4:3 viewer, with deterministic
    # low-panel/low-area tie breakers.
    fit = min(600.0 / width, 450.0 / height)
    return (-fit, width * height, sum(len(item["panels"]) for item in plans), split)


def _choose_plans(sections, widths, levels, kinds, root_width, spacing):
    alternatives = [[_section_plan(section, count, widths, levels, kinds, spacing)
                     for count in range(1, min(6, len(section["roots"])) + 1)] for section in sections]
    best = None
    # All shorter paths stay to the left of all deeper paths. Try the possible
    # cut points so unequal section cardinalities may have unequal areas.
    for split in (range(1, len(sections)) if len(sections) > 1 else [0]):
        chosen = [0] * len(sections)
        for _ in range(3):
            changed = False
            for index, options in enumerate(alternatives):
                choices = []
                for option in range(len(options)):
                    trial = chosen[:]
                    trial[index] = option
                    plans = [alternatives[i][value] for i, value in enumerate(trial)]
                    choices.append((_layout_score(plans, split, root_width), option))
                value = min(choices)[1]
                changed = changed or value != chosen[index]
                chosen[index] = value
            if not changed:
                break
        plans = [alternatives[i][value] for i, value in enumerate(chosen)]
        candidate = (_layout_score(plans, split, root_width), split, plans)
        if best is None or candidate[0] < best[0]:
            best = candidate
    return best[1], best[2]


def _polyline(points):
    output = []
    for point in points:
        point = [round(float(value), 3) for value in point]
        if output and point == output[-1]:
            continue
        if len(output) > 1:
            a, b = output[-2:]
            collinear = abs((b[0] - a[0]) * (point[1] - b[1]) - (b[1] - a[1]) * (point[0] - b[0])) < 1e-7
            between = min(a[0], point[0]) <= b[0] <= max(a[0], point[0]) and min(a[1], point[1]) <= b[1] <= max(a[1], point[1])
            if collinear and between:
                output[-1] = point
                continue
        output.append(point)
    return output


def _bounds(coords, ids):
    return {"left": round(min(coords[nid]["x"] - coords[nid]["width"] / 2 for nid in ids), 3),
            "right": round(max(coords[nid]["x"] + coords[nid]["width"] / 2 for nid in ids), 3),
            "top": round(min(coords[nid]["y"] - coords[nid]["height"] / 2 for nid in ids), 3),
            "bottom": round(max(coords[nid]["y"] + coords[nid]["height"] / 2 for nid in ids), 3)}


def _midpoint_label_route(points, at_start, source, target, label_size, padding):
    """Place the unchanged renderer's center label on a distinct branch.

    Cytoscape 3.30's segment midpoint is its middle waypoint (not arc length).
    Balanced collinear subdivisions make the chosen branch point that midpoint
    without changing any line segment or requiring endpoint-label bypasses.
    Empty-label and data-mapper bypasses are not supported by that renderer.
    """
    path = points if at_start else list(reversed(points))
    box = source if at_start else target
    other = target if at_start else source
    a, b = path[:2]
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = abs(dx) + abs(dy)
    if length <= 1e-6:
        return None
    ux, uy = dx / length, dy / length
    half = box["width"] / 2 if abs(dx) > 1e-6 else box["height"] / 2
    label_half = label_size[0 if abs(dx) > 1e-6 else 1] / 2
    low, high = half + label_half + padding, length - label_half - padding
    if len(path) == 2:
        high -= other["width"] / 2 if abs(dx) > 1e-6 else other["height"] / 2
    if high < low:
        return None
    distance = (low + high) / 2
    anchor = [round(a[0] + ux * distance, 3), round(a[1] + uy * distance, 3)]
    original = path[1:-1]
    subdivisions = [[round(a[0] + ux * (low + (distance - low) * i / (len(original) + 1)), 3),
                     round(a[1] + uy * (low + (distance - low) * i / (len(original) + 1)), 3)]
                    for i in range(1, len(original) + 1)]
    waypoints = [*subdivisions, anchor, *original]
    if not at_start:
        waypoints.reverse()
    return waypoints, anchor


def _port(box, towards):
    """Intersect the endpoint ray with the exact incoming node rectangle."""
    dx, dy = towards[0] - box["x"], towards[1] - box["y"]
    factors = []
    if abs(dx) > 1e-9:
        factors.append(box["width"] / (2 * abs(dx)))
    if abs(dy) > 1e-9:
        factors.append(box["height"] / (2 * abs(dy)))
    if not factors:
        return None
    factor = min(factors)
    return [round(box["x"] + factor * dx, 3), round(box["y"] + factor * dy, 3)]


def _spacing_unit(dimensions, config):
    return max(1.0, max(point["height"] for point in dimensions.values()) / NODE_HEIGHT,
               config.node_gap / (ROW_GAP - NODE_HEIGHT), config.layer_spacing / LEVEL_GAP,
               config.edge_clearance / 8.0, config.label_clearance / 12.0)


def _tree_metapath_layout(
    graph: Dict[str, Any],
    core_nodes: Iterable[str],
    dimensions: Dict[str, Dict[str, Any]],
    config: LayoutConfig,
) -> Optional[LayoutResult]:
    """Return sectioned tree geometry, or None for unsupported graph structure.

    Sections describe paths in the *displayed* graph only. Missing displayed
    branches do not assert assay absence, sample absence, or cohort eligibility.
    Spacing is normalized to the largest supplied height; the returned rectangles
    retain the exact display dimensions and metadata supplied by the caller.
    """
    started = time.monotonic()
    focus = {item for item in (core_nodes or ()) if isinstance(item, str)}
    detected = _detect(graph, focus)
    if detected is None:
        return None
    nodes, edges, kinds, root = (detected[key] for key in ("nodes", "edges", "kinds", "root"))
    sections, levels = detected["sections"], detected["levels"]
    if not isinstance(dimensions, dict) or set(dimensions) != set(nodes):
        return None
    for point in dimensions.values():
        if not isinstance(point, dict) or any(
            not isinstance(point.get(key), (int, float)) or isinstance(point[key], bool)
            or not math.isfinite(point[key]) or point[key] <= 0 for key in ("width", "height")
        ):
            return None
    scale = _spacing_unit(dimensions, config)
    widths = {nid: dimensions[nid]["width"] / scale for nid in nodes}
    # Reserve an entire label rectangle on the distinct child/parent branch.
    # In particular, labels must not extend over their own endpoint boxes;
    # the generic metrics intentionally exclude those boxes from label checks.
    padding = max(4.0, config.label_padding) / scale
    label_sizes = {edge["~id"]: (max(18.0, min(220.0, len(edge["~type"].replace("_", " ").strip()) * 5.8 + 8.0)) / scale,
                                     18.0 / scale) for edge in edges}
    label_span = max(size[0] for size in label_sizes.values()) + 2 * padding
    spacing = {"level_gap": max(LEVEL_GAP, 2 * label_span + 4.0),
               "branch_inset": max(BRANCH_INSET, label_span + 4.0),
               "panel_tail": max(22.0, label_span + 6.0)}
    split, plans = _choose_plans(sections, widths, levels, kinds, widths[root], spacing)
    coords, node_section, node_panel, panel_geometry, section_geometry = {}, {}, {}, {}, {}

    def put(nid, x, y):
        coords[nid] = {**dimensions[nid], "x": x, "y": y, "width": widths[nid],
                       "height": dimensions[nid]["height"] / scale}

    put(root, 0.0, 0.0)
    inner = widths[root] / 2 + 60.0
    for side, indices in ((-1, list(range(split))), (1, list(range(split, len(sections))))):
        side_height = sum(plans[index]["height"] for index in indices) + SECTION_GAP * max(0, len(indices) - 1)
        top = -side_height / 2
        for index in indices:
            section, plan = sections[index], plans[index]
            section_top = top
            center_y = top + 56.0 + (plan["height"] - 56.0) / 2
            node_top = center_y - (plan["rows"] - 1) * ROW_GAP / 2 - NODE_HEIGHT / 2
            root_bus = node_top - 18.0
            anchor_bus = node_top - 38.0
            distance = inner
            for panel_index, branch_roots in enumerate(plan["panels"]):
                x_by_level, cursor = {}, distance + spacing["branch_inset"]
                for level, width in sorted(plan["columns"].items()):
                    x_by_level[level] = side * (cursor + width / 2)
                    cursor += width + spacing["level_gap"]
                panel_geometry[(index, panel_index)] = {"inner": side * distance,
                    "outer": side * (distance + plan["panel_width"] - 2.0), "root_bus": root_bus,
                    "anchor_bus": anchor_bus, "roots": branch_roots, "nodes": []}
                row_count = sum(section["weights"][nid] for nid in branch_roots)
                first_row = center_y - (row_count - 1) * ROW_GAP / 2

                def place_subtree(nid, row):
                    weight = section["weights"][nid]
                    put(nid, x_by_level[levels[kinds[nid]]], first_row + (row + (weight - 1) / 2) * ROW_GAP)
                    node_section[nid], node_panel[nid] = index, panel_index
                    panel_geometry[(index, panel_index)]["nodes"].append(nid)
                    cursor_row = row
                    for child in section["children"][nid]:
                        place_subtree(child, cursor_row)
                        cursor_row += section["weights"][child]

                row = 0
                for nid in branch_roots:
                    place_subtree(nid, row)
                    row += section["weights"][nid]
                distance += plan["panel_width"] + PANEL_GAP
            # Shared terminals have a dedicated outer column. Stable ordering
            # and enough reserved rows prevent duplicate/coincident anchors.
            shared = section["shared"]
            anchor_inner = side * (distance - PANEL_GAP + spacing["level_gap"] / 2)
            anchor_x = side * (distance - PANEL_GAP + spacing["level_gap"] + (max((widths[nid] for nid in shared), default=0)) / 2)
            desired = sorted(shared, key=lambda nid: ((min(coords[parent]["y"] for parent in detected["parents"][nid]) + max(coords[parent]["y"] for parent in detected["parents"][nid])) / 2, _order(nodes[nid])))
            anchor_ys = []
            for nid in desired:
                parents = detected["parents"][nid]
                y = (min(coords[parent]["y"] for parent in parents) + max(coords[parent]["y"] for parent in parents)) / 2
                anchor_ys.append(max(y, anchor_ys[-1] + ROW_GAP) if anchor_ys else y)
            if anchor_ys:
                shift = max(0.0, anchor_ys[-1] - (center_y + (max(plan["rows"], len(shared)) - 1) * ROW_GAP / 2))
                anchor_ys = [y - shift for y in anchor_ys]
            for nid, y in zip(desired, anchor_ys):
                put(nid, anchor_x, y)
                node_section[nid] = index
            section_geometry[index] = {"side": side, "root_bus": root_bus, "anchor_bus": anchor_bus,
                                       "anchor_inner": anchor_inner, "reserved_top": section_top,
                                       "reserved_bottom": section_top + plan["height"]}
            top += plan["height"] + SECTION_GAP

    routes = {}
    for edge in sorted(edges, key=lambda item: item["~id"]):
        parent, child = detected["oriented"][edge["~id"]]
        index = node_section[child]
        section, sg = sections[index], section_geometry[index]
        side = sg["side"]
        a, b = coords[parent], coords[child]
        if parent == root:
            pg = panel_geometry[(index, node_panel[child])]
            spine = side * (widths[root] / 2 + 26.0)
            if node_panel[child] == 0:
                points = [[a["x"], a["y"]], [spine, a["y"]], [spine, b["y"]], [b["x"], b["y"]]]
            else:
                points = [[a["x"], a["y"]], [spine, a["y"]], [spine, pg["root_bus"]],
                          [pg["inner"], pg["root_bus"]], [pg["inner"], b["y"]], [b["x"], b["y"]]]
        elif child in section["shared"]:
            pg = panel_geometry[(index, node_panel[parent])]
            if node_panel[parent] == len(plans[index]["panels"]) - 1:
                points = [[a["x"], a["y"]], [sg["anchor_inner"], a["y"]], [sg["anchor_inner"], b["y"]], [b["x"], b["y"]]]
            else:
                points = [[a["x"], a["y"]], [pg["outer"], a["y"]], [pg["outer"], sg["anchor_bus"]],
                          [sg["anchor_inner"], sg["anchor_bus"]], [sg["anchor_inner"], b["y"]], [b["x"], b["y"]]]
        else:
            parent_border = a["x"] + side * a["width"] / 2
            child_border = b["x"] - side * b["width"] / 2
            middle = (parent_border + child_border) / 2
            points = [[a["x"], a["y"]], [middle, a["y"]], [middle, b["y"]], [b["x"], b["y"]]]
        points = _polyline(points)
        forward = edge["~start"] == parent
        # Each shared-terminal label belongs to its distinct parent branch.
        # Other labels belong to the distinct child branch.
        label_at_parent = child in section["shared"]
        midpoint = _midpoint_label_route(points, label_at_parent, a, b, label_sizes[edge["~id"]], padding)
        if midpoint is None:
            return None
        waypoints, label_anchor = midpoint
        if not forward:
            waypoints.reverse()
        routes[edge["~id"]] = {"route_type": "polyline", "waypoints": waypoints,
            "label_anchor": label_anchor, "label_placement": "segment_waypoint_midpoint", "label_visible": True,
            "label_status": "full", "route_status": "template", "endpoint_mode": "explicit_ports"}
    # Coordinates and ports use the exact display rectangles supplied by the API.
    bounds = _bounds(coords, coords)
    points = [point for route in routes.values() for point in route["waypoints"]]
    if points:
        bounds["left"] = min(bounds["left"], *(point[0] for point in points))
        bounds["right"] = max(bounds["right"], *(point[0] for point in points))
        bounds["top"] = min(bounds["top"], *(point[1] for point in points))
        bounds["bottom"] = max(bounds["bottom"], *(point[1] for point in points))
    ox, oy = (bounds["left"] + bounds["right"]) / 2, (bounds["top"] + bounds["bottom"]) / 2
    for nid, point in coords.items():
        point["x"], point["y"] = round((point["x"] - ox) * scale, 3), round((point["y"] - oy) * scale, 3)
        point["width"], point["height"] = dimensions[nid]["width"], dimensions[nid]["height"]
    for edge in edges:
        route = routes[edge["~id"]]
        route["waypoints"] = [[round((x - ox) * scale, 3), round((y - oy) * scale, 3)] for x, y in route["waypoints"]]
        route["label_anchor"] = [round((route["label_anchor"][0] - ox) * scale, 3), round((route["label_anchor"][1] - oy) * scale, 3)]
        route["source_port"] = _port(coords[edge["~start"]], route["waypoints"][0])
        route["target_port"] = _port(coords[edge["~end"]], route["waypoints"][-1])
        if route["source_port"] is None or route["target_port"] is None:
            return None
    metrics = compute_metrics(coords, edges, routes)
    if metrics["node_overlap_count"] or metrics["edge_node_intersection_count"]:
        return None
    metadata = []
    for index, section in enumerate(sections):
        signature = section["signature"]
        path = [kinds[root], *(step[2] for step in signature)]
        terminals = [nid for nid in section["members"] if not any(parent == nid for parent, _ in detected["oriented"].values())]
        section_bounds = _bounds(coords, section["members"])
        metadata.append({"id": f"metapath-{index + 1}", "side": "left" if index < split else "right",
            "path": path, "steps": [{"source_type": path[i], "target_type": step[2],
                "relationship_type": step[0], "recorded_direction": step[1]} for i, step in enumerate(signature)],
            "instance_count": sum(len(detected["parents"][nid]) for nid in terminals),
            "root_child_count": len(section["roots"]), "node_count": len(section["members"]),
            "terminal_count": len(terminals),
            "shared_terminal_ids": section["shared"], "node_ids": sorted(section["members"]),
            "root_child_ids": section["roots"], "bounds": section_bounds,
            "size": {"width": round(section_bounds["right"] - section_bounds["left"], 3),
                     "height": round(section_bounds["bottom"] - section_bounds["top"], 3)},
            "panels": [{"root_child_ids": roots, "node_ids": sorted(panel_geometry[(index, i)]["nodes"]),
                        "bounds": _bounds(coords, panel_geometry[(index, i)]["nodes"])} for i, roots in enumerate(plans[index]["panels"])],
            "scope": "displayed_graph"})
    final_bounds = {key: round((value - (ox if key in {"left", "right"} else oy)) * scale, 3) for key, value in bounds.items()}
    metrics["runtime_ms"] = round((time.monotonic() - started) * 1000, 3)
    metrics["track_policy"] = "none"
    metrics["track_policy_violation_count"] = 0
    metrics["genomic_order_violation_count"] = 0
    details = {"template": TEMPLATE, "hub_id": root, "root_id": root, "scope": "displayed_graph", "sections": metadata,
        "semantic_type_count": len(set(kinds.values())), "section_count": len(sections),
        "node_count": len(nodes), "edge_count": len(edges), "bounds": final_bounds,
        "size": {"width": round((bounds["right"] - bounds["left"]) * scale, 3),
                 "height": round((bounds["bottom"] - bounds["top"]) * scale, 3)},
        "endpoint_mode": "explicit_ports"}
    return LayoutResult(coords=coords, edge_routes=routes, metadata={
        "engine": "metapath_sections", "version": 1, "template": TEMPLATE,
        "config_fingerprint": config.fingerprint("metapath_sections"),
        "previous_layout_status": "template_deterministic", "track_policy": "none",
        "template_details": details, "metrics": metrics})


def metapath_layout(graph, core_nodes, dimensions, config):
    """Prefer compact tree sections; use general typed path roles otherwise."""
    result = _tree_metapath_layout(graph, core_nodes, dimensions, config)
    if result is not None:
        return result
    from .path_layout import path_layout
    return path_layout(graph, core_nodes, dimensions, config)
