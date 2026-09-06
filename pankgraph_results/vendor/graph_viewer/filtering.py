"""Data-only regular filter extracted from the pinned Graph_viewer app/index.py.

Changes: regular mode only; configurable real-node ceiling up to 100. No query
handler, environment credentials, database client or genome-index dependency.
"""
import hashlib
import json
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Tuple

MAX_NEIGHBORS_PER_CORE = 8
DEFAULT_MAX_GRAPH_NODES = 25
MAX_GRAPH_NODES = 100
GENERIC_LABELS = {"ontology", "coding_element", "coding_elements"}
CANONICAL_NODE_TYPE_PRIORITY = ["Gene", "Transcript", "TSS_segment", "Exon", "CDS_segments", "UTR_segments", "Protein", "GO_term"]

def get_node_type(node: Dict[str, Any]) -> str:
    """
    Return the most specific domain label from the node's complete label set.

    Neo4j can return container and entity labels together, and label order is
    not part of the data contract. Prefer known biological entity labels before
    falling back to an unknown non-container label.
    """
    labels = node.get("~labels", [])
    if isinstance(labels, str):
        labels = [labels]

    labels = [str(label) for label in labels if label]
    labels_by_lower = {label.lower(): label for label in labels}
    for canonical_type in CANONICAL_NODE_TYPE_PRIORITY:
        if canonical_type.lower() in labels_by_lower:
            return canonical_type

    for label in labels:
        if label.lower() not in GENERIC_LABELS:
            return label

    return labels[0] if labels else "Unknown"

def _effective_real_node_budget(
    node_map: Dict[str, Dict[str, Any]],
    requested_max_nodes: int,
    layout_mode: str,
) -> int:
    requested_max_nodes = max(1, int(requested_max_nodes or DEFAULT_MAX_GRAPH_NODES))
    return min(MAX_GRAPH_NODES, requested_max_nodes)

