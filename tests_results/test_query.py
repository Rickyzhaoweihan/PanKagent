import copy
import unittest
from types import SimpleNamespace

from pankgraph_results.query import QueryService, TEMPLATES, compile_template, parameters_for
from pankagent_vnext.graph import validate_cypher


def graph_result(nodes=None, edges=None, **other):
    return {"nodes": nodes or [], "edges": edges or [], "rows": [], "truncated": False,
            "materialized_bytes": 250, "status": "complete" if nodes or edges else "empty", **other}


def coloc_core(count=1):
    gene = {"id": "ENSG1", "labels": ["Gene"], "properties": {"name": "GENE"}}
    disease = {"id": "MONDO_other", "labels": ["disease"], "properties": {}}
    edges = [{"start_id": "ENSG1", "end_id": "MONDO_other", "type": "SIGNAL_COLOC_WITH", "properties": {
        "qtl_lead_vars": "rs1,rs2", "gwas_lead_vars": "rs3", "qtl_signal_id": f"ENSG1__credibleSet{i+1}",
        "gwas_signal_id": "ADCY3__credibleSet1__selected", "coloc_dataset": "t1d_exonQTL-inspire_coloc",
        "data_source": "HIRN_T1D_QTL_GWAS"}} for i in range(count)]
    return graph_result([gene, disease], edges)


class FakeGraph:
    def __init__(self, *results):
        self.results = list(results)
        self.retrievals = []
        self.explains = []
        self.identity_checks = 0

    async def _ensure_identity(self):
        self.identity_checks += 1

    async def _explain(self, query, params):
        self.explains.append((query, copy.deepcopy(params)))
        return []

    async def _retrieve(self, query, params, limits=None):
        self.retrievals.append((query, copy.deepcopy(params), copy.deepcopy(limits)))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)

    async def close(self):
        pass


def service(*results, **overrides):
    settings = SimpleNamespace(graph_version="PanKgraph_08_04", graph_timeout=1,
                               max_bytes=10000, max_nodes=2000, max_edges=5000, max_rows=1000)
    for key, value in overrides.items():
        setattr(settings, key, value)
    graph = FakeGraph(*results)
    return QueryService(settings, graph), graph


