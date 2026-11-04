"""Standard Graphviz node layout; native frontend curves preserve parallel edges."""
import math


def pygraphviz_layout(graph, coords):
    import pygraphviz as pgv

    # Synthetic identifiers keep user labels out of DOT identifiers. strict=False
    # is essential: two coloc evidence records must remain two relationships.
    ids = {nid: 'n' + str(i) for i, nid in enumerate(sorted(coords))}
    g = pgv.AGraph(strict=False, directed=True)
    g.graph_attr.update(rankdir='LR', nodesep='0.6', ranksep='1.2',
                        overlap='false', splines='spline')
    g.node_attr.update(shape='box', fixedsize='true', label='')
    for nid, point in sorted(coords.items()):
        g.add_node(ids[nid], width=str(point['width'] / 72),
                   height=str(point['height'] / 72))
    for i, edge in enumerate(sorted(graph['edges'], key=lambda e: e['~id'])):
        g.add_edge(ids[edge['~start']], ids[edge['~end']], key=str(i))
    try:
        g.layout(prog='dot')
        result = {}
        for nid, alias in ids.items():
            x, y = map(float, str(g.get_node(alias).attr['pos']).split(','))
            if not all(math.isfinite(v) for v in (x, y)):
                raise ValueError('Non-finite Graphviz position')
            result[nid] = {**coords[nid], 'x': x, 'y': -y}
        return result
    finally:
        g.close()
