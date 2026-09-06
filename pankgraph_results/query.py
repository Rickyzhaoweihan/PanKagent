"""Typed conventional searches over the same release as the Cypher generator.

Only this module owns Cypher templates. Presentation of an agent run never
passes through it, and callers cannot supply SQL or an arbitrary Cypher string.
"""
import asyncio
import json
import re
import time

from pankagent_vnext.graph import GraphAdapter, GraphValidationError, validate_cypher

TEMPLATES = {
    "qtl_by_gene": {"gene_id"},
    "qtl_by_variant_gene": {"gene_id", "variant_id"},
    "qtl_by_variant": {"variant_id"},
    "gwas_by_variant": {"variant_id"},
    "coloc_by_gene": {"gene_id"},
    "expression_by_gene": {"gene_id"},
}
PARAMETERS = {"gene_id", "variant_id", "disease_id", "credible_set_id", "lead_variant_id", "data_source", "cell_id"}
ALLOWED_PARAMETERS = {
    "qtl_by_gene": {"gene_id", "credible_set_id", "data_source"},
    "qtl_by_variant_gene": {"gene_id", "variant_id", "credible_set_id", "lead_variant_id", "data_source"},
    "qtl_by_variant": {"variant_id", "gene_id", "credible_set_id", "lead_variant_id", "data_source"},
    "gwas_by_variant": {"variant_id", "disease_id", "credible_set_id", "lead_variant_id", "data_source"},
    "coloc_by_gene": {"gene_id", "disease_id", "data_source"},
    "expression_by_gene": {"gene_id", "cell_id", "data_source"},
}
MAX_COLOC_SIGNALS = 25
MAX_LEADS_PER_SIGNAL = 25
# RL08_04 corpus identities, verified by bounded relationship-property reads.
# COLOC's data_source identifies the coloc analysis, not the source of QTL rows.
COLOC_QTL_CONTEXT = {
    "t1d_eQTL-inspire_coloc": ("INSPIRE; SusieR", "UBERON_0000006"),
    "t1d_eQTL-gtex_coloc": ("GTEx; SusieR", "UBERON_0001264"),
    "t1d_sQTL-gtex_coloc": ("splicing; GTEx", "UBERON_0001264"),
    "t1d_exonQTL-inspire_coloc": ("exon; INSPIRE", "UBERON_0000006"),
}
LEAD_SCOPE_NOTE = ("The graph shows the selected credible-set lead variant. Statistics for the searched "
                   "variant require the linked fine-mapping table; lead-edge statistics do not describe the searched nonlead variant.")


def parameters_for(template_id, supplied):
    if template_id not in TEMPLATES or not isinstance(supplied, dict):
        raise ValueError("unknown_template")
    if set(supplied) - PARAMETERS:
        raise ValueError("unknown_template_parameter")
    result = {}
    for key, value in supplied.items():
        if value is None or value == "":
            continue
        if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise ValueError("invalid_template_parameter")
        if value.strip():
            result[key] = value.strip()
    if set(result) - ALLOWED_PARAMETERS[template_id]:
        raise ValueError("unsupported_template_parameter")
    if not TEMPLATES[template_id].issubset(result):
        raise ValueError("missing_template_parameter")
    if template_id in {"gwas_by_variant", "coloc_by_gene"}:
        # This is the disease explicitly selected by the existing T1D templates.
        result.setdefault("disease_id", "MONDO_0005147")
    if result.get("lead_variant_id") and result["lead_variant_id"] != result.get("variant_id") and not result.get("credible_set_id"):
        raise ValueError("credible_set_required_for_lead_substitution")
    return result


def compile_template(template_id, supplied):
    params = parameters_for(template_id, supplied)
    if template_id.startswith("qtl_"):
        query = "MATCH (v:sequence_variant)-[r:PART_OF_QTL_SIGNAL]->(g:Gene)"
        where = []
        if "gene_id" in params:
            where.append("g.id=$gene_id")
        if "variant_id" in params:
            params["graph_variant_id"] = params.get("lead_variant_id", params["variant_id"])
            where.append("v.id=$graph_variant_id")
        if "credible_set_id" in params:
            where.append("r.credible_set=$credible_set_id")
        if "data_source" in params:
            where.append("r.data_source=$data_source")
        query += " WHERE " + " AND ".join(where) + " RETURN v,r,g"
    elif template_id == "gwas_by_variant":
        params["graph_variant_id"] = params.get("lead_variant_id", params["variant_id"])
        query = "MATCH (v:sequence_variant {id:$graph_variant_id})-[r:PART_OF_GWAS_SIGNAL]->(d:disease {id:$disease_id})"
        where = []
        if "credible_set_id" in params:
            where.append("r.credible_set_id=$credible_set_id")
        if "data_source" in params:
            where.append("r.data_source=$data_source")
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " RETURN v,r,d"
    elif template_id == "coloc_by_gene":
        # Lowercase properties and Gene label belong to the verified RL release.
        query = "MATCH (g:Gene {id:$gene_id})-[r:SIGNAL_COLOC_WITH]->(d:disease {id:$disease_id})"
        if "data_source" in params:
            query += " WHERE r.data_source=$data_source"
        query += " RETURN g,r,d"
    else:
        query = "MATCH (g:Gene {id:$gene_id})-[r:GENE_DETECTED_IN|GENE_ENRICHED_IN|MARKER_GENE_OF|GENE_ACTIVITY_SCORE_IN|T1D_DEG_IN]->(c:anatomical_structure)"
        where = []
        if "cell_id" in params:
            where.append("c.id=$cell_id")
        if "data_source" in params:
            where.append("r.data_source=$data_source")
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " RETURN g,r,c"
    return query, params


