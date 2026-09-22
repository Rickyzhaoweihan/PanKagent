"""Deterministic public records for retrieved ``SIGNAL_COLOC_WITH`` edges.

The graph relationship is the unit of evidence.  This module does not query the
graph, resolve entities, or infer tissue from a dataset name.  It only converts
already retrieved, bounded evidence into stable records that can be attached to
the public evidence object and reused by deterministic answer facts.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from pathlib import Path
import re


VERSION = "colocalization-records-v1"
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
RELATION = "SIGNAL_COLOC_WITH"
MAX_RECORDS = 5000
MAX_EDGE_SCAN = 5000
MAX_NODE_SCAN = 4096
MAX_TEXT_CHARS = 512
MAX_LEAD_TEXT_CHARS = 4096
MAX_LEAD_VARIANTS = 25

# PanKgraph_08_04 registers no tissue property on SIGNAL_COLOC_WITH.  Keep this
# explicit and versioned.  A future release must add a separately reviewed
# mapping rather than deriving tissue from ``coloc_dataset`` text.
TISSUE_SCHEMA_RELEASE = "PanKgraph_08_04"
REGISTERED_TISSUE_PROPERTIES: tuple[str, ...] = ()

_LEADS = re.compile(r"\s*rs\d+(?:\s*[,;| ]\s*rs\d+)*\s*")
_LEAD = re.compile(r"rs\d+")


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_text(value: object, *, maximum: int = MAX_TEXT_CHARS) -> str | None:
    if isinstance(value, str) and value and len(value) <= maximum:
        return value
    return None


def _text_state(value: object) -> dict:
    if value is None or value == "":
        return {"state": "not_recorded", "value": None}
    safe = _safe_text(value)
    if safe is None:
        return {"state": "unsupported_value", "value": None}
    return {"state": "recorded", "value": safe}


def _number_state(value: object) -> dict:
    if value is None or value == "":
        return {"state": "not_recorded", "value": None}
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {"state": "unsupported_value", "value": None}
    if not math.isfinite(float(value)):
        return {"state": "unsupported_value", "value": None}
    return {"state": "recorded", "value": value}


def _lead_state(value: object) -> tuple[dict, list[str] | None]:
    if value is None or value == "":
        return {"state": "not_recorded", "variant_ids": None}, None
    ids: list[str] | None = None
    if (
        isinstance(value, str)
        and len(value) <= MAX_LEAD_TEXT_CHARS
        and _LEADS.fullmatch(value)
    ):
        ids = sorted(set(re.split(r"\s*[,;| ]\s*", value.strip())))
    elif (
        isinstance(value, (list, tuple))
        and 0 < len(value) <= MAX_LEAD_VARIANTS
        and all(isinstance(item, str) and _LEAD.fullmatch(item) for item in value)
    ):
        ids = sorted(set(value))
    if ids is None:
        return {"state": "unsupported_value", "variant_ids": None}, None
    return {"state": "recorded", "variant_ids": ids}, ids


def _labels_by_id(nodes: object) -> tuple[dict[str, list[str]], bool]:
    labels: defaultdict[str, set[str]] = defaultdict(set)
    if not isinstance(nodes, (list, tuple)):
        return {}, False
    complete = len(nodes) <= MAX_NODE_SCAN
    for node in nodes[:MAX_NODE_SCAN]:
        if not isinstance(node, Mapping):
            continue
        identifier = _safe_text(node.get("id"))
        raw_labels = node.get("labels")
        if identifier is None or not isinstance(raw_labels, (list, tuple)):
            continue
        for label in raw_labels[:32]:
            safe = _safe_text(label, maximum=128)
            if safe is not None:
                labels[identifier].add(safe)
    return {identifier: sorted(values) for identifier, values in labels.items()}, complete


def _tissue_state(graph_version: str | None) -> dict:
    return {
        "state": "unavailable",
        "value": None,
        "reason": "no_registered_tissue_property_on_SIGNAL_COLOC_WITH",
        "registered_schema_release": TISSUE_SCHEMA_RELEASE,
        "evidence_graph_version": graph_version,
        "registered_tissue_properties": list(REGISTERED_TISSUE_PROPERTIES),
        "inferred_from_coloc_dataset": False,
    }


def _prototype(edge: Mapping, labels: Mapping[str, list[str]], graph_version: str | None) -> dict:
    props = edge.get("properties")
    props = props if isinstance(props, Mapping) else {}
    source_id = _safe_text(edge.get("start_id"))
    target_id = _safe_text(edge.get("end_id"))
    source_labels = list(labels.get(source_id, ())) if source_id is not None else []
    target_labels = list(labels.get(target_id, ())) if target_id is not None else []
    source_is_gene = "Gene" in source_labels
    target_is_disease = "disease" in target_labels
    gwas_lead_state, gwas_leads = _lead_state(props.get("gwas_lead_vars"))
    qtl_lead_state, qtl_leads = _lead_state(props.get("qtl_lead_vars"))
    shared_leads = (sorted(set(gwas_leads) & set(qtl_leads))
                    if gwas_leads is not None and qtl_leads is not None else None)
    return {
        "relation": RELATION,
        "source_id": source_id,
        "target_id": target_id,
        "typed_endpoints_verified": source_is_gene and target_is_disease,
        "endpoint_verification": {
            "state": "verified" if source_is_gene and target_is_disease else "failed",
            "typed_endpoints_verified": source_is_gene and target_is_disease,
            "expected_source_type": "Gene",
            "expected_target_type": "disease",
            "source_labels": source_labels,
            "target_labels": target_labels,
        },
        "recorded_gwas_signal_id": _text_state(props.get("gwas_signal_id")),
        "recorded_qtl_signal_id": _text_state(props.get("qtl_signal_id")),
        "recorded_gwas_lead_variants": gwas_lead_state,
        "recorded_qtl_lead_variants": qtl_lead_state,
        "gwas_lead_variant_ids": gwas_leads,
        "qtl_lead_variant_ids": qtl_leads,
        "shared_recorded_lead_variant_ids": shared_leads,
        "same_complete_lead_set": (
            gwas_leads == qtl_leads
            if gwas_leads is not None and qtl_leads is not None else None
        ),
        "recorded_coloc_dataset": _text_state(props.get("coloc_dataset")),
        "recorded_source": _text_state(props.get("data_source")),
        "recorded_source_version": _text_state(props.get("data_version")),
        "recorded_gwas_locus_name": _text_state(props.get("gwas_locus_name")),
        "recorded_qtl_locus_name": _text_state(props.get("qtl_locus_name")),
        "recorded_pp_h4_abf": _number_state(props.get("pp_h4_abf")),
        "recorded_tissue_context": _tissue_state(graph_version),
    }


def _recorded_values(records: Sequence[Mapping], field: str) -> list[str]:
    values = []
    for record in records:
        item = record.get(field)
        if isinstance(item, Mapping) and item.get("state") == "recorded":
            value = item.get("value")
            if isinstance(value, str) and value:
                values.append(value)
    return values


def _completeness(evidence: Mapping, *, edge_scan_complete: bool,
                  node_scan_complete: bool, omitted_record_count: int) -> dict:
    reasons = []
    status = evidence.get("status")
    if status not in {"complete", "empty"}:
        reasons.append("evidence_status:" + str(status or "unknown"))
    if evidence.get("truncated") is not False:
        reasons.append("retrieval_truncated_or_unknown")
    if evidence.get("retrieval_completeness") in {"partial", "failed", "truncated"}:
        reasons.append("retrieval_completeness:" + str(evidence["retrieval_completeness"]))
    execution = evidence.get("retrieval_execution")
    if isinstance(execution, Mapping):
        if execution.get("completed") is False:
            reasons.append("retrieval_not_completed")
        if execution.get("cursor_exhausted") is False:
            reasons.append("retrieval_cursor_not_exhausted")
    coverage = evidence.get("evidence_coverage")
    if isinstance(coverage, Mapping):
        query_scope = coverage.get("query_scope")
        if (isinstance(query_scope, Mapping)
                and query_scope.get("complete_for_requested_scope") is False):
            reasons.append("requested_scope_not_complete")
    if not edge_scan_complete:
        reasons.append("edge_scan_limit")
    if not node_scan_complete:
        reasons.append("node_scan_limit")
    if omitted_record_count:
        reasons.append("record_output_limit")
    reasons = sorted(set(reasons))
    return {
        "state": "complete" if not reasons else "partial",
        "complete_for_executed_scope": not reasons,
        "complete_for_retrieved_signal_coloc_edges": (
            edge_scan_complete and omitted_record_count == 0
        ),
        "partial_reasons": reasons,
        "interpretation": (
            "Completeness applies only to the executed graph query and retained records; "
            "it is not a claim that every source-study colocalization has been indexed."
        ),
    }


def derive_colocalization_records(evidence: Mapping, *, max_records: int = MAX_RECORDS) -> dict | None:
    """Return stable linked records and counts for retrieved colocalizations.

    ``None`` means the supplied evidence contained no retrieved
    ``SIGNAL_COLOC_WITH`` relationship.  At most ``MAX_EDGE_SCAN`` input edges
    and ``MAX_RECORDS`` output records are considered, and any cap is disclosed
    as partial metadata rather than silently treated as complete.
    """
    if not isinstance(evidence, Mapping):
        raise TypeError("evidence must be a mapping")
    if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= MAX_RECORDS:
        raise ValueError(f"max_records must be an integer from 1 through {MAX_RECORDS}")

    raw_edges = evidence.get("edges")
    if not isinstance(raw_edges, (list, tuple)):
        return None
    edge_scan_complete = len(raw_edges) <= MAX_EDGE_SCAN
    graph_version = _safe_text(evidence.get("graph_version"), maximum=128)
    labels, node_scan_complete = _labels_by_id(evidence.get("nodes"))
    prototypes = [
        _prototype(edge, labels, graph_version)
        for edge in raw_edges[:MAX_EDGE_SCAN]
        if isinstance(edge, Mapping) and edge.get("type") == RELATION
    ]
    if not prototypes:
        return None

    # Canonical sorting makes record order independent of Neo4j row order.
    prototypes.sort(key=lambda item: (
        (item["recorded_gwas_signal_id"].get("value") or ""),
        (item["recorded_qtl_signal_id"].get("value") or ""),
        (item["source_id"] or ""),
        (item["target_id"] or ""),
        _sha256(item),
    ))
    occurrences: defaultdict[str, int] = defaultdict(int)
    records = []
    for prototype in prototypes:
        payload_sha = _sha256(prototype)
        occurrences[payload_sha] += 1
        ordinal = occurrences[payload_sha]
        record_sha = _sha256({
            "version": VERSION,
            "relationship_payload_sha256": payload_sha,
            "duplicate_ordinal": ordinal,
        })
        records.append({
            "record_link": "urn:pankgraph:signal-coloc-with:sha256:" + record_sha,
            "record_sha256": record_sha,
            "relationship_payload_sha256": payload_sha,
            "duplicate_ordinal": ordinal,
            **prototype,
        })

    full_record_count = len(records)
    retained = records[:max_records]
    omitted = full_record_count - len(retained)
    gwas_signals = _recorded_values(records, "recorded_gwas_signal_id")
    qtl_signals = _recorded_values(records, "recorded_qtl_signal_id")
    pairs = [
        (gwas["value"], qtl["value"])
        for record in records
        for gwas, qtl in [(
            record["recorded_gwas_signal_id"], record["recorded_qtl_signal_id"]
        )]
        if gwas.get("state") == qtl.get("state") == "recorded"
    ]
    counts = {
        "counting_unit": "retrieved_SIGNAL_COLOC_WITH_relationship_records",
        "record_count": full_record_count,
        "retained_record_count": len(retained),
        "omitted_record_count": omitted,
        "gwas_signal_reference_count": len(gwas_signals),
        "distinct_recorded_gwas_signal_count": len(set(gwas_signals)),
        "recorded_gwas_signal_ids": sorted(set(gwas_signals)),
        "unresolved_gwas_signal_reference_count": full_record_count - len(gwas_signals),
        "qtl_signal_reference_count": len(qtl_signals),
        "distinct_recorded_qtl_signal_count": len(set(qtl_signals)),
        "recorded_qtl_signal_ids": sorted(set(qtl_signals)),
        "unresolved_qtl_signal_reference_count": full_record_count - len(qtl_signals),
        "distinct_recorded_signal_pair_count": len(set(pairs)),
        "unresolved_signal_pair_reference_count": full_record_count - len(pairs),
        "typed_endpoints_verified_record_count": sum(
            bool(record["endpoint_verification"]["typed_endpoints_verified"])
            for record in records
        ),
        "tissue_context_recorded_count": 0,
        "tissue_context_unavailable_count": full_record_count,
        "interpretation": (
            "Signal references are counted once per retrieved colocalization relationship. "
            "Distinct counts deduplicate identifiers across records."
        ),
    }
    return {
        "version": VERSION,
        "graph_version": graph_version,
        "records": retained,
        "record_links": [record["record_link"] for record in retained],
        "counts": counts,
        "derivation": {
            "input_edge_count": len(raw_edges),
            "scanned_edge_count": min(len(raw_edges), MAX_EDGE_SCAN),
            "signal_coloc_edge_count_in_scanned_input": full_record_count,
            "unscanned_input_edge_count": max(0, len(raw_edges) - MAX_EDGE_SCAN),
            "edge_scan_limit": MAX_EDGE_SCAN,
            "record_output_limit": max_records,
        },
        "completeness": _completeness(
            evidence,
            edge_scan_complete=edge_scan_complete,
            node_scan_complete=node_scan_complete,
            omitted_record_count=omitted,
        ),
        "interpretation": (
            "Each item is one retrieved SIGNAL_COLOC_WITH relationship. Tissue is "
            "unavailable because this schema has no registered tissue property; the "
            "dataset label is never used as a tissue proxy. Colocalization does not "
            "establish a biological mechanism."
        ),
    }


__all__ = ["VERSION", "DIGEST", "derive_colocalization_records"]
