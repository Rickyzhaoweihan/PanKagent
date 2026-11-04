"""Bounded, evidence-preserving mind-map sections for simple typed graphs.

Only presentation is returned. A type tree supplies the rooted hierarchy;
maximal type/relationship/direction paths define visually separate sections.
Unsupported or ambiguous instances retain the caller's network layout.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque


TEMPLATE = "metapath_sections_v1"
ROW_GAP = 26.0
NODE_HEIGHT = 16.0
LEVEL_GAP = 58.0
PANEL_GAP = 28.0
SECTION_GAP = 56.0
BRANCH_INSET = 42.0
MAX_SECTIONS = 8
_GENERIC = {"ontology", "coding element", "coding elements"}


def _text(value):
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _kind(node):
    labels = node.get("~labels", [])
    if not isinstance(labels, list) or not all(isinstance(item, str) and item.strip() for item in labels):
        return None
    semantic = {" ".join(item.casefold().replace("_", " ").split()) for item in labels}
    specific = semantic - _GENERIC
    # Generic projection aliases must not merge distinct biological types or
    # conceal nodes having two incompatible semantic labels.
    semantic = specific
    return next(iter(semantic)) if len(semantic) == 1 else None


def _label(node, kind):
    props = node.get("~properties", {})
    props = props if isinstance(props, dict) else {}
    identity = _text(props.get("id")) or node["~id"]
    assay = _text(props.get("data_modality")) or _text(props.get("Data Modality"))
    if kind in {"sample", "sample node"} and assay:
        value = f"{assay} · #{identity}"
    else:
        value = _text(props.get("name")) or _text(node.get("display_label")) or identity
    return value if len(value) <= 55 else value[:52] + "…"


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
    if any(len(neighbors) == len(nodes) - 1 for neighbors in adjacency.values()):
        return None  # Preserve the existing flat-star/list presentation.
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
    if len(levels) != len(set(kinds.values())) or max(levels.values()) < 2:
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
    candidates.sort(key=lambda nid: (nid not in focus, -len(adjacency[nid]), _order(nodes[nid])))
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


def _section_plan(section, count, widths, levels, kinds):
    panels = _split_weighted(section["roots"], count, section["weights"])
    columns = defaultdict(float)
    for nid in section["members"] - set(section["shared"]):
        level = levels[kinds[nid]]
        columns[level] = max(columns[level], widths[nid])
    # A root-child subtree is never split across wrapped panels.
    panel_width = sum(columns.values()) + (len(columns) - 1) * LEVEL_GAP + BRANCH_INSET + 22.0
    rows = max(sum(section["weights"][nid] for nid in panel) for panel in panels)
    shared_count = len(section["shared"])
    height = max(rows, shared_count, 1) * ROW_GAP + 56.0
    width = count * panel_width + (count - 1) * PANEL_GAP
    if shared_count:
        width += LEVEL_GAP + max(widths[nid] for nid in section["shared"])
    return {"panels": panels, "columns": dict(columns), "panel_width": panel_width,
            "rows": rows, "width": width, "height": height}


def _layout_score(plans, split, root_width):
    left, right = plans[:split], plans[split:]
    side_height = lambda items: sum(item["height"] for item in items) + SECTION_GAP * max(0, len(items) - 1)
    width = root_width + 120.0 + max([item["width"] for item in left] or [0]) + max([item["width"] for item in right] or [0])
    height = max(NODE_HEIGHT, side_height(left), side_height(right))
    # The physical unit is the existing renderer's 6-pixel coordinate scale.
    # Optimize fitted readability in a typical 4:3 viewer, with deterministic
    # low-panel/low-area tie breakers.
    fit = min(600.0 / width, 450.0 / height)
    return (-fit, width * height, sum(len(item["panels"]) for item in plans), split)


def _choose_plans(sections, widths, levels, kinds, root_width):
    alternatives = [[_section_plan(section, count, widths, levels, kinds)
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
            "top": round(min(coords[nid]["y"] - NODE_HEIGHT / 2 for nid in ids), 3),
            "bottom": round(max(coords[nid]["y"] + NODE_HEIGHT / 2 for nid in ids), 3)}


def _midpoint_label_route(points, at_start, source, target):
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
    low, high = half + 2.0, length - 2.0
    if len(path) == 2:
        high -= other["width"] / 2 if abs(dx) > 1e-6 else other["height"] / 2
    if high - low < 4.0:
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


def _geometry_counts(coords, routes, edges):
    boxes = {nid: (point["x"] - point["width"] / 2, point["y"] - NODE_HEIGHT / 2,
                   point["x"] + point["width"] / 2, point["y"] + NODE_HEIGHT / 2) for nid, point in coords.items()}
    ids = sorted(boxes)
    overlaps = 0
    for index, nid in enumerate(ids):
        a = boxes[nid]
        for other in ids[index + 1:]:
            b = boxes[other]
            overlaps += min(a[2], b[2]) > max(a[0], b[0]) + 1e-4 and min(a[3], b[3]) > max(a[1], b[1]) + 1e-4
    intersections = 0
    for edge in edges:
        route = routes[edge["~id"]]
        a, b = coords[edge["~start"]], coords[edge["~end"]]
        points = [[a["x"], a["y"]], *route["waypoints"], [b["x"], b["y"]]]
        for start, end in zip(points, points[1:]):
            horizontal = abs(start[1] - end[1]) < 1e-4
            for nid, box in boxes.items():
                if nid in {edge["~start"], edge["~end"]}:
                    continue
                if horizontal:
                    hit = box[1] + 1e-4 < start[1] < box[3] - 1e-4 and min(max(start[0], end[0]), box[2]) > max(min(start[0], end[0]), box[0]) + 1e-4
                else:
                    hit = box[0] + 1e-4 < start[0] < box[2] - 1e-4 and min(max(start[1], end[1]), box[3]) > max(min(start[1], end[1]), box[1]) + 1e-4
                intersections += hit
    return int(overlaps), int(intersections)


def metapath_sections(graph, focus=()):
    """Return deterministic relationship-list geometry or a conservative None.

    Sections describe paths in the *displayed* graph only. Missing displayed
    branches do not assert assay absence, sample absence, or cohort eligibility.
    """
    focus = {item for item in (focus or ()) if isinstance(item, str)}
    detected = _detect(graph, focus)
    if detected is None:
        return None
    nodes, edges, kinds, root = (detected[key] for key in ("nodes", "edges", "kinds", "root"))
    sections, levels = detected["sections"], detected["levels"]
    labels = {nid: _label(nodes[nid], kinds[nid]) for nid in sorted(nodes)}
    widths = {nid: max(35.0, len(label) * 3.6 + 10.0) for nid, label in labels.items()}
    split, plans = _choose_plans(sections, widths, levels, kinds, widths[root])
    coords, node_section, node_panel, panel_geometry, section_geometry = {}, {}, {}, {}, {}

    def put(nid, x, y):
        coords[nid] = {"x": x, "y": y, "width": widths[nid], "height": NODE_HEIGHT,
                       "Level": "Core" if nid in focus else "Neighbor"}

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
                x_by_level, cursor = {}, distance + BRANCH_INSET
                for level, width in sorted(plan["columns"].items()):
                    x_by_level[level] = side * (cursor + width / 2)
                    cursor += width + LEVEL_GAP
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
            anchor_inner = side * (distance - PANEL_GAP + LEVEL_GAP / 2)
            anchor_x = side * (distance - PANEL_GAP + LEVEL_GAP + (max((widths[nid] for nid in shared), default=0)) / 2)
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
        midpoint = _midpoint_label_route(points, label_at_parent, a, b)
        if midpoint is None:
            return None
        waypoints, label_anchor = midpoint
        if not forward:
            waypoints.reverse()
        routes[edge["~id"]] = {"route_type": "polyline", "waypoints": waypoints,
            "label_anchor": label_anchor, "label_placement": "segment_waypoint_midpoint", "label_visible": True,
            "label_status": "full", "route_status": "template", "endpoint_mode": "native_shape_intersection"}
    # Keep native border clipping: browser-measured text can differ from our
    # conservative boxes. Explicit pixel ports would detach short-label edges.
    bounds = _bounds(coords, coords)
    points = [point for route in routes.values() for point in route["waypoints"]]
    if points:
        bounds["left"] = min(bounds["left"], *(point[0] for point in points))
        bounds["right"] = max(bounds["right"], *(point[0] for point in points))
        bounds["top"] = min(bounds["top"], *(point[1] for point in points))
        bounds["bottom"] = max(bounds["bottom"], *(point[1] for point in points))
    ox, oy = (bounds["left"] + bounds["right"]) / 2, (bounds["top"] + bounds["bottom"]) / 2
    for point in coords.values():
        point["x"], point["y"] = round(point["x"] - ox, 3), round(point["y"] - oy, 3)
        point["width"] = round(point["width"], 3)
        point["start_xy"] = [round(point["x"] - point["width"] / 2, 3), round(point["y"] - NODE_HEIGHT / 2, 3)]
        point["end_xy"] = [round(point["x"] + point["width"] / 2, 3), round(point["y"] + NODE_HEIGHT / 2, 3)]
    for route in routes.values():
        route["waypoints"] = [[round(x - ox, 3), round(y - oy, 3)] for x, y in route["waypoints"]]
        route["label_anchor"] = [round(route["label_anchor"][0] - ox, 3), round(route["label_anchor"][1] - oy, 3)]
    overlaps, intersections = _geometry_counts(coords, routes, edges)
    if overlaps or intersections:
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
    geometry = {"xy_json": coords, "edge_routes": routes, "labels": labels}
    digest = hashlib.sha256(json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    final_bounds = {key: round(value - (ox if key in {"left", "right"} else oy), 3) for key, value in bounds.items()}
    return {"status": "optimized", "presentation_mode": "metapath_sections", **geometry,
        "details": {"template": TEMPLATE, "hub_id": root, "root_id": root, "scope": "displayed_graph", "sections": metadata,
            "semantic_type_count": len(set(kinds.values())), "section_count": len(sections),
            "node_count": len(nodes), "edge_count": len(edges), "bounds": final_bounds,
            "size": {"width": round(bounds["right"] - bounds["left"], 3), "height": round(bounds["bottom"] - bounds["top"], 3)},
            "endpoint_mode": "native_shape_intersection", "previous_layout_status": "template_deterministic",
            "metrics": {"node_overlap_count": overlaps, "edge_node_intersection_count": intersections,
                "geometry_scope": "node_centers_waypoints_conservative_node_rectangles", "layout_hash": digest}}}
