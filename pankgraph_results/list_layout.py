"""Ordered fan-out/fan-in presentation without filtering scientific evidence."""

def relationship_list(graph, focus=()):
    nodes = {n['~id']: n for n in graph['nodes']}
    edges = graph['edges']
    if len(nodes) < 8 or len(edges) != len(nodes) - 1:
        return None
    shared = set(nodes)
    for edge in edges:
        if edge['~start'] == edge['~end']:
            return None
        shared &= {edge['~start'], edge['~end']}
    if len(shared) != 1:
        return None
    hub = next(iter(shared))
    direction = None
    signature = None
    leaves = set()
    for edge in edges:
        forward = edge['~start'] == hub
        leaf = edge['~end'] if forward else edge['~start']
        if leaf not in nodes or leaf in leaves:
            return None
        current = (forward, edge['~type'])
        if signature is not None and current != signature:
            return None
        signature = current
        direction = forward
        leaves.add(leaf)
    if leaves != set(nodes) - {hub}:
        return None

    common = set.intersection(*(set(nodes[n].get('~labels', [])) for n in leaves))
    if not common:
        return None
    groups = {}
    for nid in leaves:
        key = tuple(sorted(set(nodes[nid].get('~labels', [])) - common))
        groups.setdefault(key, []).append(nid)

    def order(nid):
        p = nodes[nid].get('~properties', {})
        return (str(p.get('data_modality', '')).casefold(), str(p.get('anatomical_structure', '')).casefold(), str(p.get('name', '')).casefold(), str(p.get('id', nid)))
    ordered = sorted(leaves, key=order)
    labels = {}
    for nid in [hub, *ordered]:
        p = nodes[nid].get('~properties', {})
        text = str(p.get('data_modality', '')) + ' #' + str(p.get('id', nid)) if 'Sample_node' in nodes[nid].get('~labels', []) else str(p.get('name') or p.get('id') or nid)
        labels[nid] = text if len(text) <= 30 else text[:27] + '…'
    widths = {nid: max(35, len(label) * 3.6 + 10) for nid, label in labels.items()}
    hw = widths[hub]
    lw = max((widths[nid] for nid in ordered))
    gap = 85
    coords = {}

    def put(nid, x, y):
        w = widths[nid]
        coords[nid] = {'x': x, 'y': y, 'width': w, 'height': 16, 'Level': 'Core' if nid in focus else 'Neighbor', 'start_xy': [x - w / 2, y - 8], 'end_xy': [x + w / 2, y + 8]}
    put(hub, hw / 2, 0)
    sides = {}
    group_rows = []
    if len(groups) == 1:
        for index, nid in enumerate(ordered):
            put(nid, hw + gap + lw / 2, index * 30)
            sides[nid] = 1
    else:
        # Keep source collections intact; larger groups are allocated first.
        # KEGG/Reactome retain their familiar left/right order for a pair.
        keys = sorted(groups)
        if len(keys) > 2:
            keys.sort(key=lambda k: (-len(groups[k]), k))
        heights = [0, 0]
        for key in keys:
            side = min(range(2), key=lambda i: (heights[i], i))
            members = sorted(groups[key], key=order)
            sign = -1 if side == 0 else 1
            x = -gap-lw/2 if side == 0 else hw+gap+lw/2
            group_rows.append({'labels': list(key), 'side': 'left' if side == 0 else 'right', 'count': len(members)})
            for index, nid in enumerate(members):
                put(nid, x, (heights[side]+index)*30)
                sides[nid] = sign
            heights[side] += len(members)+1
    routes = {}
    for edge in edges:
        leaf = edge['~end'] if direction else edge['~start']
        p = coords[leaf]
        sign = sides[leaf]
        boundary = hw if sign == 1 else 0
        start = [boundary, 0]
        end = [p['x'] - sign*p['width'] / 2, p['y']]
        points = [[boundary + sign*20, 0], [boundary + sign*20, p['y']]]
        if not direction:
            start, end, points = (end, start, list(reversed(points)))
        routes[edge['~id']] = {'source_port': start, 'target_port': end, 'waypoints': points, 'route_type': 'polyline', 'list_leaf_endpoint': 'target' if direction else 'source', 'route_status': 'ordered', 'label_visible': True, 'label_anchor': [boundary + sign*55, p['y']]}
    return {'status': 'optimized', 'xy_json': coords, 'edge_routes': routes, 'presentation_mode': 'relationship_list', 'labels': labels, 'details': {'metrics': {'node_overlap_count': 0}, 'row_count': len(ordered), 'hub_id': hub, 'common_labels': sorted(common), 'groups': group_rows}}
