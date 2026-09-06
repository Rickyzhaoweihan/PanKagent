import copy
import json
import unittest

from pankagent_vnext.evidence_context import MAX_BYTES, compact_evidence


def node(identifier, label="Gene", **properties):
    return {"id": identifier, "labels": [label], "properties": {"id": identifier, "name": identifier, **properties}}


def edge(source, target, kind="COMMON", **properties):
    return {"start_id": source, "end_id": target, "type": kind, "properties": properties}


def evidence(nodes=None, edges=None, rows=None, **extra):
    return {"status": "complete", "graph_version": "test-release", "truncated": False,
            "nodes": nodes or [], "edges": edges or [], "rows": rows or [], **extra}


class EvidenceContextTests(unittest.TestCase):
    def test_rare_edge_after_first_hundred_survives_with_labeled_endpoints(self):
        nodes = [node("g" + str(i)) for i in range(150)] + [node("cell", "anatomical_structure")]
        edges = [edge("g" + str(i), "cell") for i in range(130)]
        edges.append(edge("g149", "cell", "DECISIVE", score=0.02))
        result = compact_evidence([evidence(nodes, edges)])[0]
        rare = next(item for item in result["edges"] if item["type"] == "DECISIVE")
        self.assertEqual(rare["start_id"], "g149")
        visible = {item["id"]: item for item in result["nodes"]}
        self.assertTrue(visible["g149"]["context_stub"])
        self.assertEqual(visible["g149"]["properties"]["name"], "g149")
        self.assertEqual(visible["g149"]["labels"], ["Gene"])
        self.assertEqual(result["edges_count"], 131)
        self.assertEqual(result["context_dropped"]["edges_by_type"], {"COMMON": 31})
        self.assertEqual(len(result["edges"]), 100)
        self.assertTrue(result["context_sampled"])
        self.assertEqual(result["status"], "complete")

    def test_equal_size_common_groups_keep_both_types(self):
        edges = [edge("g", "cell", kind, rank=i) for kind in ["TYPE_A", "TYPE_B"] for i in range(110)]
        result = compact_evidence([evidence([node("g"), node("cell")], edges)])[0]
        self.assertEqual(sum(e["type"] == "TYPE_A" for e in result["edges"]), 50)
        self.assertEqual(sum(e["type"] == "TYPE_B" for e in result["edges"]), 50)

    def test_rare_node_labels_after_first_sixty_survive(self):
        nodes = [node(str(i)) for i in range(80)] + [node("rare", "GO_term")]
        result = compact_evidence([evidence(nodes)])[0]
        self.assertIn("rare", {item["id"] for item in result["nodes"]})
        self.assertEqual(result["nodes_count"], 81)
        self.assertEqual(len(result["nodes"]), 60)
        self.assertEqual(result["context_dropped"]["nodes_by_labels"], {"Gene": 21})

    def test_previous_step_endpoint_gets_a_named_explicit_stub(self):
        steps = {
            "first": evidence([node("prior", "Gene")]),
            "second": evidence([node("cell", "anatomical_structure")], [edge("prior", "cell", "GENE_DETECTED_IN")]),
        }
        result = compact_evidence(steps)
        stub = next(n for n in result[1]["nodes"] if n["id"] == "prior")
        self.assertEqual(stub["id"], "prior")
        self.assertTrue(stub["context_stub"])
        self.assertEqual(stub["context_stub_reason"], "endpoint_from_other_step")
        self.assertEqual(stub["properties"]["name"], "prior")
        self.assertEqual(stub["labels"], ["Gene"])
        self.assertEqual(result[1]["context_cross_step_endpoints"], 1)
        self.assertEqual([r["evidence_id"] for r in result], ["G1", "G2"])

    def test_unknown_endpoint_or_other_release_never_gets_invented_node_facts(self):
        items = [evidence([node("g", name="Other release name")], graph_version="other"),
                 evidence([node("cell")], [edge("g", "cell")])]
        result = compact_evidence(items)[1]
        stub = next(n for n in result["nodes"] if n["id"] == "g")
        self.assertEqual(stub["context_stub_reason"], "endpoint_missing_from_evidence")
        self.assertEqual(stub["properties"], {"id": "g"})
        self.assertEqual(stub["labels"], [])
        self.assertEqual(result["context_missing_endpoint_nodes"], 1)

    def test_empty_and_failed_steps_are_not_dropped_or_renumbered(self):
        result = compact_evidence({
            "a": evidence(status="empty", question="Empty lookup"),
            "b": evidence(status="failed", error="timeout", question="Failed lookup"),
        })
        self.assertEqual([r["status"] for r in result], ["empty", "failed"])
        self.assertEqual([r["evidence_id"] for r in result], ["G1", "G2"])
        self.assertEqual(result[1]["error"], "timeout")
        self.assertEqual(result[0]["nodes_count"], 0)
        self.assertFalse(result[0]["context_sampled"])

    def test_sampling_is_deterministic_and_does_not_mutate_evidence(self):
        item = evidence([node(str(i), "A" if i < 60 else "B") for i in range(100)],
                        [edge(str(i), str(i + 1), "LINK", score=i) for i in range(99)])
        original = copy.deepcopy(item)
        first = compact_evidence({"step": item})
        second = compact_evidence({"step": item})
        self.assertEqual(first, second)
        self.assertEqual(item, original)

    def test_raw_validation_queries_and_statement_messages_are_excluded(self):
        item = evidence(validation=[{"valid": False, "n": 1,
            "candidate_cypher": "PRIVATE_RAW_QUERY", "parameters": {"private": "PARAMETER"},
            "reasons": ["cypher_explain_failed:Neo.ClientError.Statement.SyntaxError:PRIVATE_RAW_QUERY"]}])
        result = compact_evidence([item])[0]
        self.assertNotIn("PRIVATE_RAW_QUERY", json.dumps(result))
        self.assertNotIn("PARAMETER", json.dumps(result))
        self.assertEqual(result["validation"][0]["reasons"], ["cypher_explain_failed:Neo.ClientError.Statement.SyntaxError"])
        self.assertNotIn("n", result["validation"][0])

    def test_recovered_generation_attempts_are_not_missing_graph_evidence(self):
        item = evidence([node("g")], validation=[
            {"valid": False, "n": 1, "candidate_cypher": "REJECTED_QUERY",
             "reasons": ["incomplete_limit_or_slice"]},
            {"valid": True, "n": 8, "candidate_cypher": "ACCEPTED_QUERY", "reasons": []},
        ])
        original = copy.deepcopy(item)
        result = compact_evidence([item])[0]
        self.assertEqual(result["validation"], [{"valid": True, "reasons": []}])
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["truncated"])
        self.assertFalse(result["context_sampled"])
        self.assertNotIn("context_content_omissions", result)
        self.assertNotIn("incomplete_limit_or_slice", json.dumps(result))
        self.assertNotIn("QUERY", json.dumps(result))
        self.assertEqual(item, original)

    def test_arbitrarily_many_recovered_checks_do_not_mark_context_sampled(self):
        checks = [{"valid": False, "n": 8, "reasons": ["rejected_candidate"]} for _ in range(100)]
        checks.append({"valid": True, "n": 8, "status": "accepted", "reasons": []})
        result = compact_evidence([evidence([node("g")], validation=checks)])[0]
        self.assertEqual(result["validation"], [{"valid": True, "status": "accepted", "reasons": []}])
        self.assertFalse(result["context_sampled"])
        self.assertNotIn("context_content_omissions", result)

    def test_execution_failure_after_accepted_query_is_terminal_validation(self):
        checks = [{"valid": True, "n": 8, "reasons": []},
                  {"valid": False, "reasons": ["graph_execution_failed:ServiceUnavailable"]},
                  None]
        result = compact_evidence([evidence(status="failed", validation=checks)])[0]
        self.assertEqual(result["validation"], [{"valid": False, "reasons": ["graph_execution_failed:ServiceUnavailable"]}])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["context_sampled"])

    def test_multikilobyte_property_fields_are_labeled_and_json_safe(self):
        result = compact_evidence([evidence([node("g", description="多" * 10000)])])[0]
        self.assertIn("[context text clipped]", result["nodes"][0]["properties"]["description"])
        self.assertEqual(result["nodes"][0]["id"], "g")
        self.assertTrue(result["context_sampled"])
        self.assertEqual(result["context_content_omissions"]["clipped_strings"], 1)
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False)), result)

    def test_oversized_normal_context_uses_reduced_caps_and_keeps_rare_type(self):
        properties = {"measurement_" + str(i): "x" * 200 for i in range(20)}
        edges = [edge("g", "cell", "COMMON", **properties) for _ in range(110)]
        edges.append(edge("g", "cell", "RARE", **properties))
        item = evidence([node("g"), node("cell")], edges, rows=[{"v": i} for i in range(50)])
        result = compact_evidence([item])[0]
        self.assertEqual(result["context_compaction"], "reduced")
        self.assertEqual(len(result["edges"]), 15)
        self.assertEqual(len(result["rows"]), 5)
        self.assertIn("RARE", {item["type"] for item in result["edges"]})
        self.assertLessEqual(len(json.dumps([result], ensure_ascii=False, separators=(",", ":")).encode()), MAX_BYTES)

    def test_stable_identifiers_are_never_silently_shortened(self):
        huge_id = "g" * (MAX_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "evidence_context_too_large"):
            compact_evidence([evidence([node(huge_id)])])

    def test_total_bound_applies_across_steps(self):
        items = [evidence(question="x" * 1200) for _ in range(200)]
        with self.assertRaisesRegex(ValueError, "evidence_context_too_large"):
            compact_evidence(items)

    def test_invalid_shape_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "invalid_evidence_node"):
            compact_evidence([evidence(nodes=[{"labels": ["Gene"]}])])
        with self.assertRaisesRegex(ValueError, "invalid_evidence_shape"):
            compact_evidence("not a list")


if __name__ == "__main__":
    unittest.main()
