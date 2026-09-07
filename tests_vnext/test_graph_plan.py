"""Offline release-verified planning and narrowly scoped entity equivalence."""
import copy
import re
import time
from types import SimpleNamespace
import unittest

from pankagent_vnext.graph import GraphAdapter, validate_cypher
from pankagent_vnext.plan_constraints import build_generation_question


GENE = {"id": "test-CFTR-id", "name": "CFTR", "labels": ["Gene"]}
CELL = {"id": "CL_0002079", "name": "ductal cell", "labels": ["anatomical_structure"]}
BASE = "MATCH (g:Gene)-[r:GENE_ENRICHED_IN]->(c:anatomical_structure) WHERE g.id='test-CFTR-id' AND c.id='CL_0002079' RETURN g,r,c"


def plan(include_context=False, **step_changes):
    step = {"id": "s1", "title": "Check CFTR enrichment in ductal cells", "rationale": "Inspect the requested enrichment measurement.",
            "question": "Is CFTR (gene) specifically enriched in ductal cells (Gene name CFTR, cell type ductal cell CL_0002079)?",
            "constraints": [{"property": "name", "operator": "=", "value": "CFTR", "entity_type": "Gene"},
                            {"property": "name", "operator": "=", "value": "ductal cell", "entity_type": "anatomical_structure"}],
            "relation_types": ["GENE_ENRICHED_IN"], "depends_on": [], "complete": True, **step_changes}
    return {"steps": [step], "include_context": include_context, "literature": False, "clarification": None}


class ResolverGraph(GraphAdapter):
    def __init__(self, rows=None):
        self.settings = SimpleNamespace(graph_version="synthetic-release", graph_timeout=1,
            graph_identity_file="/nonexistent-test-manifest", neo4j_uri="bolt://configured-release",
            neo4j_database="pankgraph", cypher_url="http://configured-generator", max_nodes=2000, max_bytes=2000000)
        self.identity_verified, self.identity_check_time = True, time.monotonic()
        self.release_labels = {"Gene", "anatomical_structure", "donor"}
        self.release_relations = {"GENE_ENRICHED_IN", "GENE_DETECTED_IN"}
        self.rows = copy.deepcopy(rows if rows is not None else [GENE, CELL])
        self.reads, self.generated, self.retrieved = [], [], []
        self.last_query_success = self.last_generation_success = None

    async def _small_query(self, query, params=None):
        self.reads.append((query, params))
        label = re.search(r"MATCH \(n:`([^`]+)`\)", query)
        prop = "id" if "n.`id`" in query else "name"
        rows = [row for row in self.rows if label is None or label[1] in row["labels"]]
        if "toLower" in query:
            rows = [row for row in rows if str(row[prop]).casefold() == params["value"].casefold()]
        else:
            rows = [row for row in rows if row[prop] == params["value"]]
        return copy.deepcopy(rows[:3])

    async def _generate(self, question, n):
        self.generated.append((question, n))
        return [BASE]

    async def _explain(self, query, parameters):
        return []

    async def _retrieve(self, query, parameters, limits=None):
        self.retrieved.append(query)
        return {"status": "complete", "truncated": False, "nodes": [
            {"id": row["id"], "labels": row["labels"], "properties": {"id": row["id"], "name": row["name"]}}
            for row in self.rows[:2]], "edges": [], "rows": []}


class GraphPlanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.graph = ResolverGraph()
        self.events = []

    async def emit(self, kind, payload):
        self.events.append((kind, payload))

    async def prepared(self, **changes):
        return (await self.graph.prepare_plan(plan(**changes), self.emit))["steps"][0]

    async def test_live_failed_parenthetical_shape_accepts_verified_cell_id(self):
        source = plan()
        before = copy.deepcopy(source)
        prepared = await self.graph.prepare_plan(source, self.emit)
        step = prepared["steps"][0]
        self.assertEqual(source, before)
        self.assertEqual(step["question"], source["steps"][0]["question"])
        self.assertEqual(step["entity_resolution"]["state"], "resolved")
        self.assertEqual([(item["name"], item["id"]) for item in step["resolved_entities"]], [("CFTR", GENE["id"]), ("ductal cell", CELL["id"])])
        self.assertEqual(validate_cypher(BASE, step), [])
        self.assertEqual(validate_cypher(BASE.replace("g.id='test-CFTR-id'", "g.name='CFTR'"), step), [])
        self.assertIn("Find all GENE_ENRICHED_IN relationships", build_generation_question(step))
        self.assertTrue(self.graph._resolution_verified(step))

    async def test_known_measurement_report_suffix_uses_canonical_request_without_dropping_properties(self):
        question = ("Is CFTR specifically enriched in ductal cells? Report the measured enrichment values "
                    "(log2 fold change, adjusted p-value, condition, rank in cell type) supporting this.")
        source = plan(include_context=True, question=question)
        before = copy.deepcopy(source)
        prepared = await self.graph.prepare_plan(source, self.emit)
        primary = prepared["steps"][0]
        expected = ("Find all GENE_ENRICHED_IN relationships from Gene nodes named CFTR "
                    "to anatomical_structure nodes named ductal cell. Return the gene, cell and relationship "
                    "with their properties, without LIMIT or list slices.")
        self.assertEqual(build_generation_question(primary), expected)
        self.assertEqual(primary["question"], question)
        self.assertEqual(primary["constraints"], before["steps"][0]["constraints"])
        self.assertEqual(source, before)
        self.assertEqual(len(prepared["steps"]), 2)
        self.assertEqual(prepared["steps"][1]["purpose"], "context")
        result = await self.graph.execute(primary, {}, self.emit)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.graph.generated[0][1], 1)
        self.assertTrue(self.graph.generated[0][0].startswith(expected))
        self.assertIn("Required relationship types: GENE_ENRICHED_IN", self.graph.generated[0][0])

    async def test_report_clause_never_discards_threshold_unknown_metric_or_extra_task(self):
        questions = [
            "Is CFTR enriched in ductal cells? Report the measured enrichment values (padj < 0.05, condition) supporting this.",
            "Is CFTR enriched in ductal cells? Report the measured enrichment values (enrichment_score) supporting this.",
            "Is CFTR enriched in ductal cells? Report the measured enrichment values (padj) supporting this. Also compare beta cells.",
            "Is CFTR enriched in ductal cells under T1D? Report the measured enrichment values (padj) supporting this.",
        ]
        for question in questions:
            with self.subTest(question=question):
                prepared = await self.graph.prepare_plan(plan(include_context=True, question=question), self.emit)
                self.assertEqual(build_generation_question(prepared["steps"][0]), question)
                self.assertEqual(len(prepared["steps"]), 1)

    async def test_enrichment_schema_is_relation_scoped_and_complete_lookup_rejects_invented_cutoffs(self):
        prepared = await self.prepared()
        for wrong in ("adjusted_p_value", "enrichment_rank_in_cell_type", "enrichment_score"):
            query = BASE + " ORDER BY r." + wrong
            self.assertTrue(any(reason.startswith("invalid_relation_property:GENE_ENRICHED_IN." + wrong)
                                for reason in validate_cypher(query, prepared)))
        self.assertEqual(validate_cypher(BASE + " ORDER BY r.rank_in_cell_type", prepared), [])
        self.assertEqual(validate_cypher(BASE + ", 'r.enrichment_score'", prepared), [])
        renamed = BASE.replace("[r:", "[enrichment_score:").replace("RETURN g,r,c", "RETURN g,enrichment_score,c")
        self.assertEqual(validate_cypher(renamed, prepared), [])
        for query in (BASE.replace(" RETURN", " AND r.padj < 0.05 RETURN"),
                      BASE.replace("]->", " {padj: 0.05}]->"),
                      BASE.replace(" RETURN", " AND r.log2_fold_change >= 2 RETURN")):
            self.assertTrue(any(reason.startswith("unrequested_measurement_filter:")
                                for reason in validate_cypher(query, prepared)))
        requested = copy.deepcopy(prepared)
        requested["constraints"].append({"property": "padj", "operator": "<", "value": "0.05"})
        self.assertEqual(validate_cypher(BASE.replace(" RETURN", " AND r.padj < 0.05 RETURN"), requested), [])
        other_relation = {**prepared, "relation_types": ["T1D_DEG_IN"]}
        self.assertEqual(validate_cypher(BASE.replace("GENE_ENRICHED_IN", "T1D_DEG_IN") + " ORDER BY r.adjusted_p_value", other_relation), [])

    async def test_saved_bad_candidate_stays_rejected_and_recovery_uses_only_one_escalation(self):
        prepared = await self.prepared()
        missing_cell = BASE.replace(" AND c.id='CL_0002079'", "")
        # Same failure as the saved live n=8 candidate: an unrequested cutoff on
        # the wrong edge property and an invented ordering property.
        bad = BASE.replace(" RETURN", " AND r.adjusted_p_value < 0.05 RETURN") + " ORDER BY r.enrichment_rank_in_cell_type"
        corrected = BASE + " ORDER BY r.rank_in_cell_type"
        batches = [[missing_cell], [bad, corrected]]

        async def generate(question, n):
            self.graph.generated.append((question, n))
            return batches.pop(0)
        self.graph._generate = generate
        result = await self.graph.execute(prepared, {}, self.emit)
        self.assertEqual([n for _, n in self.graph.generated], [1, 8])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.graph.retrieved, [corrected])
        self.assertEqual(result["validation"][1]["candidate_cypher"], bad)
        self.assertFalse(result["validation"][1]["valid"])
        self.assertIn("unrequested_measurement_filter:adjusted_p_value", result["validation"][1]["reasons"])
        self.assertEqual(result["queries"], [{"cypher": corrected, "parameters": {}}])

    async def test_resolution_is_typed_parameterized_bounded_and_reuses_identity_reads(self):
        self.graph.rows.append({"id": "donor-CFTR", "name": "CFTR", "labels": ["donor"]})
        await self.prepared()
        self.assertEqual(len(self.graph.reads), 2)
        for query, parameters in self.graph.reads:
            self.assertIn("LIMIT 3", query)
            self.assertIn("$value", query)
            self.assertNotIn(parameters["value"], query)
            self.assertIn("labels(n)[..16]", query)
        await self.prepared()
        self.assertEqual(len(self.graph.reads), 2)

    async def test_id_request_can_use_graph_verified_name_on_same_typed_node(self):
        source = plan()
        source["steps"][0]["constraints"][0].update(property="id", value=GENE["id"])
        prepared = (await self.graph.prepare_plan(source, self.emit))["steps"][0]
        self.assertEqual(validate_cypher(BASE.replace("g.id='test-CFTR-id'", "g.name='CFTR'"), prepared), [])

    async def test_missing_predicate_and_unrelated_ids_are_not_globally_allowed(self):
        step = await self.prepared()
        missing = BASE.replace(" AND c.id='CL_0002079'", "")
        wrong = BASE.replace("CL_0002079", "CL_0000169")
        for query in (missing, wrong, BASE.replace("g.id='test-CFTR-id'", "g.id='invented-gene'")):
            self.assertTrue(validate_cypher(query, step))
        self.assertIn("unrequested_identity_filter:id", validate_cypher(wrong, step))

    async def test_swapped_gene_cell_and_untyped_equivalences_are_rejected(self):
        step = await self.prepared()
        swapped = BASE.replace("g.id='test-CFTR-id' AND c.id='CL_0002079'", "g.id='CL_0002079' AND c.id='test-CFTR-id'")
        for query in (swapped, BASE.replace(":Gene", "").replace(":anatomical_structure", "")):
            self.assertIn("missing_required_entity_relation_path", validate_cypher(query, step))

    async def test_wrong_relation_or_unconnected_decoy_does_not_satisfy_enrichment(self):
        step = await self.prepared()
        wrong = BASE.replace("GENE_ENRICHED_IN", "FUNCTION_ANNOTATION")
        self.assertIn("missing_required_relation:GENE_ENRICHED_IN", validate_cypher(wrong, step))
        decoy = ("MATCH (g:Gene {id:'test-CFTR-id'}), (c:anatomical_structure {id:'CL_0002079'}) "
                 "MATCH (x:Gene)-[r:GENE_ENRICHED_IN]->(y:anatomical_structure) RETURN g,c,r")
        self.assertIn("missing_required_entity_relation_path", validate_cypher(decoy, step))

    async def test_property_maps_parameters_and_reversed_equivalent_pattern_work(self):
        step = await self.prepared()
        maps = "MATCH (g:Gene {name:'CFTR'})-[r:GENE_ENRICHED_IN]->(c:anatomical_structure {id:'CL_0002079'}) RETURN g,r,c"
        self.assertEqual(validate_cypher(maps, step), [])
        params = BASE.replace("'test-CFTR-id'", "$gene").replace("'CL_0002079'", "$cell")
        self.assertEqual(validate_cypher(params, step, {"gene": GENE["id"], "cell": CELL["id"]}), [])
        reverse = "MATCH (c:anatomical_structure)<-[r:GENE_ENRICHED_IN]-(g:Gene) WHERE g.id='test-CFTR-id' AND c.id='CL_0002079' RETURN g,r,c"
        self.assertEqual(validate_cypher(reverse, step), [])

    async def test_optional_relation_and_union_missing_filter_remain_invalid(self):
        step = await self.prepared()
        optional = "MATCH (g:Gene {id:'test-CFTR-id'}), (c:anatomical_structure {id:'CL_0002079'}) OPTIONAL MATCH (g)-[r:GENE_ENRICHED_IN]->(c) RETURN g,r,c"
        self.assertIn("missing_required_relation:GENE_ENRICHED_IN", validate_cypher(optional, step))
        self.assertTrue(validate_cypher(BASE + " UNION " + BASE.replace(" AND c.id='CL_0002079'", ""), step))

    async def test_unique_casefold_match_resolves_but_ambiguity_requests_revision(self):
        source = plan()
        source["steps"][0]["constraints"][0]["value"] = "cftr"
        prepared = await self.graph.prepare_plan(source, self.emit)
        self.assertEqual(prepared["steps"][0]["resolved_entities"][0]["name"], "CFTR")
        self.assertEqual(validate_cypher(BASE, prepared["steps"][0]), [])
        ambiguous = ResolverGraph([GENE, {**GENE, "id": "other-gene"}, CELL])
        result = await ambiguous.prepare_plan(plan(include_context=True), self.emit)
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["entity_resolution"]["state"], "needs_clarification")
        self.assertEqual(result["steps"][0]["resolved_entities"][0]["state"], "ambiguous")
        self.assertEqual(len(result["steps"][0]["resolved_entities"][0]["candidates"]), 2)
        self.assertTrue(result["clarification"])

    async def test_not_found_never_broadens_into_a_query_or_context(self):
        missing = ResolverGraph([CELL])
        result = await missing.prepare_plan(plan(include_context=True), self.emit)
        outcome = await missing.execute(result["steps"][0], {}, self.emit)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(missing.generated, [])

    async def test_non_equality_entity_predicates_keep_original_strict_validation(self):
        source = plan(constraints=[{"property": "name", "operator": "IN", "value": '["CFTR","INS"]', "entity_type": "Gene"}],
                      question="Find the genes CFTR and INS.", relation_types=[])
        result = await self.graph.prepare_plan(source, self.emit)
        self.assertIsNone(result["clarification"])
        step = result["steps"][0]
        self.assertEqual(step["resolved_entities"][0]["state"], "literal_predicate")
        self.assertEqual(self.graph.reads, [])
        self.assertEqual(validate_cypher("MATCH (g:Gene) WHERE g.name IN ['CFTR','INS'] RETURN g", step), [])
        self.assertTrue(validate_cypher("MATCH (g:Gene) WHERE g.name='CFTR' RETURN g", step))

    async def test_one_complementary_context_step_is_explicit_independent_and_optional(self):
        prepared = await self.graph.prepare_plan(plan(include_context=True), self.emit)
        self.assertEqual(len(prepared["steps"]), 2)
        context = prepared["steps"][1]
        self.assertEqual((context["purpose"], context["context_for"], context["depends_on"]), ("context", "s1", []))
        self.assertEqual(context["relation_types"], ["GENE_DETECTED_IN"])
        self.assertEqual(len(context["constraints"]), 1)
        self.assertIn("different cohorts and sources", context["rationale"])
        self.assertIn("GENE_DETECTED_IN", build_generation_question(context))
        self.assertEqual(len(self.graph.reads), 2)
        repeated = await self.graph.prepare_plan(prepared, self.emit)
        self.assertEqual(len(repeated["steps"]), 2)
        self.assertEqual(len((await self.graph.prepare_plan(plan(include_context=False), self.emit))["steps"]), 1)

    async def test_detection_primary_gets_enrichment_context(self):
        result = await self.graph.prepare_plan(plan(include_context=True,
            question="Is CFTR detected in ductal cells?", relation_types=["GENE_DETECTED_IN"]), self.emit)
        self.assertEqual(result["steps"][1]["relation_types"], ["GENE_ENRICHED_IN"])

    async def test_measured_enrichment_evidence_wording_keeps_typed_request_and_context(self):
        question = "Is CFTR specifically enriched in ductal cells, based on measured enrichment evidence?"
        result = await self.graph.prepare_plan(plan(include_context=True, question=question), self.emit)
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][0]["question"], question)
        self.assertEqual(build_generation_question(result["steps"][0]),
            "Find all GENE_ENRICHED_IN relationships from Gene nodes named CFTR to anatomical_structure nodes named ductal cell. Return the gene, cell and relationship with their properties, without LIMIT or list slices.")
        self.assertEqual(result["steps"][1]["relation_types"], ["GENE_DETECTED_IN"])
        self.assertEqual(result["steps"][1]["purpose"], "context")

    async def test_unrecorded_modifiers_preserve_original_request_and_do_not_expand(self):
        for question in ("Is CFTR enriched in ductal cells only in ND?", "Is CFTR not enriched in ductal cells?",
                         "Is CFTR enriched in ductal cells with padj < 0.05?", "Is CFTR enriched in stressed ductal cells?",
                         "Is CFTR enriched in ductal cells, based on measured enrichment evidence in ND?"):
            with self.subTest(question=question):
                result = await self.graph.prepare_plan(plan(include_context=True, question=question), self.emit)
                self.assertEqual(len(result["steps"]), 1)
                self.assertEqual(build_generation_question(result["steps"][0]), question)

    async def test_tampered_resolution_is_reverified_and_cannot_allow_an_unrelated_id(self):
        step = await self.prepared()
        step["resolved_entities"][1]["id"] = "unrelated-cell"
        step["resolved_entities"][1]["labels"].append("Gene")
        self.assertFalse(self.graph._resolution_verified(step))
        outcome = await self.graph.execute(step, {}, self.emit)
        self.assertEqual(outcome["resolved_entities"][1]["id"], CELL["id"])
        self.assertEqual(outcome["resolved_entities"][1]["labels"], ["anatomical_structure"])
        self.assertEqual(outcome["status"], "complete")
        self.assertEqual(self.graph.generated[0][1], 1)

    async def test_preview_identity_changes_on_release_bounds_or_verification_not_time(self):
        first = self.graph.preview_identity()
        self.graph.identity_check_time += 1
        self.assertEqual(first, self.graph.preview_identity())
        self.graph.settings.max_nodes += 1
        self.assertNotEqual(first, self.graph.preview_identity())
        self.graph.settings.max_nodes -= 1
        self.graph.identity_verified = False
        self.assertNotEqual(first, self.graph.preview_identity())

    async def test_evidence_keeps_reviewed_title_purpose_rationale_and_original_question(self):
        step = await self.prepared(purpose="primary", context_for=None)
        outcome = await self.graph.execute(step, {}, self.emit)
        for key in ("title", "purpose", "rationale", "context_for", "question"):
            self.assertEqual(outcome[key], step[key])
        self.assertEqual(outcome["status"], "complete")
        self.assertNotIn("resolution_key", outcome)


if __name__ == "__main__":
    unittest.main()