def filter_graph(
    query_results: List[Dict[str, Any]],
    core_nodes: Iterable[str],
    max_neighbors_per_core: int = MAX_NEIGHBORS_PER_CORE,
    max_nodes: int = DEFAULT_MAX_GRAPH_NODES,
    layout_mode: str = "kg_only",
) -> Tuple[Dict[str, Any], set[str], Dict[str, int]]:
    """
    Deduplicate graph data and keep a bounded visible real-node graph.

    Selection follows the global rule: keep core nodes first, then preserve
    node-type and relationship-type diversity. Overflow display nodes are added
    after layout and are not part of this real-node budget.
    """
    core_nodes = {str(node_id) for node_id in core_nodes if node_id}
    node_map = {}
    edge_map = {}

    for result in query_results:
        for node in result.get("nodes", []):
            nid = node.get("~id")
            if nid:
                node_map[str(nid)] = node

        for edge in result.get("edges", []):
            s = edge.get("~start")
            t = edge.get("~end")
            if not s or not t or s == t:
                continue

            edge_id = str(edge.get("~id") or "")
            if not edge_id:
                edge_id = hashlib.sha1(
                    json.dumps(
                        {
                            "start": str(s),
                            "end": str(t),
                            "type": str(edge.get("~type") or ""),
                            "properties": edge.get("~properties") or {},
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
            edge_map.setdefault(edge_id, edge)

    def edge_sort_key(edge: Dict[str, Any]) -> Tuple[int, str, str, str, str]:
        props = edge.get("~properties") or {}
        source_priority = 1 if props.get("query_source") == "pgsql" else 0
        return (
            source_priority,
            str(edge.get("~type") or ""),
            str(edge.get("~start") or ""),
            str(edge.get("~end") or ""),
            str(edge.get("~id") or ""),
        )

    all_edges = sorted(edge_map.values(), key=edge_sort_key)

    if not core_nodes:
        core_nodes = set(node_map.keys())

    def node_sort_key(node_id: str) -> Tuple[str, str, str]:
        node = node_map.get(node_id, {})
        props = node.get("~properties", {})
        return (get_node_type(node), str(props.get("name", "")), node_id)

    def hidden_type_counts(visible_nodes: Iterable[str]) -> Dict[str, int]:
        visible_set = {str(node_id) for node_id in visible_nodes}
        counts = defaultdict(int)
        for node_id, node in node_map.items():
            if node_id not in visible_set:
                counts[get_node_type(node)] += 1
        return dict(sorted(counts.items()))

    max_nodes = _effective_real_node_budget(node_map, max_nodes, layout_mode)
    if len(node_map) <= max_nodes:
        filtered_nodes = set(node_map.keys())
        filtered_edges = sorted([
            edge
            for edge in all_edges
            if str(edge.get("~start")) in filtered_nodes
            and str(edge.get("~end")) in filtered_nodes
        ], key=edge_sort_key)
        filtered_result = {
            "nodes": [
                node_map[nid]
                for nid in sorted(filtered_nodes, key=node_sort_key)
                if nid in node_map
            ],
            "edges": filtered_edges,
        }
        metadata = {
            "input_node_count": len(node_map),
            "input_edge_count": len(all_edges),
            "filtered_node_count": len(filtered_result["nodes"]),
            "filtered_edge_count": len(filtered_edges),
            "hidden_node_count": 0,
            "hidden_node_types": {},
            "overflow_node_count": 0,
            "real_node_budget": max_nodes,
            "kg_node_budget": max_nodes,
            "kg_filtered_node_count": len(filtered_result["nodes"]),
            "genome_track_context_node_count": 0,
        }
        return filtered_result, core_nodes, metadata

    present_core_nodes = {node_id for node_id in core_nodes if node_id in node_map}

    if len(present_core_nodes) >= max_nodes:
        filtered_nodes = set(sorted(present_core_nodes, key=node_sort_key)[:max_nodes])
    else:
        adjacency = defaultdict(set)
        incident_edge_types = defaultdict(set)
        core_neighbor_ids = set()
        for edge in all_edges:
            start = str(edge.get("~start"))
            end = str(edge.get("~end"))
            if start not in node_map or end not in node_map:
                continue
            edge_type = str(edge.get("~type") or "")
            adjacency[start].add(end)
            adjacency[end].add(start)
            incident_edge_types[start].add(edge_type)
            incident_edge_types[end].add(edge_type)
            if start in present_core_nodes and end not in present_core_nodes:
                core_neighbor_ids.add(end)
            elif end in present_core_nodes and start not in present_core_nodes:
                core_neighbor_ids.add(start)

        selected_neighbors: List[str] = []
        neighbor_slots = max_nodes - len(present_core_nodes)
        selected_nodes = set(present_core_nodes)
        selected_edge_types = {
            str(edge.get("~type") or "")
            for edge in all_edges
            if str(edge.get("~start")) in selected_nodes
            and str(edge.get("~end")) in selected_nodes
        }
        selected_type_counts = defaultdict(int)
        for node_id in selected_nodes:
            selected_type_counts[get_node_type(node_map[node_id])] += 1

        remaining = {node_id for node_id in node_map if node_id not in selected_nodes}

        def candidate_score(node_id: str) -> Tuple[float, int, str]:
            node_type = get_node_type(node_map[node_id])
            direct_core_bonus = 500.0 if node_id in core_neighbor_ids else 0.0
            selected_degree = len(adjacency[node_id].intersection(selected_nodes))
            total_degree = len(adjacency[node_id])
            new_edge_type_count = len(incident_edge_types[node_id] - selected_edge_types)
            type_diversity_bonus = 760.0 / (1 + selected_type_counts[node_type])
            score = (
                direct_core_bonus
                + selected_degree * 120.0
                + total_degree * 28.0
                + new_edge_type_count * 60.0
                + type_diversity_bonus
            )
            return (score, total_degree, node_id)

        while len(selected_neighbors) < neighbor_slots and remaining:
            missing_types = sorted(
                {
                    get_node_type(node_map[node_id])
                    for node_id in remaining
                    if selected_type_counts[get_node_type(node_map[node_id])] == 0
                }
            )
            if not missing_types:
                break
            best_node = max(
                remaining,
                key=lambda node_id: (
                    get_node_type(node_map[node_id]) in missing_types,
                    candidate_score(node_id),
                ),
            )
            if selected_type_counts[get_node_type(node_map[best_node])] > 0:
                break
            remaining.remove(best_node)
            selected_neighbors.append(best_node)
            selected_nodes.add(best_node)
            selected_type_counts[get_node_type(node_map[best_node])] += 1
            for edge in all_edges:
                start = str(edge.get("~start"))
                end = str(edge.get("~end"))
                if start in selected_nodes and end in selected_nodes:
                    selected_edge_types.add(str(edge.get("~type") or ""))

        while len(selected_neighbors) < neighbor_slots and remaining:
            best_node = max(remaining, key=candidate_score)
            remaining.remove(best_node)
            selected_neighbors.append(best_node)
            selected_nodes.add(best_node)
            selected_type_counts[get_node_type(node_map[best_node])] += 1
            for edge in all_edges:
                start = str(edge.get("~start"))
                end = str(edge.get("~end"))
                if start in selected_nodes and end in selected_nodes:
                    selected_edge_types.add(str(edge.get("~type") or ""))

        filtered_nodes = present_core_nodes.union(selected_neighbors)

    kg_filtered_nodes = set(filtered_nodes)
    genome_track_context_nodes = set()  # Adapter intentionally supports regular KG only.

    hidden_ids_by_type = defaultdict(list)
    for node_id, node in node_map.items():
        if node_id not in filtered_nodes:
            hidden_ids_by_type[get_node_type(node)].append(node_id)
    for hidden_ids in hidden_ids_by_type.values():
        if len(hidden_ids) == 1 and len(filtered_nodes) < max_nodes:
            filtered_nodes.add(hidden_ids[0])

    filtered_edges = sorted([
        edge
        for edge in all_edges
        if str(edge.get("~start")) in filtered_nodes
        and str(edge.get("~end")) in filtered_nodes
    ], key=edge_sort_key)

    filtered_result = {
        "nodes": [
            node_map[nid]
            for nid in sorted(filtered_nodes, key=node_sort_key)
            if nid in node_map
        ],
        "edges": filtered_edges,
    }
    metadata = {
        "input_node_count": len(node_map),
        "input_edge_count": len(all_edges),
        "filtered_node_count": len(filtered_result["nodes"]),
        "filtered_edge_count": len(filtered_edges),
        "hidden_node_count": max(0, len(node_map) - len(filtered_result["nodes"])),
        "hidden_node_types": hidden_type_counts(filtered_nodes),
        "overflow_node_count": 0,
        "real_node_budget": max_nodes,
        "kg_node_budget": max_nodes,
        "kg_filtered_node_count": len(kg_filtered_nodes),
        "genome_track_context_node_count": len(genome_track_context_nodes),
    }

    return filtered_result, core_nodes, metadata
