"""Offline behavioral tests for graph composition and model/tool feedback.

No model endpoint, database, service, or repository branch is touched.
Run: python3 -m unittest discover -s tests -v (from the experiment directory).
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from simple_agent.core import Agent, Feedback, SchemaPack, compile_pattern


BIO_PACK = {
    "release": "offline-biological-fixture",
    "nodes": {"Gene": ["id", "name"], "Cell": ["id", "name"]},
    "relations": {
        "DETECTED_IN": {
            "paths": [{"source": ["Gene"], "target": ["Cell"]}],
            "properties": ["adjusted_p_value", "condition"],
        },
        "INTERACTS": {
            "paths": [{"source": ["Gene"], "target": ["Gene"]}],
            "properties": ["confidence"],
        },
    },
    "identity": {"Gene": ["id", "name"], "Cell": ["id", "name"]},
}

SECOND_PACK = {
    "release": "offline-citation-fixture",
    "nodes": {"Author": ["id", "display_name"], "Paper": ["id", "title"]},
    "relations": {
        "WROTE": {
            "paths": [{"source": ["Author"], "target": ["Paper"]}],
            "properties": ["year"],
        }
    },
    "identity": {"Author": ["id", "display_name"], "Paper": ["id", "title"]},
}


def node(key, label="Gene", filters=None):
    return {"key": key, "label": label, "filters": filters or []}


def edge(key, source, target, kind="INTERACTS", filters=None, direction="out"):
    return {"key": key, "source": source, "target": target, "type": kind,
            "filters": filters or [], "direction": direction}


def comparison(property, op, value):
    return {"property": property, "op": op, "value": value}


def detection_pattern(property="adjusted_p_value"):
    return {"nodes": [node("g", filters=[comparison("id", "eq", "gene-1")]),
                      node("c", "Cell", [comparison("id", "eq", "cell-1")])],
            "edges": [edge("r", "g", "c", "DETECTED_IN",
                           [comparison(property, "lt", 0.05)])]}


def tool_block(name, args, ident="call-1"):
    return {"type": "tool_use", "id": ident, "name": name, "input": args}


def finish(status="complete", query_id="q1"):
    return tool_block("finish", {
        "status": status, "query_id": query_id,
        "standardized_question": "Show gene-1 detection in cell-1 with adjusted p-value < 0.05.",
        "answer": "Matching graph records returned." if status == "complete" else status,
        "condition_audit": [{"condition": "gene-1 in cell-1 at adjusted p-value < 0.05", "binding": "g.id, c.id, r.adjusted_p_value"}],
        "unfulfilled_conditions": [],
    })


def latest_feedback(request):
    last = request["messages"][-1]["content"]
    if not isinstance(last, list):
        raise AssertionError("Expected a tool-result message before the next inference")
    return [json.loads(item["content"]) for item in last]


class OfflineTransport:
    """Script model decisions; test actual compiler/loop, not a fabricated loop."""

    def __init__(self, decisions, query_result=None):
        self.decisions = list(decisions)
        self.requests = []
        self.query_result = query_result or {
            "status": "complete",
            "graph": {"nodes": [{"id": "gene-1"}, {"id": "cell-1"}],
                      "edges": [{"id": "edge-1"}]},
            "provenance": {"release": "offline-biological-fixture"},
        }

    async def call(self, action, request):
        self.requests.append((action, copy.deepcopy(request)))
        if action == "model":
            if not self.decisions:
                raise AssertionError("Unexpected extra inference call")
            decision = self.decisions.pop(0)
            content = decision(request) if callable(decision) else decision
            return {"content": content, "usage": {"input_tokens": 10, "output_tokens": 3}}
        if action == "query":
            if isinstance(self.query_result, Exception):
                raise self.query_result
            return copy.deepcopy(self.query_result)
        if action == "resolve":
            return {"status": "candidates", "candidates": [{"id": "gene-1", "name": request["text"]}]}
        if action == "values":
            return {"status": "sampled", "values": [0.01, 0.04], "types": ["float"], "partial": True}
        raise AssertionError("Unexpected transport action: " + action)


class PackMixin:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pack = self.make_pack(BIO_PACK, "bio.json")

    def make_pack(self, value, name):
        path = Path(self.temp.name) / name
        path.write_text(json.dumps(value))
        return SchemaPack(path)


class CompilerTests(PackMixin, unittest.TestCase):
    def test_values_are_parameters_even_with_cypher_injection_text(self):
        text = "CFTR' }) DETACH DELETE n // `; $p0"
        pattern = {"nodes": [node("g", filters=[comparison("name", "eq", text)])], "edges": []}
        cypher, params = compile_pattern(self.pack, pattern)
        self.assertNotIn(text, cypher)
        self.assertNotIn("DELETE", cypher)
        self.assertEqual(params["p0"], text)
        self.assertIn("n0.`name` = $p0", cypher)

    def test_numeric_threshold_has_conversion_and_exact_value(self):
        cypher, params = compile_pattern(self.pack, detection_pattern())
        self.assertIn("toFloat(r0.`adjusted_p_value`) < $p2", cypher)
        self.assertEqual(params["p2"], 0.05)
        self.assertEqual(params["p0"], "gene-1")

    def test_list_filter_preserves_or_set_and_other_conjunction(self):
        pattern = detection_pattern()
        pattern["edges"][0]["filters"].append(comparison("condition", "in", ["ND", "T1D"]))
        cypher, params = compile_pattern(self.pack, pattern)
        self.assertIn("AND r0.`condition` IN $p3", cypher)
        self.assertEqual(params["p3"], ["ND", "T1D"])
        self.assertEqual(params["p2"], 0.05)

    def test_filter_owner_is_not_interchangeable(self):
        wrong_node = detection_pattern()
        wrong_node["nodes"][0]["filters"].append(comparison("adjusted_p_value", "lt", 0.05))
        with self.assertRaisesRegex(Feedback, "Gene.adjusted_p_value"):
            compile_pattern(self.pack, wrong_node)
        wrong_edge = detection_pattern("id")
        with self.assertRaisesRegex(Feedback, "DETECTED_IN.id"):
            compile_pattern(self.pack, wrong_edge)

    def test_incoming_is_represented_by_swapped_source_target(self):
        pattern = detection_pattern()
        pattern["edges"][0].update(source="c", target="g")
        with self.assertRaisesRegex(Feedback, "connectivity"):
            compile_pattern(self.pack, pattern)
        pattern["edges"][0]["direction"] = "either"
        cypher, _ = compile_pattern(self.pack, pattern)
        self.assertIn("(n1)-[r0:`DETECTED_IN`]-(n0)", cypher)
        self.assertNotIn("->", cypher)

    def test_directed_edges_keep_stored_direction(self):
        cypher, _ = compile_pattern(self.pack, detection_pattern())
        self.assertIn("(n0)-[r0:`DETECTED_IN`]->(n1)", cypher)

    def test_cycle_reuses_declared_aliases(self):
        pattern = {"nodes": [node("a"), node("b"), node("c")],
                   "edges": [edge("ab", "a", "b"), edge("bc", "b", "c"), edge("ca", "c", "a")]}
        cypher, _ = compile_pattern(self.pack, pattern)
        self.assertIn("(n2)-[r2:`INTERACTS`]->(n0)", cypher)
        self.assertIn("RETURN [n0,n1,n2] AS nodes, [r0,r1,r2] AS edges", cypher)

    def test_self_loop_compiles_without_inventing_another_node(self):
        pattern = {"nodes": [node("a")], "edges": [edge("aa", "a", "a")]}
        cypher, _ = compile_pattern(self.pack, pattern)
        self.assertIn("(n0)-[r0:`INTERACTS`]->(n0)", cypher)

    def test_long_paths_and_cycles_have_no_fixed_edge_or_node_limit(self):
        pattern = {"nodes": [node("n" + str(i)) for i in range(6)],
                   "edges": [edge("e" + str(i), "n" + str(i), "n" + str(i + 1)) for i in range(5)]}
        cypher, params = compile_pattern(self.pack, pattern, row_cap=7)
        self.assertIn("r4", cypher)
        self.assertEqual(params["row_limit"], 8, "lookahead must remain available to detect truncation")
        pattern["edges"].append(edge("back", "n5", "n0"))
        self.assertIn('r5', compile_pattern(self.pack, pattern)[0])
        pattern = {"nodes": [node("n" + str(i)) for i in range(12)],
                   "edges": [edge("e" + str(i), "n" + str(i), "n" + str(i + 1)) for i in range(11)]}
        self.assertIn('r10', compile_pattern(self.pack, pattern)[0])

    def test_disconnected_components_do_not_make_cartesian_results(self):
        pattern = {"nodes": [node("a"), node("b"), node("c"), node("d")],
                   "edges": [edge("ab", "a", "b"), edge("cd", "c", "d")]}
        with self.assertRaisesRegex(Feedback, "connected"):
            compile_pattern(self.pack, pattern)

    def test_core_compiles_an_unrelated_schema_pack(self):
        other = self.make_pack(SECOND_PACK, "citation.json")
        pattern = {"nodes": [node("a", "Author", [comparison("display_name", "eq", "Ada")]),
                             node("p", "Paper")],
                   "edges": [edge("w", "a", "p", "WROTE", [comparison("year", "gte", 2020)])]}
        cypher, params = compile_pattern(other, pattern)
        self.assertIn("`Author`", cypher)
        self.assertIn("`WROTE`", cypher)
        self.assertNotIn("Gene", cypher)
        self.assertEqual(params["p0"], "Ada")
        self.assertEqual(params["p1"], 2020)

    def test_distinct_cycle_honors_all_pairwise_node_constraints(self):
        pattern = {"nodes": [node("a"), node("b"), node("c")],
                   "edges": [edge("ab", "a", "b"), edge("bc", "b", "c"), edge("ca", "c", "a")],
                   "distinct_nodes": True}
        cypher, _ = compile_pattern(self.pack, pattern)
        for inequality in ["n0 <> n1", "n0 <> n2", "n1 <> n2"]:
            self.assertIn(inequality, cypher)

    def test_distinct_flag_must_be_boolean(self):
        pattern = {"nodes": [node("a"), node("b")], "edges": [edge("ab", "a", "b")],
                   "distinct_nodes": "false"}
        with self.assertRaisesRegex(Feedback, "bool"):
            compile_pattern(self.pack, pattern)

    def test_unknown_constraint_fields_return_feedback_at_every_layer(self):
        changes = [
            ("pattern", lambda p: p.update(distinct_edges=True)),
            ("node", lambda p: p["nodes"][0].update(exclude_id="gene-2")),
            ("edge", lambda p: p["edges"][0].update(require_evidence="validated")),
            ("filter", lambda p: p["edges"][0]["filters"][0].update(optional=True)),
        ]
        for layer, change in changes:
            with self.subTest(layer=layer):
                pattern = detection_pattern()
                change(pattern)
                with self.assertRaises(Feedback):
                    compile_pattern(self.pack, pattern)


class AgentLoopTests(PackMixin, unittest.IsolatedAsyncioTestCase):
    async def test_unknown_property_feedback_recovery_preserves_threshold_and_entity(self):
        bad = detection_pattern("padj")
        good = detection_pattern()

        def after_invalid(request):
            feedback = latest_feedback(request)[0]
            self.assertEqual(feedback["status"], "tool_error")
            self.assertIn("DETECTED_IN.padj", feedback["feedback"])
            return [tool_block("schema", {"relations": ["DETECTED_IN"]}, "schema-1")]

        def after_schema(request):
            feedback = latest_feedback(request)[0]
            self.assertIn("adjusted_p_value", feedback["relations"]["DETECTED_IN"]["properties"])
            return [tool_block("query", {"pattern": good, "standardized_question": "Show gene-1 detection in cell-1 at adjusted p-value < 0.05."}, "query-good")]

        transport = OfflineTransport([
            [tool_block("query", {"pattern": bad, "standardized_question": "Same strict question."}, "query-bad")],
            after_invalid, after_schema, [finish()],
        ])
        result = await Agent(self.pack, transport).run("Show gene-1 detection in cell-1 at adjusted p-value < 0.05.")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["metapath"], good)
        query_requests = [request for action, request in transport.requests if action == "query"]
        self.assertEqual(len(query_requests), 1, "invalid pattern must not reach execution")
        self.assertEqual(query_requests[0]["parameters"]["p2"], 0.05)
        self.assertEqual(query_requests[0]["parameters"]["p0"], "gene-1")
        self.assertEqual(result["metrics"]["model_calls"], 4)
        self.assertEqual(result["metrics"]["usage"], {"input_tokens": 40, "output_tokens": 12,
                                                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

    async def test_partial_cannot_be_finished_as_complete(self):
        partial = {"status": "partial", "graph": {"nodes": [{"id": "gene-1"}], "edges": []},
                   "truncated": True, "row_cap": 1}

        def after_false_complete(request):
            feedback = latest_feedback(request)[0]
            self.assertEqual(feedback["status"], "tool_error")
            self.assertIn("retrieval_status=partial", feedback["feedback"])
            return [finish("partial")]

        transport = OfflineTransport([
            [tool_block("query", {"pattern": detection_pattern(), "standardized_question": "Strict question."})],
            [finish("complete")], after_false_complete,
        ], partial)
        result = await Agent(self.pack, transport).run("Return detected genes.")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["graph"], partial["graph"])
        self.assertEqual([e["status"] for e in result["events"] if e["turn"] > 0], ["partial", "tool_error", "finished"])

    async def test_graph_completion_requires_an_executed_query(self):
        def after_fabricated_completion(request):
            feedback = latest_feedback(request)[0]
            self.assertIn("query_id from an executed query", feedback["feedback"])
            return [tool_block("finish", {"status": "needs_clarification", "answer": "Choose detection or enrichment.",
                                          "standardized_question": "Show associated genes.", "clarification": "Which evidence type?"}, "clarify")]

        result = await Agent(self.pack, OfflineTransport([[finish()], after_fabricated_completion])).run("Show associated genes.")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["graph"], {"nodes": [], "edges": []})

    async def test_model_can_disclose_semantic_partial_from_complete_retrieval(self):
        final = finish("partial")
        final["input"]["unfulfilled_conditions"] = ["Requested context could not be expressed"]
        transport = OfflineTransport([
            [tool_block("query", {"pattern": detection_pattern(), "standardized_question": "Question."})], [final]])
        result = await Agent(self.pack, transport).run("Question with unavailable context.")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["retrieval_status"], "complete")
        self.assertEqual(result["parameters"]["p2"], 0.05)

    async def test_declared_unfulfilled_condition_cannot_be_complete(self):
        wrong = finish()
        wrong["input"]["unfulfilled_conditions"] = ["Unenforced context"]
        def after_wrong(request):
            self.assertIn("unenforced conditions", latest_feedback(request)[0]["feedback"])
            return [finish("partial")]
        result = await Agent(self.pack, OfflineTransport([
            [tool_block("query", {"pattern": detection_pattern(), "standardized_question": "Question."})],
            [wrong], after_wrong])).run("Question.")
        self.assertEqual(result["status"], "partial")

    async def test_condition_audit_is_required_but_not_semantically_graded(self):
        wrong = finish()
        wrong["input"].pop("condition_audit")
        def after_wrong(request):
            self.assertIn("condition_audit", latest_feedback(request)[0]["feedback"])
            return [finish()]
        result = await Agent(self.pack, OfflineTransport([
            [tool_block("query", {"pattern": detection_pattern(), "standardized_question": "Question."})],
            [wrong], after_wrong])).run("Question.")
        self.assertEqual(result["status"], "complete")

    async def test_execution_failure_is_feedback_and_cannot_be_labeled_empty(self):
        def after_timeout(request):
            feedback = latest_feedback(request)[0]
            self.assertEqual(feedback["category"], "TimeoutError")
            self.assertIn("not empty", feedback["hint"])
            return [finish("empty")]

        def after_false_empty(request):
            self.assertEqual(latest_feedback(request)[0]["status"], "tool_error")
            return [tool_block("finish", {"status": "failed", "standardized_question": "Original strict question.",
                                          "answer": "Execution timed out; graph absence was not established."}, "failed")]

        transport = OfflineTransport([
            [tool_block("query", {"pattern": detection_pattern(), "standardized_question": "Original strict question."})],
            after_timeout, after_false_empty,
        ], TimeoutError("transaction timed out"))
        result = await Agent(self.pack, transport).run("Original strict question.")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["graph"], {"nodes": [], "edges": []})

    async def test_legacy_turn_limit_does_not_end_model_reasoning(self):
        transport = OfflineTransport([
            [{"type": "text", "text": "I will think about it."}],
            [tool_block("finish", {"status": "unsupported",
                "standardized_question": "Return nodes and edges.",
                "answer": "The requested evidence is not represented."})],
        ])
        result = await Agent(self.pack, transport, max_turns=1).run("Return nodes and edges.")
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["metrics"]["model_calls"], 2)
        self.assertEqual(result["graph"], {"nodes": [], "edges": []})

    async def test_identity_fields_and_relative_schema_come_from_pack(self):
        def after_resolution(request):
            feedback = latest_feedback(request)[0]
            self.assertNotIn("schema", feedback)
            bootstrap = json.loads(request["messages"][2]["content"][0]["content"])
            self.assertIn("DETECTED_IN", bootstrap["relationship_types"])
            return [tool_block("finish", {"status": "needs_clarification", "standardized_question": "Resolve gene-1.",
                                          "answer": "Choose evidence type."}, "finish")]

        transport = OfflineTransport([
            [tool_block("resolve_entity", {"text": "INS", "label": "Gene"}, "resolve")], after_resolution,
        ])
        await Agent(self.pack, transport).run("Resolve INS.")
        resolve_request = next(req for action, req in transport.requests if action == "resolve")
        self.assertIn("search_roles", resolve_request)


if __name__ == "__main__":
    unittest.main()
