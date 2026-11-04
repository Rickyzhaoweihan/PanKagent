"""Pure renderer projection; presentation metadata never replaces graph evidence."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from typing import Any


NODE_DISPLAY_TYPES = {
    "Gene": "gene", "GO_term": "gene_ontology", "Protein": "coding_elements",
    "Transcript": "coding_elements", "Exon": "coding_elements",
    "TSS_segment": "coding_elements", "CDS_segments": "coding_elements",
    "UTR_segments": "coding_elements", "Sample_node": "Sample node",
}
EDGE_DISPLAY_TYPES = {
    "GENE_ENRICHED_IN": "gene_enriched_in", "GENE_DETECTED_IN": "gene_detected_in",
    "FUNCTION_ANNOTATION": "function_annotation", "GENE_ACTIVITY_SCORE_IN": "gene_activity_score_in",
    "HAS_EFFECTOR_GENE": "effector_gene_of",
}
_GENERIC_LABELS = {"ontology", "coding_element", "coding_elements"}


def _id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    result = str(value)
    return result if result and len(result) <= 1024 else None


def _json_value(value: Any, fixes: Counter, depth: int = 0) -> Any:
    """Accept JSON-safe evidence without interpreting properties as controls."""
    if depth > 12:
        raise ValueError("Graph properties exceed the supported nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            fixes["nonfinite_values"] += 1
            return None
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Graph property keys must be strings")
        return {key: _json_value(item, fixes, depth + 1) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item, fixes, depth + 1) for item in value]
    raise ValueError("Graph properties must contain JSON values")


def _steps(evidence: Mapping) -> list[Mapping]:
    steps = evidence.get("steps", [])
    if isinstance(steps, Mapping):
        steps = list(steps.values())
    return [step for step in steps if isinstance(step, Mapping)] if isinstance(steps, list) else []


def _completeness(evidence: Mapping, steps: list[Mapping], count: int) -> str:
    if evidence.get("completeness") in {"complete", "partial", "empty", "failed", "unknown"}:
        return evidence["completeness"]
    states = [step.get("status", "unknown") for step in steps] or [evidence.get("status", "unknown")]
    if evidence.get("truncated") or any(step.get("truncated") for step in steps):
        return "partial"
    if any(state in {"failed", "partial", "cancelled", "unknown"} for state in states):
        return "failed" if all(state == "failed" for state in states) else "partial"
    return "complete" if count else "empty"


def project_evidence(evidence: dict, focus_ids: list[str] | None = None) -> dict:
    """Map bounded canonical evidence to the legacy renderer without querying.

    Real IDs and raw labels/types remain unchanged. Display aliases are separate
    fields. A supplied edge ID takes priority; missing IDs receive a stable hash
    that includes the graph release, endpoints, relationship type and properties.
    """
    if not isinstance(evidence, Mapping):
        raise ValueError("Evidence must be an object")
    steps = _steps(evidence)
    versions = {str(item["graph_version"]) for item in [evidence, *steps] if item.get("graph_version")}
    if len(versions) > 1:
        raise ValueError("Cannot project evidence from conflicting graph releases")
    version = next(iter(versions), "unknown")
    # Aggregated records take precedence; step-only evidence is also supported.
    batches = [evidence, *steps]
    nodes, edges = {}, {}
    counters = Counter(invalid_nodes=0, invalid_edges=0, dangling_edges=0, duplicate_nodes=0,
                       duplicate_edges=0, nonfinite_values=0, conflicting_node_records=0)
    for batch in batches:
        for node in batch.get("nodes", []) or []:
            if not isinstance(node, Mapping) or not (nid := _id(node.get("id", node.get("~id")))):
                counters["invalid_nodes"] += 1
                continue
            labels = node.get("labels", node.get("~labels", []))
            props = node.get("properties", node.get("~properties", {}))
            if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels) or not isinstance(props, Mapping):
                counters["invalid_nodes"] += 1
                continue
            labels = sorted(set(labels))
            props = _json_value(props, counters)
            label = next((kind for kind in NODE_DISPLAY_TYPES if kind in labels), None)
            label = label or next((kind for kind in labels if kind.lower() not in _GENERIC_LABELS), labels[0] if labels else "Unknown")
            name = props.get("name") or props.get("ensembl_name") or props.get("id") or nid
            visible_name = str(name) if len(str(name)) <= 15 else str(props.get("id") or nid)
            projected = {"~id": nid, "~labels": labels, "~properties": props,
                         "display_type": NODE_DISPLAY_TYPES.get(label, label), "display_label": visible_name}
            if nid in nodes:
                counters["duplicate_nodes"] += 1
                if nodes[nid] != projected:
                    counters["conflicting_node_records"] += 1
                continue
            nodes[nid] = projected
    for batch in batches:
        for edge in batch.get("edges", []) or []:
            if not isinstance(edge, Mapping):
                counters["invalid_edges"] += 1
                continue
            start = _id(edge.get("start_id", edge.get("source", edge.get("~start"))))
            end = _id(edge.get("end_id", edge.get("target", edge.get("~end"))))
            kind = edge.get("type", edge.get("~type"))
            props = edge.get("properties", edge.get("~properties", {}))
            if not start or not end or not isinstance(kind, str) or not kind or not isinstance(props, Mapping):
                counters["invalid_edges"] += 1
                continue
            if start not in nodes or end not in nodes:
                counters["dangling_edges"] += 1
                continue
            props = _json_value(props, counters)
            identity = [version, start, kind, end, props]
            eid = _id(edge.get("id", edge.get("~id")))
            eid = eid or "edge:" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            projected = {"~id": eid, "~start": start, "~end": end, "~type": kind, "~properties": props,
                         "display_type": EDGE_DISPLAY_TYPES.get(kind, kind.lower()),
                         "display_label": kind.replace("_", " ").capitalize()}
            if eid in edges:
                if edges[eid] != projected:
                    raise ValueError("Conflicting records reuse a graph relationship ID")
                counters["duplicate_edges"] += 1
                continue
            edges[eid] = projected
    graph = {"nodes": [nodes[nid] for nid in sorted(nodes)], "edges": [edges[eid] for eid in sorted(edges)]}
    # This is a payload bound, not a display-node limit: larger valid result graphs
    # are filtered later while their full evidence stays in the owning run.
    if len(nodes) > 6000 or len(edges) > 15000 or len(json.dumps(graph, ensure_ascii=False, allow_nan=False).encode()) > 8_000_000:
        raise ValueError("Graph projection exceeds the bounded evidence payload")
    requested = [str(nid) for nid in (focus_ids or []) if _id(nid)]
    focus = sorted(set(requested).intersection(nodes))
    focus_source = "explicit" if focus else "resolved_entities"
    if not focus:
        for step in sorted(steps, key=lambda item: item.get("purpose") == "context"):
            resolved = [str(entity["id"]) for entity in step.get("resolved_entities", [])
                        if isinstance(entity, Mapping) and entity.get("state", entity.get("status")) == "resolved"
                        and entity.get("id") is not None and str(entity["id"]) in nodes]
            if resolved:
                focus = sorted(set(resolved))
                break
    if not focus and nodes:
        degree = Counter(endpoint for edge in edges.values() for endpoint in (edge["~start"], edge["~end"]))
        focus = [min(nodes, key=lambda nid: (-degree[nid], nid))]
        focus_source = "graph_seed"
    completeness = _completeness(evidence, steps, len(nodes))
    full = {"node_count": len(nodes), "edge_count": len(edges), "graph_version": version,
            "scientific_completeness": completeness, "truncated": bool(evidence.get("truncated") or any(step.get("truncated") for step in steps)),
            "step_count": len(steps), "steps": [{"step_id": step.get("step_id", step.get("id")), "status": step.get("status", "unknown"),
                                                  "purpose": step.get("purpose", "primary"), "row_count": len(step.get("rows", []) or [])} for step in steps]}
    return {"combined_query_result": graph, "core_nodes": focus, "graph_version": version,
            "full_evidence": full, "metadata": {"projection": dict(counters), "focus_source": focus_source,
                                                "missing_focus_ids": sorted(set(requested) - nodes.keys())}}
