"""Polygon geometry for a simple cycle after collapsing parallel records.

The motif is topological: neither node names nor biological classes select it.
Every record retains a distinct route, direction and inspectable center label.
"""
from collections import defaultdict
import math

from .geometry import rect_for, rects_overlap
from .metrics import compute_metrics, _label_rect


def _port(box, towards):
    dx, dy = towards[0] - box['x'], towards[1] - box['y']
    factors = [box[key] / (2 * abs(delta)) for key, delta in (('width', dx), ('height', dy)) if abs(delta) > 1e-9]
    factor = min(factors)
    return [round(box['x'] + dx * factor, 3), round(box['y'] + dy * factor, 3)]


def cycle_order(model):
    from .metapath import _kind, _order
    nodes, root = model['nodes'], model['root']
    if not 3 <= len(nodes) <= 8:
        return None
    neighbors = {nid: set() for nid in nodes}
    for edge in model['edges']:
        a, b = edge['~start'], edge['~end']
        neighbors[a].add(b); neighbors[b].add(a)
    if any(len(ids) != 2 for ids in neighbors.values()):
        return None
    order = [root]
    current = min(neighbors[root], key=lambda nid: (_kind(nodes[nid]), _order(nodes[nid])))
    while current != root:
        if current in order:
            return None
        previous = order[-1]
        order.append(current)
        current = next(nid for nid in neighbors[current] if nid != previous)
    return order if len(order) == len(nodes) else None


def cycle_geometry(model, dimensions, config):
    order = cycle_order(model)
    if order is None:
        return None
    edges = model['edges']
    groups = defaultdict(list)
    for edge in edges:
        groups[tuple(sorted((edge['~start'], edge['~end'])))].append(edge)
    span = max(max(point['width'], point['height']) for point in dimensions.values())
    label_span = max(min(220, len(edge['~type'].replace('_', ' ')) * 5.8 + 8) for edge in edges)
    max_parallel = max(map(len, groups.values()))
    base_radius = max(span, label_span, config.layer_spacing) * 1.4 + 30 * max_parallel
    for expansion in (1.0, 1.35, 1.8, 2.4):
        radius = base_radius * expansion
        coords = {nid: {**dimensions[nid],
                       'x': round(radius * math.cos(math.pi + index * 2 * math.pi / len(order)), 3),
                       'y': round(radius * math.sin(math.pi + index * 2 * math.pi / len(order)), 3)}
                  for index, nid in enumerate(order)}
        routes = {}
        for (a, b), records in sorted(groups.items()):
            u, v = coords[a], coords[b]
            dx, dy = v['x'] - u['x'], v['y'] - u['y']
            length = math.hypot(dx, dy)
            nx, ny = -dy / length, dx / length
            # Space labels in model coordinates, accounting for horizontal
            # text on a diagonal edge. Cubic t=.5 moves 3/4 of the control bow.
            text_width = max(min(220, len(edge['~type'].replace('_', ' ')) * 5.8 + 8) for edge in records)
            separation = max(42, text_width * abs(nx) + 18 * abs(ny) + 2 * config.label_clearance) / .75
            for index, edge in enumerate(sorted(records, key=lambda e: e['~id'])):
                bow = (index - (len(records)-1)/2) * separation
                c1 = [u['x'] + dx/3 + nx*bow, u['y'] + dy/3 + ny*bow]
                c2 = [u['x'] + dx*2/3 + nx*bow, u['y'] + dy*2/3 + ny*bow]
                source, target = _port(u, c1), _port(v, c2)
                if len(records) == 1:
                    anchor = [round((source[0]+target[0])/2, 3), round((source[1]+target[1])/2, 3)]
                    route = {'route_type': 'polyline', 'source_port': source, 'target_port': target,
                             'waypoints': [anchor], 'label_anchor': anchor,
                             'label_placement': 'segment_waypoint_midpoint'}
                else:
                    c1, c2 = [[round(value, 3) for value in point] for point in (c1, c2)]
                    anchor = [round((source[i] + 3*c1[i] + 3*c2[i] + target[i])/8, 3) for i in range(2)]
                    route = {'route_type': 'bezier', 'source_port': source, 'target_port': target,
                             'control_points': [c1, c2], 'label_anchor': anchor,
                             'label_placement': 'cubic_midpoint', 'tangent_mode': 'cubic_smooth'}
                if edge['~start'] != a:
                    route['source_port'], route['target_port'] = route['target_port'], route['source_port']
                    if 'control_points' in route: route['control_points'].reverse()
                    else: route['waypoints'].reverse()
                route.update(label_visible=True, label_status='full', endpoint_mode='explicit_ports', route_status='cycle')
                routes[edge['~id']] = route
        metrics = compute_metrics(coords, edges, routes)
        boxes = [_label_rect(routes[edge['~id']], edge) for edge in edges]
        label_pairs = sum(rects_overlap(a, b) for i, a in enumerate(boxes) for b in boxes[i+1:])
        label_nodes = sum(rects_overlap(box, rect_for(point)) for box in boxes for point in coords.values())
        if not any(metrics[key] for key in ('node_overlap_count', 'edge_node_intersection_count',
                                            'edge_label_node_intersection_count', 'edge_label_edge_intersection_count')) and not label_pairs and not label_nodes:
            return coords, routes, {'geometry_strategy': 'cycle_polygon',
                                    'shape': 'triangle' if len(order) == 3 else 'polygon',
                                    'cycle_node_ids': order, 'vertex_count': len(order)}
    return None
