"""Bounded, type-preserving model context derived from full graph evidence.

This changes only the synthesis view. Query results and their completeness stay
untouched. Every omission is labelled so a sampled context is never mistaken
for the entire retrieved graph.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .evidence_coverage import coverage_for_answer


TARGET_BYTES = 75_000
MAX_BYTES = 100_000
_SUMMARY_FIELDS = ("step_id", "status", "graph_version", "truncated", "error", "question", "title", "purpose", "context_for", "requested_scope", "resolved_constraints", "semantic_registry", "donor_summary")


@dataclass(frozen=True)
class _Limits:
    nodes: int
    edges: int
    rows: int
    string_chars: int
    collection_items: int


_NORMAL = _Limits(60, 100, 30, 1024, 40)
_REDUCED = _Limits(10, 15, 5, 512, 20)


def _node_type(node: Mapping) -> str:
    labels = node.get("labels", [])
    if not isinstance(labels, (list, tuple)):
        labels = [labels] if labels else []
    return "|".join(sorted({str(label) for label in labels})) or "unlabelled"


def _edge_type(edge: Mapping) -> str:
    return str(edge.get("type") or "unknown")


def _sample(items: list, cap: int, key) -> tuple[list, dict[str, int]]:
    """Allocate fairly across types, rarest first; retain original record order."""
    groups = defaultdict(list)
    for index, item in enumerate(items):
        groups[key(item)].append(index)
    selected = set()
    ordered = [indices for _, indices in sorted(groups.items(), key=lambda pair: (len(pair[1]), pair[0]))]
    position = 0
    while len(selected) < min(cap, len(items)):
        for indices in ordered:
            if position < len(indices):
                selected.add(indices[position])
                if len(selected) >= cap:
                    break
        position += 1
    kept = [item for index, item in enumerate(items) if index in selected]
    dropped = Counter(key(item) for index, item in enumerate(items) if index not in selected)
    return kept, dict(sorted(dropped.items()))


def _bounded(value: Any, limits: _Limits, changes: Counter, depth: int = 0) -> Any:
    """Bound individual fields while preserving valid JSON and explicit loss."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        changes["nonfinite_values"] += 1
        return str(value)
    if isinstance(value, str):
        if len(value) <= limits.string_chars:
            return value
        changes["clipped_strings"] += 1
        changes["omitted_string_chars"] += len(value) - limits.string_chars
        return value[:limits.string_chars] + "… [context text clipped]"
    if depth >= 6:
        changes["depth_limited_values"] += 1
        return "[nested value omitted from context]"
    if isinstance(value, Mapping):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= limits.collection_items:
                changes["omitted_mapping_fields"] += len(value) - index
                break
            key = str(key)
            if len(key) > 256:
                changes["omitted_mapping_fields"] += 1
                continue
            result[key] = _bounded(item, limits, changes, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > limits.collection_items:
            changes["omitted_collection_items"] += len(value) - limits.collection_items
        return [_bounded(item, limits, changes, depth + 1) for item in value[:limits.collection_items]]
    return _bounded(str(value), limits, changes, depth + 1)


def _compact_node(node: Mapping, limits: _Limits, changes: Counter) -> dict:
    # Stable identifiers and type labels are never shortened. An unexpectedly
    # huge identity therefore fails the total-size gate instead of changing IDs.
    return {
        "id": str(node["id"]),
        "labels": list(node.get("labels") or []),
        "properties": _bounded(node.get("properties") or {}, limits, changes),
    }


def _compact_edge(edge: Mapping, limits: _Limits, changes: Counter) -> dict:
    return {
        "start_id": str(edge["start_id"]),
        "end_id": str(edge["end_id"]),
        "type": str(edge.get("type") or "unknown"),
        "properties": _bounded(edge.get("properties") or {}, limits, changes),
    }


def _validation(checks: Any, limits: _Limits, changes: Counter) -> list:
    if not isinstance(checks, list):
        return []
    # The adapter returns after an accepted query, or appends a final failed
    # check when execution fails. Earlier attempts and candidate counts are
    # execution diagnostics, not retrieved evidence or missing graph results.
    # Excluding that history therefore does not mark the context as sampled.
    check = next((check for check in reversed(checks) if isinstance(check, Mapping)), None)
    if check is None:
        return []
    item = {key: check[key] for key in ("valid", "status") if key in check}
    reasons = check.get("reasons", [])
    if isinstance(reasons, list):
        compact_reasons = []
        for reason in reasons:
            # Neo4j syntax errors can include the candidate query after a
            # second colon. Keep the category and property/error code only.
            parts = str(reason).split(":", 2)
            compact_reasons.append(":".join(parts[:2]))
        item["reasons"] = _bounded(compact_reasons, limits, changes)
    return [item]


def _focal_gene(item: Mapping, node_index: dict) -> str | None:
    """Use the requested verified gene, never a hub guessed from an excerpt."""
    scope = item.get("requested_scope") or {}
    constraints = scope.get("constraints") or []
    resolved = item.get("resolved_entities") or []
    release = item.get("graph_version")
    identities = set()
    for index, constraint in enumerate(constraints):
        if constraint.get("entity_type") != "Gene" or constraint.get("property") not in {"id", "name"}:
            continue
        if (constraint.get("operator", "=") != "=" or constraint.get("owner_kind", "node") != "node"
                or constraint.get("relationship_type") or not isinstance(constraint.get("value"), str)):
            return None
        records = [record for record in resolved if record.get("constraint_index") == index]
        if records:
            if len(records) != 1:
                return None
            record = records[0]
            requested = record.get("requested") or {}
            if (record.get("state") != "resolved" or record.get("graph_version") != release
                    or record.get("entity_type") != "Gene" or not isinstance(record.get("id"), str)
                    or any(requested.get(key) != constraint.get(key) for key in ("entity_type", "property", "value"))
                    or requested.get("operator", "=") != constraint.get("operator", "=")):
                return None
            identifier = record["id"]
            if constraint["property"] == "id" and constraint["value"] != identifier:
                return None
        else:
            # An exact requested node ID also has a lossless proof in the full
            # returned node list. A name alone needs an actual resolver record.
            if constraint["property"] != "id":
                return None
            identifier = constraint["value"]
        if "Gene" not in (node_index.get(identifier, {}).get("labels") or []):
            return None
        identities.add(identifier)
    return next(iter(identities)) if len(identities) == 1 else None


def _interaction_totals(item: Mapping, edges: list, node_index: dict, coverage: Mapping) -> dict:
    """Counts from all retrieved physical interactions before any sampling.

    Incoming and outgoing endpoints form one partner set. Repeated experiments
    remain separate records, but neither the focal gene nor a self-interaction
    increases the partner count. Unverified identities do not acquire a total.
    """
    records = [edge for edge in edges if edge.get("type") == "PHYSICAL_INTERACTION"]
    focal = _focal_gene(item, node_index)
    result = {"focal_gene_id": focal, "unique_partner_genes": None,
              "count_scope": "all_retrieved_records_before_excerpt_selection",
              "complete_for_requested_scope": False}
    if focal is None:
        result["partner_count_state"] = "focal_gene_not_uniquely_verified"
        return result
    incident = [edge for edge in records if focal in {str(edge["start_id"]), str(edge["end_id"])}]
    partners = {str(edge[key]) for edge in incident for key in ("start_id", "end_id")} - {focal}
    verified = all("Gene" in (node_index.get(identifier, {}).get("labels") or []) for identifier in partners)
    complete = bool(item.get("status") in {"complete", "empty"} and item.get("truncated") is False
                    and coverage.get("query_scope", {}).get("complete_for_requested_scope") is True
                    and coverage.get("query_scope", {}).get("constraints") == (item.get("requested_scope") or {}).get("constraints")
                    and len(incident) == len(records) and verified)
    result.update(unique_partner_genes=len(partners) if verified else None,
                  unique_partner_entities=len(partners), focal_interaction_records=len(incident),
                  self_interaction_records=sum(str(edge["start_id"]) == str(edge["end_id"]) == focal for edge in incident),
                  partner_count_state="verified_from_full_retrieved_records" if verified else "partner_gene_labels_unverified",
                  complete_for_requested_scope=complete)
    return result


def _compact_step(item: Mapping, index: int, limits: _Limits, node_context: dict) -> dict:
    changes = Counter()
    entry = {key: _bounded(item[key], limits, changes) for key in _SUMMARY_FIELDS if key in item}
    entry["evidence_id"] = "G" + str(index + 1)
    entry["validation"] = _validation(item.get("validation"), limits, changes)
    # Record full query/source scope before sampling. Never clip scope filters or
    # turn excerpt omissions into a retrieval limit. The total-size gate remains.
    entry["evidence_coverage"] = coverage_for_answer(item)
    if isinstance(item.get("coloc_linkage"), Mapping):
        # The linkage helper already bounds record/reference examples and keeps
        # authoritative full counts. Do not reclip away role or signal identity.
        entry["coloc_linkage"] = deepcopy(item["coloc_linkage"])
    nodes, edges, rows = (item.get(key) or [] for key in ("nodes", "edges", "rows"))
    if not all(isinstance(values, list) for values in (nodes, edges, rows)):
        raise ValueError("invalid_evidence_collections")
    if any(not isinstance(node, Mapping) or "id" not in node for node in nodes):
        raise ValueError("invalid_evidence_node")
    if any(not isinstance(edge, Mapping) or "start_id" not in edge or "end_id" not in edge for edge in edges):
        raise ValueError("invalid_evidence_edge")

    sampled_nodes, dropped_nodes = _sample(nodes, limits.nodes, _node_type)
    sampled_edges, dropped_edges = _sample(edges, limits.edges, _edge_type)
    entry["nodes"] = [_compact_node(node, limits, changes) for node in sampled_nodes]
    entry["edges"] = [_compact_edge(edge, limits, changes) for edge in sampled_edges]
    entry["rows"] = [_bounded(row, limits, changes) for row in rows[:limits.rows]]
    full_node_index = {str(node["id"]): node for node in nodes}
    visible_ids = {node["id"] for node in entry["nodes"]}
    stubs, missing_endpoints, cross_step_endpoints = 0, 0, 0
    for edge in entry["edges"]:
        for endpoint in (edge["start_id"], edge["end_id"]):
            if endpoint in visible_ids:
                continue
            original = full_node_index.get(endpoint)
            stub_reason = "endpoint_not_selected"
            if original is None:
                original = node_context.get((str(item.get("graph_version", "")), endpoint))
                stub_reason = "endpoint_from_other_step" if original is not None else "endpoint_missing_from_evidence"
                cross_step_endpoints += original is not None
            original_properties = (original.get("properties") or {}) if original is not None else {}
            properties = {"id": endpoint}
            if isinstance(original_properties, Mapping) and "name" in original_properties:
                properties["name"] = _bounded(original_properties["name"], limits, changes)
            entry["nodes"].append({
                "id": endpoint,
                "labels": list(original.get("labels") or []) if original is not None else [],
                "properties": properties,
                "context_stub": True,
                "context_stub_reason": stub_reason,
            })
            visible_ids.add(endpoint)
            stubs += 1
            missing_endpoints += original is None

    for key, values in (("nodes", nodes), ("edges", edges), ("rows", rows)):
        entry[key + "_count"] = len(values)
    entry["evidence_totals"] = {
        "distinct_nodes_by_label": {label: len({str(n["id"]) for n in nodes if label in (n.get("labels") or [])})
            for label in sorted({label for n in nodes for label in (n.get("labels") or [])})},
        "relationships": {kind: {
            "records": sum(e.get("type") == kind for e in edges),
            "unique_start_entities": len({str(e["start_id"]) for e in edges if e.get("type") == kind}),
            "unique_end_entities": len({str(e["end_id"]) for e in edges if e.get("type") == kind})}
            for kind in sorted({e.get("type") or "unknown" for e in edges})},
    }
    from .signal_membership import summarize_signal_membership
    signal_membership = summarize_signal_membership(item, max_records=min(20, limits.edges))
    if signal_membership:
        entry["signal_membership_summary"] = signal_membership
    from .evidence_coverage import CELL_MEASUREMENTS
    cell_kinds = CELL_MEASUREMENTS.intersection(entry['evidence_totals']['relationships'])
    if cell_kinds:
        all_nodes = {identifier: node for (release, identifier), node in node_context.items()
                     if release == str(item.get('graph_version', ''))}
        all_nodes.update(full_node_index)
        for kind in sorted(cell_kinds):
            targets = {str(edge['end_id']) for edge in edges if edge.get('type') == kind}
            verified = all('anatomical_structure' in (all_nodes.get(identifier, {}).get('labels') or [])
                           for identifier in targets)
            entry['evidence_totals']['relationships'][kind].update(
                matched_cell_type_count=len(targets) if verified else None,
                matched_cell_type_count_state='verified_from_returned_endpoints' if verified else 'endpoint_types_unverified',
                matched_cell_type_count_scope='all_returned_records_before_excerpt_selection',
                source_study_total_cell_types=None,
                source_study_denominator_state='not_established_by_this_query',
                denominator_rule='Count cell types with matching recorded evidence. A complete PanKgraph search does not by itself '
                    'establish the total number of cell types profiled in the source study. Do not describe the returned count as '
                    'all profiled cell types or compute a fraction of the source study without a separately verified denominator.')
    if "PHYSICAL_INTERACTION" in entry["evidence_totals"]["relationships"]:
        all_nodes = {identifier: node for (release, identifier), node in node_context.items()
                     if release == str(item.get("graph_version", ""))}
        all_nodes.update(full_node_index)
        entry["evidence_totals"]["relationships"]["PHYSICAL_INTERACTION"].update(
            _interaction_totals(item, edges, all_nodes, entry["evidence_coverage"]))
    entry["context_counts"] = {
        "full_nodes_selected": len(sampled_nodes), "endpoint_stubs": stubs,
        "edges_selected": len(sampled_edges), "rows_selected": len(entry["rows"]),
    }
    entry["context_dropped"] = {
        "nodes_by_labels": dropped_nodes,
        "edges_by_type": dropped_edges,
        "rows": max(0, len(rows) - limits.rows),
    }
    if missing_endpoints:
        entry["context_missing_endpoint_nodes"] = missing_endpoints
    if cross_step_endpoints:
        entry["context_cross_step_endpoints"] = cross_step_endpoints
    if changes:
        entry["context_content_omissions"] = dict(sorted(changes.items()))
    entry["context_sampled"] = bool(dropped_nodes or dropped_edges or len(rows) > limits.rows or stubs or changes)
    entry["context_compaction"] = "standard" if limits is _NORMAL else "reduced"
    return entry


def compact_evidence(evidence: Mapping | list) -> list[dict]:
    """Return a JSON-safe synthesis view; do not modify the full evidence.

    Caps apply to full records per step. Minimal endpoint stubs may increase
    the node-array length, while the serialized total remains bounded.
    """
    if isinstance(evidence, Mapping):
        steps = list(evidence.values())
    elif isinstance(evidence, list):
        steps = evidence
    else:
        raise ValueError("invalid_evidence_shape")
    if any(not isinstance(step, Mapping) for step in steps):
        raise ValueError("invalid_evidence_step")
    node_context = {}
    for step in steps:
        for node in step.get("nodes") or []:
            if isinstance(node, Mapping) and "id" in node:
                node_context.setdefault((str(step.get("graph_version", "")), str(node["id"])), node)

    def build(limits):
        result = [_compact_step(item, index, limits, node_context) for index, item in enumerate(steps)]
        size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
        return result, size

    result, size = build(_NORMAL)
    if size > TARGET_BYTES:
        result, size = build(_REDUCED)
    if size > MAX_BYTES:
        raise ValueError("evidence_context_too_large")
    return result


def scientific_excerpt(compact):
    """Keep trace diagnostics in application metadata, outside scientific prose.

    Explicit excerpt scope remains, so selected records cannot imply complete
    tabular coverage. This does not change full evidence or graph display data.
    """
    def clean(value):
        if isinstance(value, dict):
            return {k:clean(v) for k,v in value.items() if not k.startswith('context_') and k not in {'endpoint_stub', 'endpoint_stub_reason'}}
        if isinstance(value,list): return [clean(v) for v in value]
        return value
    result=[]
    for item in compact:
        entry=clean(item)
        entry['answer_evidence_scope']={
            'individual_records_are_selected_examples':bool(item.get('context_sampled')),
            'authoritative_totals':'Use evidence_totals and donor_summary. For physical interactions, use unique_partner_genes computed from the union of both endpoints with the requested focal gene excluded. Never count selected example nodes or add unique_start_entities and unique_end_entities to invent a partner total.',
            'retrieval_scope':'Use evidence_coverage.query_scope; selected examples do not make a verified complete search incomplete.',
            'source_comparison':'Use evidence_coverage.source_comparisons; query and display subsets never redefine the source analysis comparison.',
            'cell_type_denominator':'Use matched_cell_type_count for cell types with returned evidence. Completeness means all matching PanKgraph records were checked; it does not make this count the source-study total or change a recorded one-versus-rest comparison.',
            'metadata_rule':'Unshown fields and identity-only node examples do not establish missing metadata in Neo4j. Do not describe excerpt omissions or placeholders as incomplete database records. Only explicit source-backed missing values can support a missing-metadata statement.',
            'graph_display':'Not described by this synthesis excerpt; do not infer visible or omitted graph records.'}
        result.append(entry)
    return result
