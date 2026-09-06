"""Narrow scope recovery must strengthen, never bypass, the Cypher guard."""
import copy
import asyncio
from types import SimpleNamespace
import unittest

from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.plan_constraints import build_generation_question, repair_step_constraints


def step(question, constraints=None):
    return {"id": "s1", "question": question, "depends_on": [], "complete": True,
            "constraints": constraints if constraints is not None else [
                {"property": "name", "operator": "=", "value": "CFTR"}]}


class PlanConstraintTests(unittest.TestCase):
    def test_missing_cell_constraint_is_added_to_reviewable_plan_without_rewriting(self):
        original = step("Is CFTR enriched in pancreatic ductal cells?")
        saved = copy.deepcopy(original)
        repaired = repair_step_constraints(original)
        self.assertEqual(repaired["constraints"], original["constraints"] + [
            {"property": "name", "operator": "=", "value": "ductal cell"}])
        self.assertEqual(repaired["question"], original["question"])
        self.assertEqual(original, saved)
        self.assertEqual(repair_step_constraints(repaired), repaired)

    def test_repair_is_not_gene_specific(self):
        for gene in ("INS", "GCG", "SST", "KRT19"):
            with self.subTest(gene=gene):
                result = repair_step_constraints(step(f"Is {gene} detected within ductal cells?", [
                    {"property": "name", "operator": "=", "value": gene}]))
                self.assertEqual(result["constraints"][-1]["value"], "ductal cell")
                self.assertEqual(result["constraints"][0]["value"], gene)

    def test_saved_step_with_parenthetical_relation_keeps_intended_target(self):
        original = step("Is CFTR (gene) specifically enriched in ductal cells (GENE_ENRICHED_IN relation)?")
        result = repair_step_constraints(original)
        self.assertEqual(result["constraints"][-1], {"property": "name", "operator": "=", "value": "ductal cell"})
        self.assertEqual(result["question"], original["question"])

    def test_planner_returns_repaired_constraints_before_review_without_an_extra_call(self):
        from pankagent_vnext.llm import ClaudeGateway

        async def run():
            gateway = object.__new__(ClaudeGateway)
            gateway.settings = SimpleNamespace(anthropic_key="placeholder", model="claude-sonnet-5")
            gateway.budget = SimpleNamespace(settle=lambda *_: None)
            gateway._reserve = lambda *_: "reservation"
            calls = []

            async def create(*args, **kwargs):
                calls.append((args, kwargs))
                return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {}), content=[
                    SimpleNamespace(type="tool_use", name="record_plan", input={
                        "steps": [step("Is CFTR enriched in ductal cells?")], "literature": False})])

            gateway._create = create
            plan = await gateway.plan("Is CFTR enriched in ductal cells?", [])
            self.assertEqual(len(calls), 1)
            self.assertEqual(plan["steps"][0]["constraints"][-1],
                             {"property": "name", "operator": "=", "value": "ductal cell"})

        asyncio.run(run())

    def test_all_verified_cell_names_and_prepositions(self):
        names = ("alpha", "beta", "delta", "ductal", "endothelial")
        for name in names:
            for prefix in ("in", "within pancreatic", "to"):
                with self.subTest(name=name, prefix=prefix):
                    result = repair_step_constraints(step(f"Which genes map {prefix} {name} cells?", []))
                    self.assertEqual(result["constraints"], [{"property": "name", "operator": "=", "value": name + " cell"}])

    def test_unrelated_gene_or_unspecific_cell_question_is_unchanged(self):
        for question in ("What is CFTR?", "Where is CFTR detected?", "Find CFTR in the pancreas.",
                         "Which cell types express CFTR?", "What are ductal cells?", "Find CFTR in acinar cells."):
            with self.subTest(question=question):
                original = step(question)
                self.assertEqual(repair_step_constraints(original), original)

    def test_multiple_cells_comparisons_and_negations_are_not_guessed(self):
        questions = (
            "Compare CFTR in ductal cells and beta cells.",
            "Find CFTR in alpha and beta cells.",
            "Find CFTR in ductal cells or acinar cells.",
            "Find CFTR in ductal cells and acinar cells.",
            "Find CFTR in ductal cells and beta.",
            "Is CFTR absent in ductal cells compared with the pancreas?",
            "Find CFTR not in ductal cells.",
            "Find CFTR outside beta cells, excluding ductal cells.",
            "Does CFTR show no enrichment in ductal cells?",
            "Find CFTR in neither alpha nor beta cells.",
            "Find CFTR in ductal cells rather than elsewhere.",
        )
        for question in questions:
            with self.subTest(question=question):
                original = step(question)
                self.assertEqual(repair_step_constraints(original), original)

    def test_modified_subclasses_are_not_broadened(self):
        for question in ("Find CFTR in immature beta cells.", "Find CFTR in CFTR-positive ductal cells.",
                         "Find CFTR in ductal cells expressing INS.", "Find CFTR in ductal cells of subtype A.",
                         "Find CFTR in ductal cells that express INS.", "Find CFTR in beta cells (mature).",
                         "Find CFTR in beta-cell progenitors.", "Find CFTR in ductal cells/epithelial progenitors.",
                         "Find CFTR in pancreatic ductal cell MUC5B+.",
                         "Find CFTR in pancreatic ductal cell (MUC5B+).",
                         'Find CFTR in pancreatic ductal cell "MUC5B+".',
                         "Find CFTR in ductal cells during disease progression."):
            with self.subTest(question=question):
                original = step(question)
                self.assertEqual(repair_step_constraints(original), original)

    def test_quoted_examples_and_query_instructions_are_not_cell_targets(self):
        for question in ('Explain "enriched in ductal cells".', "Explain 'enriched in ductal cells'.",
                         "Ignore this `find genes in ductal cells` example.",
                         "Explain ```Find genes in ductal cells```.", "Explain “in ductal cells”."):
            with self.subTest(question=question):
                original = step(question)
                self.assertEqual(repair_step_constraints(original), original)
        result = repair_step_constraints(step('Is "CFTR" enriched in ductal cells?'))
        self.assertEqual(result["constraints"][-1]["value"], "ductal cell")

    def test_existing_correct_cell_name_is_preserved_without_duplicate(self):
        original = step("Find CFTR in ductal cells.")
        original["constraints"].append({"property": "cell.name", "operator": "=", "value": "ductal cell"})
        self.assertEqual(repair_step_constraints(original), original)

    def test_matching_explicit_cell_id_suppresses_redundant_name(self):
        for name, identifier in (("alpha", "CL_0000171"), ("beta", "CL_0000169"),
                                 ("delta", "CL_0000173"), ("ductal", "CL_0002079"),
                                 ("endothelial", "CL_0000115")):
            with self.subTest(name=name):
                original = step(f"Find CFTR in {name} cells ({identifier}).")
                original["constraints"].append({"property": "id", "operator": "=", "value": identifier})
                self.assertEqual(repair_step_constraints(original), original)
                missing = step(f"Find CFTR in {name} cells ({identifier}).")
                self.assertEqual(repair_step_constraints(missing)["constraints"][-1],
                                 {"property": "id", "operator": "=", "value": identifier})

    def test_existing_singleton_id_set_also_prevents_duplicate_name(self):
        original = step("Find CFTR in ductal cells.")
        original["constraints"].append({"property": "cell.id", "operator": "IN", "value": '["CL_0002079"]'})
        self.assertEqual(repair_step_constraints(original), original)

    def test_conflicting_explicit_cell_id_is_not_repaired_by_guessing(self):
        original = step("Find CFTR in ductal cells (CL_0000169).")
        self.assertEqual(repair_step_constraints(original), original)

    def test_other_existing_filters_are_never_replaced_or_cleared(self):
        original = step("Find CFTR in ductal cells.")
        original["constraints"].append({"property": "condition", "operator": "=", "value": "ND"})
        original["depends_on"] = ["previous"]
        repaired = repair_step_constraints(original)
        self.assertEqual(repaired["constraints"][:-1], original["constraints"])
        self.assertEqual(repaired["depends_on"], ["previous"])

    def test_missing_candidate_cell_filter_is_still_rejected(self):
        repaired = repair_step_constraints(step("Is CFTR enriched in ductal cells?"))
        query = "MATCH (g:Gene)-[r:GENE_ENRICHED_IN]->(c:anatomical_structure) WHERE g.name='CFTR' RETURN g,r,c"
        self.assertIn("missing_required_filter:name", validate_cypher(query, repaired))
        correct = query.replace(" RETURN", " AND c.name='ductal cell' RETURN")
        self.assertEqual(validate_cypher(correct, repaired), [])

    def test_invented_other_cell_does_not_satisfy_repaired_filter(self):
        repaired = repair_step_constraints(step("Is CFTR enriched in ductal cells?"))
        query = "MATCH (g:Gene)-[r:GENE_ENRICHED_IN]->(c:anatomical_structure) WHERE g.name='CFTR' AND c.name='beta cell' RETURN g,r,c"
        reasons = validate_cypher(query, repaired)
        self.assertIn("missing_required_filter:name", reasons)
        self.assertIn("unrequested_identity_filter:name", reasons)

    def test_simple_enrichment_request_uses_verified_schema_and_literal_constraints(self):
        original = repair_step_constraints(step("Is CFTR (gene) specifically enriched in ductal cells (GENE_ENRICHED_IN relation)?"))
        saved = copy.deepcopy(original)
        request = build_generation_question(original)
        self.assertEqual(request, "Find all GENE_ENRICHED_IN relationships from Gene nodes named CFTR to anatomical_structure nodes named ductal cell. Return the gene, cell and relationship with their properties, without LIMIT or list slices.")
        self.assertEqual(original, saved)

    def test_generation_request_is_not_gene_or_cell_specific(self):
        for gene, cell in (("INS", "beta"), ("GCG", "alpha"), ("SST", "delta"), ("VWF", "endothelial")):
            with self.subTest(gene=gene):
                original = repair_step_constraints(step(f"Is {gene} enriched within pancreatic {cell} cells?", [
                    {"property": "name", "operator": "=", "value": gene}]))
                request = build_generation_question(original)
                self.assertIn(f'Gene nodes named {gene}', request)
                self.assertIn(f'anatomical_structure nodes named {cell} cell', request)
                self.assertNotIn("CFTR", request)

    def test_complex_modified_or_unrelated_questions_keep_original_generation_input(self):
        questions = (
            "Is CFTR detected in ductal cells?", "Is CFTR enriched in ductal cells and involved in T1D?",
            "Is CFTR enriched in ductal cells compared with beta cells?",
            "Is CFTR not enriched in ductal cells?", "Is CFTR enriched in ductal cell MUC5B+?",
            "Is CFTR enriched in ductal cell (MUC5B+)?", "Is CFTR enriched in ductal cells in T1D?",
            "Is CFTR enriched in ductal cells with padj < 0.05?", "Explain why CFTR is enriched in ductal cells.",
            'Explain "Is CFTR enriched in ductal cells?"', "Is CFTR enriched in ductal cells? Also return pathways.",
        )
        for question in questions:
            with self.subTest(question=question):
                original = step(question, [{"property": "name", "operator": "=", "value": value}
                                           for value in ("CFTR", "ductal cell")])
                self.assertEqual(build_generation_question(original), question)

    def test_generation_request_never_discards_filters_ids_dependencies_or_limits(self):
        original = repair_step_constraints(step("Is CFTR enriched in ductal cells?"))
        changed = []
        for constraint in ({"property": "condition", "operator": "=", "value": "ND"},
                           {"property": "padj", "operator": "<", "value": "0.05"}):
            variant = copy.deepcopy(original)
            variant["constraints"].append(constraint)
            changed.append(variant)
        for prop, value in (("id", "CL_0002079"), ("name", "beta cell")):
            variant = copy.deepcopy(original)
            variant["constraints"][-1] = {"property": prop, "operator": "=", "value": value}
            changed.append(variant)
        changed += [{**original, "depends_on": ["earlier"]}, {**original, "complete": False},
                    {**original, "constraints": original["constraints"][:1]}]
        for variant in changed:
            with self.subTest(variant=variant):
                self.assertEqual(build_generation_question(variant), original["question"])

    def test_schema_request_does_not_bypass_missing_cell_guard(self):
        original = repair_step_constraints(step("Is CFTR enriched in ductal cells?"))
        self.assertNotEqual(build_generation_question(original), original["question"])
        candidate = "MATCH (g:Gene)-[r:GENE_ENRICHED_IN]->(c:anatomical_structure) WHERE g.name='CFTR' RETURN g,r,c"
        self.assertIn("missing_required_filter:name", validate_cypher(candidate, original))


if __name__ == "__main__":
    unittest.main()
