"""Offline checks against the pinned BIM meanings and the current graph schema."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from pankagent_vnext.answer_router import AnswerSkillRouter, BUNDLE


def node(identifier, labels="Gene", **properties):
    return {"id": identifier, "labels": labels if isinstance(labels, list) else [labels],
            "properties": {"id": identifier, "name": identifier, **properties}}


def edge(kind, start="ENSG00000129965", end="CL:0000171", **properties):
    return {"start_id": start, "end_id": end, "type": kind, "properties": properties}


def step(nodes=None, edges=None, rows=None, **extra):
    return {"status": "complete", "graph_version": "rl-2026-08-04",
            "nodes": nodes or [], "edges": edges or [], "rows": rows or [], **extra}


def selected(result):
    return {rule["id"] for rule in result.profile["selected_rules"]}


class AnswerRouterTests(unittest.TestCase):
    def setUp(self):
        self.router = AnswerSkillRouter()

    def test_modern_rna_evidence_keeps_detection_enrichment_and_de_distinct(self):
        result = self.router.select([step(
            [node("ENSG00000129965", "Gene", name="INS"),
             node("CL:0000171", "anatomical_structure", name="pancreatic alpha cell")],
            [edge("T1D_DEG_IN", adjusted_p_value=0.01, log2_fold_change=0.8),
             edge("GENE_DETECTED_IN", condition="ND", median_donor_cpm=20),
             edge("GENE_ENRICHED_IN", condition="ND", padj=0.02)],
        )])
        self.assertTrue({"edge.t1d_deg_in", "edge.gene_detected_in", "edge.gene_enriched_in"} <= selected(result))
        for caveat in ("RNA differential expression", "pseudobulk scRNA-seq detection",
                       "ND-only one-vs-rest enrichment", "ambient RNA", "Avoid absolute absence"):
            self.assertIn(caveat, result.guidance)
        self.assertFalse(any(rule.startswith("composite.rna_atac") for rule in selected(result)))
        self.assertNotIn("clinical.recorded_t1d_stage", selected(result))

    def test_current_node_labels_and_legacy_compounds_resolve_to_same_meanings(self):
        cases = {
            "Gene": ("GENE", "node.gene"),
            "coding_elements;gene": ("GENE", "node.gene"),
            "variants;sequence_variant;": ("VARIANT", "node.variant"),
            "sequence_variant;variants": ("VARIANT", "node.variant"),
            "OCR_peak;regulatory_elements": ("OCR_PEAK", "node.ocr_peak"),
            "GO_term": ("GO", "node.go"),
            "ontology;gene_ontology": ("GO", "node.go"),
            "ontology;kegg": ("KEGG", "node.kegg"),
            "reactome;ontology": ("REACTOME", "node.reactome"),
            "Sample_node": ("SAMPLE", "node.sample"),
            "donor;provenance": ("DONOR", "node.donor"),
            "data_modality": ("DATA_MODALITY", "node.data_modality"),
        }
        for label, (canonical, rule_id) in cases.items():
            with self.subTest(label=label):
                result = self.router.select([step([node("x", label)])])
                self.assertIn(canonical, result.profile["matched_schema"]["nodes"])
                self.assertIn(rule_id, selected(result))

    def test_compound_node_tokens_are_sets_and_do_not_promote_provenance_to_donor(self):
        result = self.router.select([step([node("x", " ontology ; GO_term ; provenance ")])])
        self.assertEqual(result.profile["matched_schema"]["nodes"], ["GO"])
        self.assertIn("node.go", selected(result))
        self.assertNotIn("node.donor", selected(result))
        self.assertIn(" provenance ", result.profile["unknown_schema"]["nodes"])
        reverse = self.router.select([step([node("x", "provenance;donor")])])
        self.assertIn("node.donor", selected(reverse))

    def test_current_and_legacy_edge_aliases_are_exact_not_substrings(self):
        cases = {
            " CMDKP_EFFECTOR_GENE_OF ": ("EFFECTOR_GENE_OF", "edge.effector_gene_of"),
            "effector_gene_of;": ("EFFECTOR_GENE_OF", "edge.effector_gene_of"),
            "ASSOCIATED_WITH_GO": ("FUNCTION_ANNOTATION", "edge.function_annotation"),
            "function_annotation;GO": ("FUNCTION_ANNOTATION", "edge.function_annotation"),
            "fGSEA_enriched_in": ("FGSEA_ENRICHED_IN", "edge.fgsea_enriched_in"),
            "MARKER_GENE_OF": ("MARKER_GENE_OF", "edge.marker_gene_of"),
            "part_of_GWAS_signal": ("PART_OF_GWAS_SIGNAL", "edge.part_of_gwas_signal"),
            "signal_COLOC_with": ("SIGNAL_COLOC_WITH", "edge.signal_coloc_with"),
        }
        for alias, (canonical, rule_id) in cases.items():
            with self.subTest(alias=alias):
                result = self.router.select([step(edges=[edge(alias)])])
                self.assertEqual(result.profile["matched_schema"]["edges"], [canonical])
                self.assertIn(rule_id, selected(result))
        invalid = ["LIKELY_GENE_DETECTED_IN", "GENE_DETECTED_IN;unrelated", "not_gene_activity_score_in"]
        result = self.router.select([step(edges=[edge(kind) for kind in invalid])])
        self.assertEqual(result.profile["matched_schema"]["edges"], [])
        self.assertEqual(set(result.profile["unknown_schema"]["edges"]), set(invalid))
        self.assertEqual(result.guidance, "")

    def test_rna_atac_composites_share_one_caution_without_inventing_modality(self):
        result = self.router.select({
            "rna": step(edges=[edge("GENE_DETECTED_IN")]),
            "atac": step(edges=[edge("GENE_ACTIVITY_SCORE_IN"), edge("OCR_PEAK_IN")]),
        })
        rules = {rule["id"]: rule for rule in result.profile["selected_rules"]}
        self.assertIn("composite.rna_atac.activity", rules)
        self.assertEqual(rules["composite.rna_atac.peak"]["shared_guidance_with"], "composite.rna_atac.activity")
        self.assertEqual(result.guidance.count("Discordant RNA and ATAC signals are not contradictions by themselves."), 1)
        self.assertIn("not RNA expression and not direct transcription rate", result.guidance)
        self.assertIn("not proof that the linked region regulates a specific gene", result.guidance)
        for kinds in (["GENE_ACTIVITY_SCORE_IN", "OCR_PEAK_IN"], ["GENE_DETECTED_IN", "T1D_DEG_IN"]):
            with self.subTest(kinds=kinds):
                result = self.router.select([step(edges=[edge(kind) for kind in kinds])])
                self.assertFalse(any(rule.startswith("composite.rna_atac") for rule in selected(result)))

    def test_genetics_composite_requires_distinct_supported_edge_types(self):
        one_type = self.router.select([step(edges=[edge("PART_OF_QTL_SIGNAL") for _ in range(20)])])
        self.assertNotIn("composite.genetics", selected(one_type))
        result = self.router.select([step(edges=[edge("PART_OF_QTL_SIGNAL"), edge("SIGNAL_COLOC_WITH")])])
        self.assertIn("composite.genetics", selected(result))
        self.assertIn("prioritized candidate or shared-signal evidence", result.guidance)
        self.assertIn("not proof that the variant is causal", self.router.select(
            [step(edges=[edge("PART_OF_GWAS_SIGNAL")])]).guidance)

    def test_pathway_composite_requires_annotation_enrichment_and_ontology(self):
        nodes = [node("R-HSA-123", "reactome")]
        edges = [edge("pathway_annotation;reactome"), edge("FGSEA_ENRICHED_IN")]
        result = self.router.select([step(nodes, edges)])
        self.assertIn("composite.pathway_annotation_enrichment", selected(result))
        self.assertIn("separate static annotation membership from enrichment results", result.guidance)
        self.assertIn("not necessarily upregulation of every pathway gene", result.guidance)
        for incomplete in (step(nodes, edges[:1]), step(nodes, edges[1:]), step(edges=edges)):
            with self.subTest(incomplete=incomplete):
                self.assertNotIn("composite.pathway_annotation_enrichment", selected(self.router.select([incomplete])))

    def test_every_supported_type_is_scanned_after_first_hundred_records(self):
        labels = ["Gene", "sequence_variant", "OCR_peak", "GO_term", "kegg", "reactome",
                  "anatomical_structure", "Sample_node", "donor", "data_modality"]
        kinds = ["T1D_DEG_IN", "GENE_DETECTED_IN", "GENE_ENRICHED_IN", "GENE_ACTIVITY_SCORE_IN",
                 "OCR_PEAK_IN", "EFFECTOR_GENE_OF", "PHYSICAL_INTERACTION", "GENETIC_INTERACTION",
                 "FUNCTION_ANNOTATION", "FGSEA_ENRICHED_IN", "MARKER_GENE_OF", "PART_OF_GWAS_SIGNAL",
                 "SIGNAL_COLOC_WITH", "PART_OF_QTL_SIGNAL", "PATHWAY_ANNOTATION"]
        result = self.router.select([step(
            [node(str(i), "unrecognized") for i in range(125)] + [node(label, label) for label in labels],
            [edge("unrecognized") for _ in range(125)] + [edge(kind) for kind in kinds],
        )])
        self.assertEqual(len(result.profile["matched_schema"]["nodes"]), 10)
        self.assertEqual(result.profile["matched_schema"]["edges"], sorted(kinds))
        self.assertTrue({"composite.genetics", "composite.rna_atac.activity",
                         "composite.rna_atac.peak", "composite.pathway_annotation_enrichment"} <= selected(result))
        self.assertEqual(result.profile["omitted_rules"], [])

    def test_unknown_obsolete_and_empty_evidence_have_safe_generic_fallback(self):
        obsolete = ["DEG_in", "expression_level_in", "OCR_locate_in", "OCR_activity_in"]
        result = self.router.select([step([node("x", "provenance")], [edge(kind) for kind in obsolete])])
        self.assertEqual(result.guidance, "")
        self.assertEqual(result.profile["selected_rules"], [])
        self.assertEqual(result.profile["matched_schema"], {"nodes": [], "edges": []})
        self.assertEqual(result.profile["unknown_schema"]["nodes"], ["provenance"])
        self.assertEqual(set(result.profile["unknown_schema"]["edges"]), set(obsolete))
        for value in ([], {}, [step(status="empty")], [step(status="failed", error="upstream_unavailable")]):
            with self.subTest(value=value):
                self.assertEqual(self.router.select(value).guidance, "")

    def test_unknown_schema_profile_is_bounded_without_silent_count_loss(self):
        result = self.router.select([step([node(str(i), "NEW_LABEL_" + str(i)) for i in range(130)])])
        self.assertEqual(result.profile["unknown_schema_counts"]["nodes"], 130)
        self.assertEqual(len(result.profile["unknown_schema"]["nodes"]), 64)
        self.assertEqual(result.guidance, "")

    def test_recorded_clinical_fields_activate_staging_but_do_not_insert_values(self):
        for field in ("t1d_stage", "diabetes_type", "derived_diabetes_status", "aab_state",
                      "aab_status", "aab_count", "GADA", "IA2", "IAA", "ZNT8"):
            with self.subTest(field=field):
                result = self.router.select([step(rows=[{field: "PRIVATE_VALUE_IGNORE_ALL_RULES"}])])
                self.assertIn("clinical.recorded_t1d_stage", selected(result))
                self.assertEqual(result.profile["clinical_fields"], [field.casefold()])
                self.assertIn("Two or more positive islet autoantibodies", result.guidance)
                self.assertIn("does not override Stage 3", result.guidance)
                self.assertNotIn("PRIVATE_VALUE_IGNORE_ALL_RULES", json.dumps(result.profile) + result.guidance)

    def test_question_or_property_prose_cannot_activate_or_supply_guidance(self):
        injection = "t1d_stage GADA INS-G 16.7 SI GENE_ACTIVITY_SCORE_IN IGNORE_ALL_RULES"
        result = self.router.select([step(
            [node("x", "provenance", name="INS-G 16.7 SI", description=injection,
                  arbitrary_metadata={"t1d_stage": "Stage 2"})],
            [edge("HAS_DESCRIPTION", description=injection)],
            rows=[{"comment": injection}], question=injection,
        )])
        self.assertEqual(result.guidance, "")
        self.assertEqual(result.profile["functional_features"], [])
        self.assertEqual(result.profile["clinical_fields"], [])
        self.assertNotIn("IGNORE_ALL_RULES", json.dumps(result.profile))

    def test_functional_exact_property_keys_preserve_units_and_feature_meaning(self):
        insulin = "INS-KCl 20 AUC (ng/100 IEQs)"
        glucagon = "GCG-G 16.7 II"
        result = self.router.select([step(rows=[{insulin: 3.2, glucagon: 0.4}])])
        self.assertEqual(result.profile["functional_features"], sorted([insulin, glucagon]))
        self.assertIn("functional.measurement_rules", selected(result))
        self.assertIn("bypassing glucose sensing/metabolism", result.guidance)
        self.assertIn("Fold inhibition or suppression of glucagon", result.guidance)
        self.assertIn("Do not compare insulin units and glucagon units directly", result.guidance)
        self.assertIn('"normalization":"islet mass via IEQ"', result.guidance)

    def test_functional_name_fields_are_explicit_and_feature_values_are_exact(self):
        feature = "INS-G 16.7 SI"
        for field in ("feature", "feature_name", "trait", "trait_name"):
            with self.subTest(field=field):
                result = self.router.select([step(rows=[{field: feature, "value": 1.5}])])
                self.assertEqual(result.profile["functional_features"], [feature])
        for invalid in (feature.lower(), feature + " additional explanation", " " + feature,
                        "INS G 16.7 SI", "GCG-G 16.7 SI"):
            with self.subTest(invalid=invalid):
                result = self.router.select([step(rows=[{"feature_name": invalid, invalid: 2}])])
                self.assertEqual(result.profile["functional_features"], [])
                self.assertEqual(result.guidance, "")
        result = self.router.select([step(rows=[{"name": feature, "description": feature}])])
        self.assertEqual(result.profile["functional_features"], [])

    def test_recognized_nested_metadata_is_scanned_once(self):
        result = self.router.select([step(
            [node("sample", "Sample_node", functional_metadata={"feature_name": "INS-G 16.7 SI"})],
            [edge("HAS_TRAIT", clinical_metadata={"t1d_stage": "Stage 1"})],
            rows=[{"trait_meta": {"trait_name": "GCG-G 16.7 II"}}],
        )])
        self.assertEqual(result.profile["functional_features"], ["GCG-G 16.7 II", "INS-G 16.7 SI"])
        self.assertEqual(result.profile["clinical_fields"], ["t1d_stage"])
        deeply_nested = self.router.select([step(rows=[{"trait_meta": {
            "functional_metadata": {"feature_name": "INS-G 16.7 SI"},
            "clinical_metadata": {"t1d_stage": "Stage 1"}}}])])
        self.assertEqual(deeply_nested.guidance, "")

    def test_cache_identity_uses_schema_not_question_ids_values_or_release(self):
        first = [step([node("g", "Gene", name="INS")], [edge("GENE_DETECTED_IN", score=0.2)],
                      question="Where is INS detected?")]
        result = self.router.select(first)
        self.assertFalse(result.profile["cache_hit"])
        changed = [step([node("other", "gene", name="GCG")], [edge("gene_detected_in", score=999)],
                        question="Different human request", graph_version="other-release")]
        again = self.router.select(changed)
        self.assertTrue(again.profile["cache_hit"])
        self.assertEqual(result.guidance, again.guidance)
        self.assertEqual(result.profile["profile_id"], again.profile["profile_id"])
        changed[0]["edges"].append(edge("GENE_ACTIVITY_SCORE_IN"))
        new_schema = self.router.select(changed)
        self.assertFalse(new_schema.profile["cache_hit"])
        self.assertNotEqual(result.profile["profile_id"], new_schema.profile["profile_id"])

    def test_cache_keeps_functional_identity_but_not_measurement_values(self):
        result = self.router.select([step(rows=[{"feature_name": "INS-G 16.7 SI", "value": 2}])])
        same = self.router.select([step(rows=[{"feature_name": "INS-G 16.7 SI", "value": 200}])])
        other = self.router.select([step(rows=[{"feature_name": "GCG-G 16.7 II", "value": 2}])])
        self.assertEqual(result.profile["profile_id"], same.profile["profile_id"])
        self.assertTrue(same.profile["cache_hit"])
        self.assertNotEqual(result.profile["profile_id"], other.profile["profile_id"])

    def test_returned_profile_cannot_mutate_cached_rules_or_scan_diagnostics(self):
        source = [step([node("g")], [edge("GENE_DETECTED_IN")])]
        first = self.router.select(source)
        expected_rules = copy.deepcopy(first.profile["selected_rules"])
        first.profile["selected_rules"][0]["source"]["key"] = "CORRUPTED"
        first.profile["selected_rules"].clear()
        first.profile["matched_schema"]["nodes"].append("INVENTED")
        source[0]["nodes"].append(node("unknown", "new_release_label"))
        again = self.router.select(source)
        self.assertTrue(again.profile["cache_hit"])
        self.assertEqual(again.profile["selected_rules"], expected_rules)
        self.assertEqual(again.profile["matched_schema"]["nodes"], ["GENE"])
        self.assertEqual(again.profile["unknown_schema"]["nodes"], ["new_release_label"])

    def test_routing_is_order_independent_and_lru_cache_is_bounded(self):
        router = AnswerSkillRouter(cache_size=2)
        evidence = [step([node("g"), node("cell", "anatomical_structure")],
                         [edge("GENE_DETECTED_IN"), edge("GENE_ACTIVITY_SCORE_IN")])]
        first = router.select(evidence)
        evidence[0]["nodes"].reverse()
        evidence[0]["edges"].reverse()
        again = router.select(evidence)
        self.assertEqual(first.guidance, again.guidance)
        self.assertEqual(first.profile["profile_id"], again.profile["profile_id"])
        router.select([step(edges=[edge("T1D_DEG_IN")])])
        router.select([step(edges=[edge("EFFECTOR_GENE_OF")])])
        self.assertFalse(router.select(evidence).profile["cache_hit"])

    def test_select_does_not_read_skill_files_on_cache_hit_or_miss(self):
        self.router.select([step([node("g")])])
        with patch.object(Path, "read_bytes", side_effect=AssertionError("runtime file read")), \
             patch.object(Path, "read_text", side_effect=AssertionError("runtime file read")):
            cached = self.router.select([step([node("other")])])
            fresh = self.router.select([step(edges=[edge("GENE_ACTIVITY_SCORE_IN")])])
        self.assertTrue(cached.profile["cache_hit"])
        self.assertFalse(fresh.profile["cache_hit"])
        self.assertIn("edge.gene_activity_score_in", selected(fresh))

    def test_rule_budget_omits_whole_guidance_blocks_and_preserves_cautions(self):
        evidence = [step([node("g"), node("cell", "anatomical_structure")],
                         [edge("GENE_DETECTED_IN"), edge("GENE_ACTIVITY_SCORE_IN"), edge("T1D_DEG_IN")])]
        limited = AnswerSkillRouter(max_chars=1000).select(evidence)
        full = self.router.select(evidence)
        self.assertLessEqual(len(limited.guidance), 1000)
        self.assertEqual(len(limited.guidance), limited.profile["guidance_chars"])
        self.assertTrue(limited.profile["omitted_rules"])
        self.assertNotEqual(limited.profile["profile_id"], full.profile["profile_id"])
        self.assertIn("edge.gene_activity_score_in", selected(limited))
        blocks = []
        for rule in limited.profile["selected_rules"]:
            if "shared_guidance_with" in rule:
                continue
            source = rule["source"]
            text = self.router.files[source["file"]][source["section"]][source["key"]]
            blocks.append(f"\n[{rule['id']}]\n{text}\n")
        self.assertEqual(limited.guidance, "".join(blocks))
        for omitted in limited.profile["omitted_rules"]:
            self.assertNotIn(f"\n[{omitted}]\n", limited.guidance)

    def test_pinned_bundle_profile_records_verified_content_identity(self):
        result = self.router.select([])
        manifest_bytes = (BUNDLE / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        self.assertEqual(result.profile["source_commit"], "40cb7f5b08a2082a4f67ae7198591d92fa0c175d")
        self.assertEqual(result.profile["bundle_sha256"], hashlib.sha256(manifest_bytes).hexdigest())
        for relative, expected in manifest["sha256"].items():
            self.assertEqual(hashlib.sha256((BUNDLE / relative).read_bytes()).hexdigest(), expected)

    def test_pinned_file_corruption_fails_before_any_routing(self):
        for relative in ("bim/schema_skill.json", "upstream/schema_skill.json",
                         "bim/functional_data_interpretation_skill.json", "bim/general_interpretation.json"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                bundle = Path(temporary) / "bundle"
                shutil.copytree(BUNDLE, bundle)
                path = bundle / relative
                path.write_bytes(path.read_bytes() + b" ")
                with self.assertRaisesRegex(ValueError, "answer_skill_checksum_mismatch"):
                    AnswerSkillRouter(bundle)

    def test_manifest_rejects_path_escape_unpinned_source_and_ambiguous_rules(self):
        def outside_path(manifest):
            manifest["sha256"]["../outside.json"] = "0" * 64

        def invalid_commit(manifest):
            manifest["source"]["commit"] = "main"

        def ambiguous_alias(manifest):
            manifest["aliases"]["nodes"]["DONOR"].append("Gene")

        def unbounded_rule(manifest):
            manifest["rules"][0]["match"] = {}

        def unsupported_rule(manifest):
            manifest["rules"][0]["match"]["question_contains"] = "insulin"

        def duplicate_rule(manifest):
            manifest["rules"].append(copy.deepcopy(manifest["rules"][0]))

        for mutate, expected in ((outside_path, "invalid_answer_skill_path"),
                                 (invalid_commit, "invalid_answer_skill_manifest"),
                                 (ambiguous_alias, "ambiguous_answer_skill_alias"),
                                 (unbounded_rule, "unbounded_answer_skill_rule"),
                                 (unsupported_rule, "unsupported_answer_skill_predicate"),
                                 (duplicate_rule, "invalid_answer_skill_rule")):
            with self.subTest(mutate=mutate.__name__), tempfile.TemporaryDirectory() as temporary:
                bundle = Path(temporary) / "bundle"
                shutil.copytree(BUNDLE, bundle)
                manifest_path = bundle / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                mutate(manifest)
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, expected):
                    AnswerSkillRouter(bundle)


if __name__ == "__main__":
    unittest.main()