class TemplateTests(unittest.TestCase):
    def test_all_six_templates_are_readonly_typed_and_parameterized(self):
        for template, required in TEMPLATES.items():
            with self.subTest(template=template):
                query, params = compile_template(template, {key: "arbitrary'} DETACH DELETE n //" for key in required})
                self.assertNotIn("DETACH", query)
                self.assertEqual(validate_cypher(query, {"complete": False, "constraints": []}, params), [])
                self.assertNotIn("LIMIT", query)
                if template.startswith("qtl"):
                    self.assertIn(":PART_OF_QTL_SIGNAL", query)
                self.assertTrue(any("DETACH" in value for value in params.values()))

    def test_parameter_whitelist_type_and_required_values(self):
        for template, values in [("unknown", {}), ("qtl_by_gene", {"gene_id": " "}),
                                 ("qtl_by_gene", {"gene_id": ["ENSG1"]}),
                                 ("qtl_by_gene", {"gene_id": "ENSG1", "cypher": "MATCH(n) RETURN n"}),
                                 ("qtl_by_gene", {"gene_id": "ENSG1", "disease_id": "MONDO_2"}),
                                 ("coloc_by_gene", {"gene_id": "ENSG1", "cell_id": "CL_1"})]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                parameters_for(template, values)
        self.assertEqual(parameters_for("qtl_by_gene", {"gene_id": " ENSG1 ", "cell_id": None}), {"gene_id": "ENSG1"})

    def test_explicit_disease_is_preserved_and_legacy_default_is_visible(self):
        for template, values in [("gwas_by_variant", {"variant_id": "rs1"}), ("coloc_by_gene", {"gene_id": "ENSG1"})]:
            query, params = compile_template(template, {**values, "disease_id": "MONDO_other", "data_source": "selected"})
            self.assertEqual(params["disease_id"], "MONDO_other")
            self.assertIn("id:$disease_id", query)
            self.assertIn("r.data_source=$data_source", query)
            self.assertEqual(parameters_for(template, values)["disease_id"], "MONDO_0005147")

    def test_expression_requires_selected_cell_edge_and_qtl_keeps_set_source(self):
        query, _ = compile_template("expression_by_gene", {"gene_id": "ENSG1", "cell_id": "CL_1", "data_source": "source"})
        self.assertNotIn("OPTIONAL", query)
        self.assertIn("c.id=$cell_id", query)
        self.assertIn("r.data_source=$data_source", query)
        query, params = compile_template("qtl_by_variant_gene", {"gene_id": "ENSG1", "variant_id": "rs1", "lead_variant_id": "rs2", "credible_set_id": "cs1", "data_source": "source"})
        self.assertIn("g.id=$gene_id", query)
        self.assertIn("r.credible_set=$credible_set_id", query)
        self.assertEqual(params["graph_variant_id"], "rs2")
        with self.assertRaisesRegex(ValueError, "credible_set_required"):
            compile_template("qtl_by_variant", {"variant_id": "rs1", "lead_variant_id": "rs2"})


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_and_truncated_completeness(self):
        query, _ = service(graph_result())
        result = await query.execute("expression_by_gene", {"gene_id": "ENSG1"})
        self.assertEqual(result["completeness"], "empty")
        query, _ = service(graph_result(truncated=True, status="partial"))
        result = await query.execute("qtl_by_gene", {"gene_id": "ENSG1"})
        self.assertEqual(result["completeness"], "partial")

    async def test_coloc_expansion_preserves_exact_gene_disease_signals_and_context(self):
        core = coloc_core()
        query, graph = service(core, graph_result())
        result = await query.execute("coloc_by_gene", {"gene_id": "ENSG1", "disease_id": "MONDO_other", "data_source": "HIRN_T1D_QTL_GWAS"})
        self.assertEqual(len(graph.retrievals), 2)
        expanded, params, limits = graph.retrievals[1]
        self.assertEqual(validate_cypher(expanded, {"complete": False, "constraints": []}, params), [])
        self.assertEqual(params["gene_id"], "ENSG1")
        self.assertEqual(params["disease_id"], "MONDO_other")
        self.assertEqual(expanded.count("id:$disease_id"), 2)
        self.assertEqual(expanded.count("coloc.qtl_signal_id=signal.qtl_signal_id"), 2)
        self.assertIn("r.credible_set=signal.qtl_signal_id", expanded)
        self.assertIn("r.credible_set_id=signal.gwas_credible_set_id", expanded)
        signal = params["signals"][0]
        self.assertEqual(signal["gwas_credible_set_id"], "ADCY3__credibleSet1")
        self.assertEqual(signal["gwas_signal_id"], "ADCY3__credibleSet1__selected")
        self.assertEqual(signal["qtl_data_source"], "exon; INSPIRE")
        self.assertEqual(signal["qtl_tissue_id"], "UBERON_0000006")
        self.assertEqual(signal["qtl_leads"], ["rs1", "rs2"])
        self.assertEqual(limits["used_bytes"], 250)
        self.assertEqual(set(limits["known_node_ids"]), {"ENSG1", "MONDO_other"})
        self.assertEqual(result["steps"][1]["depends_on"], ["conventional"])
        self.assertEqual(result["completeness"], "complete")
        self.assertIn("exact credible sets", result["steps"][1]["source_note"])
        self.assertEqual(result["steps"][1]["expansion"]["qtl"]["status"], "empty")
        self.assertEqual(result["steps"][1]["expansion"]["gwas"]["status"], "empty")

    async def test_unknown_context_or_wrong_disease_never_broadens_expansion(self):
        for mutation, reason in [("dataset", "unsupported_coloc_dataset"), ("disease", "unexpected_coloc_endpoints"), ("lead", "unsupported_coloc_identifiers")]:
            core = coloc_core()
            if mutation == "dataset": core["edges"][0]["properties"]["coloc_dataset"] = "unknown"
            elif mutation == "disease": core["edges"][0]["end_id"] = "different_disease"
            else: core["edges"][0]["properties"]["qtl_lead_vars"] = "please query rs1"
            query, graph = service(core)
            result = await query.execute("coloc_by_gene", {"gene_id": "ENSG1", "disease_id": "MONDO_other"})
            self.assertEqual(len(graph.retrievals), 1)
            self.assertEqual(result["completeness"], "partial")
            self.assertIn(reason, result["steps"][1]["expansion"]["issues"])

    async def test_coloc_expansion_failure_retains_primary_evidence(self):
        core = coloc_core()
        query, _ = service(core, TimeoutError("private diagnostics"))
        result = await query.execute("coloc_by_gene", {"gene_id": "ENSG1", "disease_id": "MONDO_other"})
        self.assertEqual(result["nodes"], core["nodes"])
        self.assertEqual(result["edges"], core["edges"])
        self.assertEqual(result["steps"][1]["status"], "failed")
        self.assertEqual(result["steps"][1]["error"], {"category": "TimeoutError"})
        self.assertEqual(result["completeness"], "partial")

    async def test_signal_and_materialization_budgets_are_global_and_visible(self):
        query, graph = service(coloc_core(26), graph_result())
        result = await query.execute("coloc_by_gene", {"gene_id": "ENSG1", "disease_id": "MONDO_other"})
        self.assertEqual(len(graph.retrievals[1][1]["signals"]), 25)
        self.assertTrue(result["truncated"])
        self.assertIn("coloc_signal_budget", result["steps"][1]["expansion"]["issues"])
        query, graph = service(coloc_core(), max_bytes=250)
        result = await query.execute("coloc_by_gene", {"gene_id": "ENSG1", "disease_id": "MONDO_other"})
        self.assertEqual(len(graph.retrievals), 1)
        self.assertIn("materialization_budget_exhausted", result["steps"][1]["expansion"]["issues"])

    async def test_nonlead_scope_reaches_step_and_question(self):
        query, graph = service(graph_result())
        result = await query.execute("qtl_by_variant_gene", {"gene_id": "ENSG1", "variant_id": "rs1", "lead_variant_id": "rs2", "credible_set_id": "cs1"})
        self.assertEqual(graph.retrievals[0][1]["graph_variant_id"], "rs2")
        self.assertIn("nonlead", result["steps"][0]["source_note"])
        self.assertIn(result["scope_note"], result["steps"][0]["question"])
        self.assertEqual(result["requested_variant"], "rs1")

    async def test_query_boundary_rejects_writes_before_graph_execution(self):
        query, graph = service()
        with self.assertRaises(ValueError):
            await query.execute_query("MATCH(n) DETACH DELETE n", {})
        self.assertEqual(graph.retrievals, [])


if __name__ == "__main__":
    unittest.main()
