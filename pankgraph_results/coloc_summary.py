"""Exact-record evidence adapter for the shared PanKagent answer pipeline.

This module does not contain a model client or synthesis policy. It preserves
recorded observations, source boundaries and denominators for the existing
versioned answer router and evidence compactor.
"""
from copy import deepcopy
import json

from pankagent_vnext.answer_router import ROUTER_VERSION
from pankagent_vnext.llm import STYLE_VERSION

from .store import digest

SUMMARY_VERSION = "coloc-summary-2"
TOP_MEMBERS = 5


def scientific_snapshot(detail):
    """Exclude observation clocks and presentation URLs from durable identity."""
    snapshot = {key: deepcopy(detail[key]) for key in (
        "record", "graph_version", "variants", "coverage", "coordinate_build", "status", "notices")}
    snapshot["variants"].sort(key=lambda item: item["id"])
    snapshot["sources"] = [{key: deepcopy(value) for key, value in source.items()
        if key not in {"checked_at", "download_url"}} for source in detail["sources"]]
    snapshot["sources"].sort(key=lambda item: json.dumps(item, sort_keys=True))
    # Renderer aliases and geometry are not evidence. The projected records
    # retain the original database labels, relationship types and properties.
    graph = detail["graph"]
    snapshot["graph"] = {
        "nodes": sorted([{"id": node["~id"], "labels": node["~labels"],
            "properties": deepcopy(node["~properties"])} for node in graph["nodes"]], key=lambda item: item["id"]),
        "edges": sorted([{"id": edge["~id"], "start_id": edge["~start"], "end_id": edge["~end"],
            "type": edge["~type"], "properties": deepcopy(edge["~properties"])}
            for edge in graph["edges"]], key=lambda item: item["id"]),
    }
    return snapshot


def answer_configuration(gateway, settings):
    """Identify the actual shared gateway's verified bundle, style and model."""
    router = gateway.answer_router
    return {"model": settings.model, "style_version": STYLE_VERSION,
        "router_version": ROUTER_VERSION, "bundle_version": router.manifest["bundle_version"],
        "bundle_sha256": router.bundle_hash, "router_max_chars": router.max_chars}


