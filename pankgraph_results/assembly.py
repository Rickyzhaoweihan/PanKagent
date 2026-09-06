"""Legacy renderer contract plus explicit versioned scientific/display status."""


def assemble(source, evidence, presentation):
    nodes = presentation["combined_query_result"]["nodes"]
    edges = presentation["combined_query_result"]["edges"]
    full = presentation["full_evidence"]
    shown, total = len(nodes), full["node_count"]
    notice = f"Showing {shown} of {total} retrieved nodes and {len(edges)} of {full['edge_count']} relationships."
    if shown < total or len(edges) < full["edge_count"]:
        notice += " The full retrieved evidence is retained separately from this display."
    if evidence.get("truncated"):
        notice += " Graph retrieval reached its safety limit and is incomplete."
    if evidence.get("scope_note"):
        notice += " " + evidence["scope_note"]
    return {**presentation, "version": 1, "status": "ready", "question": source["question"],
        "title": source["question"], "answer": source.get("answer", ""),
        "literature": source.get("literature", []), "resources_tabs": {}, "evidence": evidence,
        "completeness": evidence.get("completeness", "unknown"),
        "display": {**presentation["display"], "notice": notice},
        "source": {k: source[k] for k in ("kind", "run_id", "session_id", "phase", "template_id", "retrieval") if k in source},
        "component_status": {"graph": "available" if nodes or edges else "empty", "layout": presentation["layout"]["status"]}}
