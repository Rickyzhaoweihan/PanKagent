"""Selected query records adapted to the existing graph/formatter contract."""
import copy
import json
import re


def graph_union(graphs):
    nodes, edges = {}, {}
    for graph in graphs:
        for kind, target in (("nodes", nodes), ("edges", edges)):
            for record in graph[kind]:
                target[str(record["id"])] = record
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def summaries(queries):
    return [{"query_id": key, "pattern": value["pattern"], "status": value["status"],
             "question": value.get("standardized_question"),
             "node_count": len(value["graph"]["nodes"]), "edge_count": len(value["graph"]["edges"]),
             "graph_sample": {k: value["graph"][k][:6] for k in ("nodes", "edges")},
             "next_offset": value.get("next_offset"), "provenance": value.get("provenance"),
             **({"condition_contract": copy.deepcopy(value["condition_contract"])}
                if "condition_contract" in value else {})}
            for key, value in queries.items()]


def handoff(queries, selections, args, base=None):
    """Union explicitly selected records; preserve parallel edges by record ID."""
    selected = [(s, queries[s["query_id"]]) for s in selections]
    required = [q for s, q in selected if s.get("purpose", "requested") == "requested"]
    status = "partial" if any(q["status"] == "partial" for q in required) else (
        "complete" if any(q["status"] == "complete" for q in required) else "empty")
    graph = graph_union(([base["graph"]] if base else []) + [q["graph"] for _, q in selected])
    # Apply the existing graph byte allowance to the combined handoff as well.
    omitted = 0
    if len(json.dumps(graph, default=str).encode()) > 2_000_000:
        kept = copy.deepcopy(base["graph"] if base else {"nodes": [], "edges": []})
        known = {str(n["id"]): n for n in graph["nodes"]}
        for edge in graph["edges"]:
            addition = {"nodes": [known[str(k)] for k in (edge.get("source"), edge.get("target")) if str(k) in known], "edges": [edge]}
            trial = graph_union([kept, addition])
            if len(json.dumps(trial, default=str).encode()) <= 2_000_000:
                kept = trial
            else:
                omitted += 1
        for node in graph["nodes"]:
            trial = graph_union([kept, {"nodes": [node], "edges": []}])
            if len(json.dumps(trial, default=str).encode()) <= 2_000_000:
                kept = trial
            elif str(node["id"]) not in {str(n["id"]) for n in kept["nodes"]}:
                omitted += 1
        graph = kept
        # Existing truncation status describes delivered requested records.
        requested_graph = graph_union([q["graph"] for q in required])
        retained = {kind: {str(r["id"]) for r in graph[kind]} for kind in ("nodes", "edges")}
        if any(str(r["id"]) not in retained[kind] for kind in retained for r in requested_graph[kind]):
            status = "partial"
    pattern = {"nodes": [], "edges": []}
    statements, parameters = [], {}
    context_audit = []
    for selection, q in selected:
        prefix = selection["query_id"] + "_"
        purpose = selection.get("purpose", "requested")
        if purpose == "context":
            context_audit.append({"condition": "Optional supporting context, not a qualifying condition",
                                  "binding": selection["query_id"] + ": " + str(q.get("standardized_question")) +
                                             "; retrieval " + q["status"]})
        for kind in (("nodes", "edges") if purpose == "requested" else ()):
            for item in q["pattern"][kind]:
                item = copy.deepcopy(item)
                item["key"] = prefix + item["key"] if len(selected) > 1 else item["key"]
                if kind == "edges" and len(selected) > 1:
                    item["source"], item["target"] = prefix + item["source"], prefix + item["target"]
                pattern[kind].append(item)
        if len(selected) == 1:
            statements.append(q["cypher"]); parameters.update(q["parameters"])
        else:
            statements.append(re.sub(r"\$([A-Za-z][A-Za-z0-9_]*)", lambda m: "$" + prefix + m[1], q["cypher"]))
            parameters.update({prefix + k: v for k, v in q["parameters"].items()})
    result = {**(base or {}), **args, "graph": graph, "metapath": pattern,
              "cypher": "\nUNION ALL\n".join(statements), "parameters": parameters,
              "retrieval_status": status, "selected_queries": copy.deepcopy(selections),
              "provenance": {"queries": [{"query_id": s["query_id"], "purpose": s.get("purpose", "requested"),
                                             "source": q.get("provenance")} for s, q in selected]},
              "omitted_records": omitted}
    # The unchanged formatter reads requested_scope from metapath and audit text.
    # Keep optional expansion filters out of that scope while disclosing their role.
    result["condition_audit"] = [a for a in result.get("condition_audit", [])
                                 if a.get("condition") not in ("Optional supporting context, not a qualifying condition", "Graph delivery allowance")] + context_audit
    if omitted:
        result["condition_audit"].append({"condition": "Graph delivery allowance",
            "binding": str(omitted) + " records omitted by the existing graph byte allowance; delivered context may be incomplete."})
    if status == "partial" and result.get("status") in ("complete", "empty"):
        result["status"] = "partial"
    return result