def summary_source(detail, configuration):
    snapshot = scientific_snapshot(detail)
    record, coverage = snapshot["record"], snapshot["coverage"]
    scope = ("Only this recorded gene, disease, dataset and exact QTL/GWAS signal pair are in scope. "
        "The original coloc nsnp is the analysis denominator; membership counts describe the returned credible sets. "
        "Shared membership is not a recomputed colocalization result. Source-file members are tabular evidence, "
        "not additional Neo4j relationships. No LD, effect harmonization or causal mechanism was computed. "
        "Lead membership facts use all returned members; sampled graph edges and top-row excerpts cannot establish membership or absence.")
    question = (f"Summarize this recorded colocalization comparison for {record['gene_name']} "
        f"and {record['disease_name']}, dataset {record['dataset']}. "
        "Use at most 100 prose words plus exactly one table with two study rows: GWAS and QTL. "
        "Use only the columns Study, Members (returned/expected), Recorded lead, and Lead PIP. "
        "State the recorded H4, source/tissue, original nsnp, shared-member count, and main evidence caveat briefly. "
        "Do not add headings, a H0–H4 table, or full signal IDs; these are already visualized. "
        "Use the explicit recorded_lead_membership facts for each lead's membership and PIP in either study. "
        "Never infer other-study lead membership from sampled graph edges or top-row excerpts. "
        "Cross-study lead-membership commentary may be omitted for conciseness. "
        "A null membership means unknown because retrieval is incomplete; only false establishes absence from a complete returned set. "
        "Keep nsnp separate from set sizes; distinguish support for a shared signal from proof of a causal gene, variant or mechanism.")

    def step(step_id, rows, *, nodes=None, edges=None, complete=True, purpose="context"):
        return {"step_id": step_id, "graph_version": snapshot["graph_version"],
            "status": "complete" if complete else "partial", "purpose": purpose,
            "requested_scope": {"description": scope, "constraints": []}, "truncated": False,
            "nodes": nodes or [], "edges": edges or [], "rows": rows}

    variant_index = {variant["id"]: variant for variant in snapshot["variants"]}
    lead_facts = []
    for variant_id in sorted(set(record["gwas_leads"]) | set(record["qtl_leads"])):
        variant = variant_index.get(variant_id, {})
        fact = {"variant_id": variant_id,
            "gwas_lead": variant_id in record["gwas_leads"],
            "qtl_lead": variant_id in record["qtl_leads"]}
        for role in ("gwas", "qtl"):
            association = variant.get(role)
            fact["in_returned_" + role] = association is not None
            fact[role + "_member"] = (True if association is not None
                else False if coverage[role + "_complete"] else None)
            fact[role + "_pip"] = association.get("pip") if association is not None else None
        lead_facts.append(fact)
    graph = snapshot["graph"]
    steps = [step("recorded_coloc", [
        {"kind": "recorded_colocalization", **record},
        {"kind": "credible_set_coverage", **coverage},
        {"kind": "recorded_lead_membership", "basis": "all returned members, independent of graph display and top-row selection",
            "gwas_complete": coverage["gwas_complete"], "qtl_complete": coverage["qtl_complete"],
            "membership_semantics": "true: observed member; false: absent from complete returned set; null: not observed in incomplete returned set",
            "leads": lead_facts},
        {"kind": "record_limitations", "notices": snapshot["notices"],
            "coordinate_build": snapshot["coordinate_build"]}],
        nodes=graph["nodes"], edges=graph["edges"],
        complete=snapshot["status"] == "ready", purpose="primary")]
    for role in ("gwas", "qtl"):
        members = [variant for variant in snapshot["variants"] if variant.get(role) is not None]
        # Highest recorded PIP first, then actual nominal P and stable ID.
        # Null PIP/P stays null; neither value is inferred from the other.
        ranked = sorted(members, key=lambda item: (
            item[role].get("pip") is None, -(item[role].get("pip") or 0),
            item[role].get("nominal_p") is None,
            item[role].get("nominal_p") if item[role].get("nominal_p") is not None else 1,
            item["id"]))
        rows = [{"kind": "member_summary", "study": role, "scope": "credible_set",
            "returned": coverage[role + "_count"], "expected": coverage[role + "_expected"],
            "complete": coverage[role + "_complete"], "shared_count": coverage["shared_count"],
            "pip_available_count": sum(row[role].get("pip") is not None for row in members),
            "nominal_p_available_count": sum(row[role].get("nominal_p") is not None for row in members),
            "top_rows_shown": min(TOP_MEMBERS, len(ranked)),
            "members_not_shown_in_top_rows": max(0, len(members) - TOP_MEMBERS),
            "selection": "highest recorded PIP; ties by nominal P then variant ID",
            "recorded_leads": record[role + "_leads"]}]
        rows.extend({"kind": "recorded_member", "study": role, "variant_id": row["id"],
            "recorded_lead": row["id"] in record[role + "_leads"],
            "shared_member": row.get("gwas") is not None and row.get("qtl") is not None,
            "chromosome": row["chromosome"], "position": row["position"],
            "coordinate_source": row["coordinate_source"], **row[role]} for row in ranked[:TOP_MEMBERS])
        steps.append(step(role + "_credible_set", rows, complete=coverage[role + "_complete"]))
    steps.append(step("source_provenance", [{"kind": "retrieved_source", **source}
        for source in snapshot["sources"]], complete=all(source["status"] == "available" for source in snapshot["sources"])))
    evidence = {"graph_version": snapshot["graph_version"], **graph, "steps": steps,
        "completeness": "complete" if coverage["complete"] else "partial",
        "truncated": bool(coverage.get("omitted_count")), "scope_note": scope}
    return {"kind": "coloc", "record_id": record["id"], "question": question,
        "evidence": evidence, "scientific_snapshot": snapshot, "snapshot_sha256": digest(snapshot),
        "answer_configuration": configuration, "summary_version": SUMMARY_VERSION}


def summary_identity(source):
    return {"kind": "coloc", "record_id": source["record_id"],
        "scientific_snapshot_sha256": source["snapshot_sha256"],
        "answer_configuration": source["answer_configuration"], "summary_version": source["summary_version"]}