def _lead_ids(value):
    """Strict literal ID lists; text containing an rsID is not a verified list."""
    if isinstance(value, str):
        if len(value) > 4096 or not re.fullmatch(r"\s*rs\d+(?:\s*[,;| ]\s*rs\d+)*\s*", value):
            return None
        values = re.split(r"\s*[,;| ]\s*", value.strip())
    elif isinstance(value, list):
        values = value
    else:
        return None
    if not values or any(not isinstance(item, str) or not re.fullmatch(r"rs\d+", item) for item in values):
        return None
    values = sorted(set(values))
    return values if len(values) <= MAX_LEADS_PER_SIGNAL else None


def _gwas_credible_set(signal):
    if not isinstance(signal, str) or not signal or len(signal) > 512:
        return None
    if signal.endswith("__selected"):
        # The verified release decorates COLOC IDs, while GWAS membership stores
        # the exact undecorated credible-set ID. No general substring matching.
        return signal[:-10] if re.fullmatch(r"[A-Za-z0-9_.:+-]+__credibleSet\d+__selected", signal) else None
    return signal


def _coloc_signals(step, params):
    signals, issues, seen = [], [], set()
    for edge in step.get("edges", []):
        if edge.get("type") != "SIGNAL_COLOC_WITH":
            continue
        if edge.get("start_id") != params["gene_id"] or edge.get("end_id") != params["disease_id"]:
            issues.append("unexpected_coloc_endpoints")
            continue
        props = edge.get("properties", {})
        context = COLOC_QTL_CONTEXT.get(props.get("coloc_dataset"))
        qtl_leads, gwas_leads = _lead_ids(props.get("qtl_lead_vars")), _lead_ids(props.get("gwas_lead_vars"))
        qtl_signal = props.get("qtl_signal_id")
        gwas_signal = _gwas_credible_set(props.get("gwas_signal_id"))
        source = props.get("data_source")
        if not context:
            issues.append("unsupported_coloc_dataset")
            continue
        if not qtl_leads or not gwas_leads or not isinstance(qtl_signal, str) or not 0 < len(qtl_signal) <= 512 or not gwas_signal or not isinstance(source, str):
            issues.append("unsupported_coloc_identifiers")
            continue
        if params.get("data_source") and source != params["data_source"]:
            issues.append("coloc_source_mismatch")
            continue
        signal = {"qtl_signal_id": qtl_signal, "gwas_signal_id": props["gwas_signal_id"],
                  "gwas_credible_set_id": gwas_signal, "coloc_dataset": props["coloc_dataset"],
                  "coloc_data_source": source, "qtl_leads": qtl_leads, "gwas_leads": gwas_leads,
                  "qtl_data_source": context[0], "qtl_tissue_id": context[1]}
        key = json.dumps(signal, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        if len(signals) == MAX_COLOC_SIGNALS:
            issues.append("coloc_signal_budget")
            continue
        signals.append(signal)
    return signals, sorted(set(issues))


def compile_coloc_expansion(params, signals):
    core = ("UNWIND $signals AS signal "
            "MATCH (g:Gene {id:$gene_id})-[coloc:SIGNAL_COLOC_WITH]->(d:disease {id:$disease_id}) "
            "WHERE coloc.qtl_signal_id=signal.qtl_signal_id AND coloc.gwas_signal_id=signal.gwas_signal_id "
            "AND coloc.coloc_dataset=signal.coloc_dataset AND coloc.data_source=signal.coloc_data_source ")
    qtl = (core + "MATCH (v:sequence_variant)-[r:PART_OF_QTL_SIGNAL]->(g) "
           "WHERE v.id IN signal.qtl_leads AND r.credible_set=signal.qtl_signal_id "
           "AND r.data_source=signal.qtl_data_source AND r.tissue_id=signal.qtl_tissue_id "
           "RETURN v,r,g AS target")
    gwas = (core + "MATCH (v:sequence_variant)-[r:PART_OF_GWAS_SIGNAL]->(d) "
            "WHERE v.id IN signal.gwas_leads AND r.credible_set_id=signal.gwas_credible_set_id "
            "RETURN v,r,d AS target")
    return qtl + " UNION ALL " + gwas, {"gene_id": params["gene_id"], "disease_id": params["disease_id"], "signals": signals}


def _used_limits(steps):
    return {"known_node_ids": list({node["id"] for step in steps for node in step.get("nodes", [])}),
            "known_edge_keys": list({json.dumps(edge, sort_keys=True, separators=(",", ":")) for step in steps for edge in step.get("edges", [])}),
            "used_bytes": sum(step.get("materialized_bytes", 0) for step in steps),
            "used_rows": sum(len(step.get("rows", [])) for step in steps)}


def _merge_steps(steps):
    nodes, edges, rows = {}, {}, []
    for step in steps:
        for node in step.get("nodes", []):
            nodes.setdefault(node["id"], node)
        for edge in step.get("edges", []):
            edges.setdefault(json.dumps(edge, sort_keys=True, separators=(",", ":")), edge)
        rows.extend(step.get("rows", []))
    partial = any(step.get("truncated") or step.get("status") in {"failed", "partial"} for step in steps)
    status = "partial" if partial else "complete" if nodes or edges or rows else "empty"
    return {"nodes": list(nodes.values()), "edges": list(edges.values()), "rows": rows,
            "queries": [item for step in steps for item in step.get("queries", [])],
            "validation": [item for step in steps for item in step.get("validation", [])],
            "provenance": [item for step in steps for item in step.get("provenance", [])],
            "truncated": any(step.get("truncated") for step in steps), "completeness": status,
            "graph_version": steps[0]["graph_version"], "steps": steps}


class QueryService:
    def __init__(self, settings, graph=None):
        self.settings = settings
        self.graph = graph or GraphAdapter(settings)
        self.last_success = None
        self.last_error = None
        self.calls = 0
        self._semaphore = asyncio.Semaphore(2)

    async def probe(self):
        return await self.graph.probe()

    async def close(self):
        await self.graph.close()

    async def execute_query(self, query, parameters, step_id="conventional", purpose="primary", limits=None):
        """Internal validated-Cypher boundary; not an HTTP arbitrary-query API."""
        async with self._semaphore:
            await self.graph._ensure_identity()
            errors = validate_cypher(query, {"complete": False, "constraints": []}, parameters)
            if errors:
                raise GraphValidationError(";".join(errors))
            timeout = self.settings.graph_timeout + 1
            errors = await asyncio.wait_for(self.graph._explain(query, parameters), timeout)
            if errors:
                raise GraphValidationError(";".join(errors))
            self.calls += 1
            try:
                result = await asyncio.wait_for(self.graph._retrieve(query, parameters, limits=limits), timeout)
                self.last_success = time.time()
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                raise
        result.setdefault("status", "partial" if result.get("truncated") else "complete" if result.get("nodes") or result.get("rows") else "empty")
        return {**result, "step_id": step_id, "purpose": purpose,
                "graph_version": self.settings.graph_version,
                "queries": [{"cypher": query, "parameters": parameters}],
                "validation": [{"valid": True, "checks": ["configured_release", "readonly", "schema_explain", "bounded_materialization"]}],
                "provenance": [{"source": "configured_graph", "graph_version": self.settings.graph_version}]}

    async def execute(self, template_id, supplied, question=""):
        query, params = compile_template(template_id, supplied)
        step = await self.execute_query(query, params)
        step.update(question=question or template_id.replace("_", " "), title=question or template_id.replace("_", " "))
        steps = [step]
        if template_id == "coloc_by_gene" and step["status"] != "empty":
            signals, issues = _coloc_signals(step, params)
            context = {"step_id": "coloc_leads", "purpose": "context", "context_for": step["step_id"],
                       "depends_on": [step["step_id"]], "title": "Inspect the colocalized QTL and GWAS lead signals",
                       "question": "Retrieve lead-variant membership for the exact colocalized signals, gene, disease and source context.",
                       "source_note": "These are the lead variants and exact credible sets identified by the returned colocalization records. Other signals at the locus are not implied to colocalize.",
                       "graph_version": self.settings.graph_version, "nodes": [], "edges": [], "rows": [],
                       "queries": [], "validation": [], "provenance": [], "truncated": False,
                       "status": "empty", "expansion": {"signal_count": len(signals), "signal_limit": MAX_COLOC_SIGNALS, "issues": issues}}
            limits = _used_limits(steps)
            exhausted = (limits["used_bytes"] >= self.settings.max_bytes or limits["used_rows"] >= getattr(self.settings, "max_rows", 1000)
                         or len(limits["known_node_ids"]) >= self.settings.max_nodes or len(limits["known_edge_keys"]) >= self.settings.max_edges)
            if exhausted:
                context.update(status="partial", truncated=True)
                context["expansion"]["issues"].append("materialization_budget_exhausted")
            elif signals:
                expanded_query, expanded_params = compile_coloc_expansion(params, signals)
                try:
                    retrieved = await self.execute_query(expanded_query, expanded_params, "coloc_leads", "context", limits=limits)
                    context.update(retrieved)
                    for branch, relation in (("qtl", "PART_OF_QTL_SIGNAL"), ("gwas", "PART_OF_GWAS_SIGNAL")):
                        count = sum(edge.get("type") == relation for edge in context["edges"])
                        context["expansion"][branch] = {"edge_count": count, "status": "partial" if context["truncated"] else "complete" if count else "empty"}
                        if not count:
                            context["source_note"] += f" No matching {branch.upper()} lead membership was returned under these exact identifiers and filters."
                except Exception as exc:
                    context.update(status="failed", error={"category": type(exc).__name__})
            if issues:
                context["status"] = "partial" if context["status"] != "failed" else "failed"
                context["source_note"] += " Some COLOC records could not be expanded under the verified context or signal bounds."
            if "coloc_signal_budget" in issues:
                context["truncated"] = True
            steps.append(context)
        evidence = _merge_steps(steps)
        evidence.update(template_id=template_id, parameters=parameters_for(template_id, supplied))
        if supplied.get("variant_id") and params.get("graph_variant_id", supplied["variant_id"]) != supplied["variant_id"]:
            evidence["scope_note"] = LEAD_SCOPE_NOTE
            evidence["requested_variant"] = supplied["variant_id"]
            step["source_note"] = LEAD_SCOPE_NOTE
            step["requested_variant"] = supplied["variant_id"]
            step["question"] += " Evidence scope: " + LEAD_SCOPE_NOTE
        return evidence

    async def search(self, kind, term="", template_id="", **supplied):
        if kind not in {"gene", "variant", "credible_set"}:
            raise ValueError("unknown_search_kind")
        if not isinstance(term, str) or len(term) > 512:
            raise ValueError("invalid_search_term")
        await self.graph._ensure_identity()
        if kind in {"gene", "variant"}:
            if len(term.strip()) < 2:
                return {"items": [], "status": "ready", "coverage": {"source": "configured_graph", "complete": True}}
            label = "Gene" if kind == "gene" else "sequence_variant"
            query = f"MATCH (n:{label}) WHERE toLower(n.name) STARTS WITH toLower($term) OR n.id STARTS WITH $term RETURN n.id AS id,n.name AS name ORDER BY n.name,n.id LIMIT 26" if kind == "gene" else "MATCH (n:sequence_variant) WHERE n.id=$term RETURN n.id AS id,n.id AS name ORDER BY n.id LIMIT 26"
            rows = await self.graph._small_query(query, {"term": term.strip()})
            items = [{**row, "value": row["id"], "label": row.get("name") or row["id"], "gene": row["id"] if kind == "gene" else None, "snp": row["id"] if kind == "variant" else None} for row in rows[:25]]
        else:
            supplied = {key: value for key, value in supplied.items() if value}
            if template_id not in TEMPLATES:
                raise ValueError("unknown_template")
            query, params = compile_template(template_id, supplied)
            if template_id.startswith("qtl_"):
                query = query.rsplit(" RETURN ", 1)[0] + " RETURN v.id AS snp,g.id AS gene,g.name AS gene_name,properties(r) AS properties ORDER BY r.pip DESC,r.credible_set,v.id LIMIT 201"
            elif template_id == "gwas_by_variant":
                query = query.rsplit(" RETURN ", 1)[0] + " RETURN v.id AS snp,properties(r) AS properties ORDER BY r.pip DESC,r.credible_set_id,v.id LIMIT 201"
            else:
                return {"items": [], "status": "ready", "coverage": {"source": "configured_graph", "complete": True}}
            rows = await self.graph._small_query(query, params)
            items = []
            for row in rows[:200]:
                props = row.get("properties", {})
                cs = props.get("credible_set", props.get("credible_set_id", props.get("credibleset")))
                items.append({**props, **{k: v for k, v in row.items() if k != "properties"}, "credible_set_id": cs, "credible_set": cs, "lead_snp": row.get("snp"), "lead_variant_id": row.get("snp"), "gene_id": row.get("gene")})
        self.last_success = time.time()
        maximum = 200 if kind == "credible_set" else 25
        return {"items": items, "status": "ready", "coverage": {"source": "configured_graph", "graph_version": self.settings.graph_version, "complete": len(rows) <= maximum, "truncated": len(rows) > maximum, "nonlead_variant_index": "separate_resource_index"}}
