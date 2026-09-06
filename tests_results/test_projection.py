import copy
import math
import unittest

from pankgraph_results.projection import project_evidence


def evidence():
    return {"graph_version": "RL08_04", "completeness": "complete", "nodes": [
        {"id": "ENSG1", "labels": ["Gene", "coding_element"], "properties": {"id": "ENSG1", "name": "INS"}},
        {"id": "CL_1", "labels": ["anatomical_structure"], "properties": {"id": "CL_1", "name": "beta cell"}},
    ], "edges": [{"start_id": "ENSG1", "end_id": "CL_1", "type": "GENE_ENRICHED_IN", "properties": {"p_adj": 0.004}}]}


class ProjectionTests(unittest.TestCase):
    def test_canonical_identity_and_properties_are_preserved(self):
        source = evidence()
        before = copy.deepcopy(source)
        result = project_evidence(source, ["ENSG1"])
        self.assertEqual(source, before)
        gene = next(node for node in result["combined_query_result"]["nodes"] if node["~id"] == "ENSG1")
        self.assertEqual(gene["~properties"], source["nodes"][0]["properties"])
        self.assertEqual(gene["display_type"], "gene")
        self.assertEqual(gene["display_label"], "INS")
        self.assertIn("Gene", gene["~labels"])
        edge = result["combined_query_result"]["edges"][0]
        self.assertEqual((edge["~start"], edge["~end"], edge["~type"]), ("ENSG1", "CL_1", "GENE_ENRICHED_IN"))
        self.assertEqual(edge["display_type"], "gene_enriched_in")
        self.assertEqual(result["core_nodes"], ["ENSG1"])
        result["combined_query_result"]["nodes"][0]["~properties"]["name"] = "changed"
        self.assertEqual(source, before)

    def test_hash_is_order_stable_parallel_sensitive_and_release_scoped(self):
        source = evidence()
        source["edges"].append({**source["edges"][0], "properties": {"p_adj": 0.03}})
        first = project_evidence(source)["combined_query_result"]
        source["nodes"].reverse()
        source["edges"].reverse()
        second = project_evidence(source)["combined_query_result"]
        self.assertEqual(first, second)
        ids = {edge["~id"] for edge in first["edges"]}
        self.assertEqual(len(ids), 2)
        source["graph_version"] = "different-release"
        self.assertTrue(ids.isdisjoint({edge["~id"] for edge in project_evidence(source)["combined_query_result"]["edges"]}))

    def test_real_edge_id_and_source_target_compatibility(self):
        source = evidence()
        source["edges"] = [{"id": "real-edge-42", "source": "ENSG1", "target": "CL_1", "type": "GENE_ENRICHED_IN", "properties": {"id": "other-property"}}]
        edge = project_evidence(source)["combined_query_result"]["edges"][0]
        self.assertEqual(edge["~id"], "real-edge-42")
        self.assertEqual(edge["~properties"]["id"], "other-property")
        source["edges"].append({**source["edges"][0], "target": "ENSG1"})
        with self.assertRaisesRegex(ValueError, "relationship ID"):
            project_evidence(source)

    def test_steps_deduplicate_and_preserve_failed_empty_metadata(self):
        source = evidence()
        steps = [{**copy.deepcopy(source), "step_id": "G1", "status": "complete"},
                 {"step_id": "G2", "graph_version": "RL08_04", "status": "failed", "nodes": [], "edges": []},
                 {"step_id": "G3", "graph_version": "RL08_04", "status": "empty", "nodes": [], "edges": []}]
        result = project_evidence({"steps": steps})
        self.assertEqual(result["full_evidence"]["scientific_completeness"], "partial")
        self.assertEqual([step["status"] for step in result["full_evidence"]["steps"]], ["complete", "failed", "empty"])
        source["steps"] = steps
        deduplicated = project_evidence(source)
        self.assertEqual(deduplicated["full_evidence"]["node_count"], 2)
        self.assertEqual(deduplicated["full_evidence"]["edge_count"], 1)

    def test_dangling_edges_are_visible_omissions_without_invented_nodes(self):
        source = evidence()
        source["edges"].append({"start_id": "ENSG1", "end_id": "unknown", "type": "X", "properties": {}})
        result = project_evidence(source)
        self.assertEqual(result["metadata"]["projection"]["dangling_edges"], 1)
        self.assertEqual(len(result["combined_query_result"]["nodes"]), 2)
        self.assertEqual(len(result["combined_query_result"]["edges"]), 1)
        self.assertEqual(result["full_evidence"]["scientific_completeness"], "complete")

    def test_release_conflict_fails_before_aliasing(self):
        source = evidence()
        source["steps"] = [{"graph_version": "classic8687", "nodes": [], "edges": []}]
        with self.assertRaisesRegex(ValueError, "conflicting graph releases"):
            project_evidence(source)

    def test_focus_uses_verified_primary_entities_then_graph_seed(self):
        source = evidence()
        source["steps"] = [
            {"purpose": "context", "resolved_entities": [{"id": "CL_1", "state": "resolved"}]},
            {"purpose": "primary", "resolved_entities": [{"id": "ENSG1", "state": "resolved"}, {"id": "CL_1", "state": "ambiguous"}]},
        ]
        result = project_evidence(source, ["absent"])
        self.assertEqual(result["core_nodes"], ["ENSG1"])
        self.assertEqual(result["metadata"]["missing_focus_ids"], ["absent"])
        del source["steps"]
        result = project_evidence(source)
        self.assertEqual(len(result["core_nodes"]), 1)
        self.assertEqual(result["metadata"]["focus_source"], "graph_seed")

    def test_properties_cannot_override_identity_or_layout_and_nonfinite_is_explicit(self):
        source = evidence()
        source["nodes"][0]["properties"].update({"id": "unrelated-property", "~id": "forged", "layout_mode": "genome_mode", "value": math.nan})
        result = project_evidence(source)
        gene = next(node for node in result["combined_query_result"]["nodes"] if node["~id"] == "ENSG1")
        self.assertEqual(gene["~properties"]["~id"], "forged")
        self.assertNotIn("layout_mode", result)
        self.assertIsNone(gene["~properties"]["value"])
        self.assertEqual(result["metadata"]["projection"]["nonfinite_values"], 1)

    def test_empty_and_large_valid_evidence_are_not_display_truncation(self):
        empty = project_evidence({"status": "empty", "graph_version": "v1", "nodes": [], "edges": []})
        self.assertEqual(empty["full_evidence"]["scientific_completeness"], "empty")
        source = evidence()
        source["nodes"] += [{"id": str(i), "labels": ["Gene"], "properties": {}} for i in range(250)]
        result = project_evidence(source)
        self.assertEqual(result["full_evidence"]["node_count"], 252)


if __name__ == "__main__":
    unittest.main()
