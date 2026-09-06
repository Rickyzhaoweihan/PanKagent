"""Offline safety and behavior tests; no shared API calls or credentials."""
import asyncio
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from neo4j.graph import Graph, Node

from pankagent_vnext.graph import GraphAdapter, schema_fingerprint, suppress_driver_query_logging, validate_cypher


def step(**changes):
    return {"id": "s1", "question": "Which genes are T1D effectors?", "depends_on": [],
            "constraints": [{"property": "name", "operator": "=", "value": "T1D"}],
            "complete": True, **changes}


VALID = "MATCH (g:gene)-[r:effector_gene_of]->(d:disease) WHERE d.name = 'T1D' RETURN g,r,d"


class GuardTests(unittest.TestCase):
    def test_literal_and_comments_are_not_clauses(self):
        query = "MATCH (d:disease {name:'T1D'}) // DELETE x\n RETURN d, 'CREATE; DELETE', `SET` /* CALL unsafe */;"
        self.assertEqual(validate_cypher(query, step()), [])

    def test_strings_cannot_spoof_required_filter(self):
        query = 'MATCH (n) RETURN "WHERE d.name = \'T1D\'", n'
        self.assertIn("missing_required_filter:name", validate_cypher(query, step()))

    def test_write_and_external_procedures_rejected(self):
        for query in ["MATCH (n) DELETE n RETURN n", "CALL apoc.load.json('https://x') YIELD value RETURN value",
                      "MATCH (n) SET n.x=1 RETURN n", "RETURN 1; MATCH (n) RETURN n", "SHOW USERS",
                      "MATCH (n) FOREACH (x IN [1] | DELETE n) RETURN n"]:
            with self.subTest(query=query):
                self.assertTrue(validate_cypher(query, step(constraints=[])))

    def test_unclosed_string_and_comment_rejected(self):
        for query in ["RETURN 'test", "RETURN 1 /*never closes", "MATCH (`bad) RETURN 1"]:
            self.assertTrue(validate_cypher(query, step(constraints=[])))

    def test_unicode_escapes_cannot_hide_code_or_identifier_boundaries(self):
        for query in [r"MATCH (n:`x\u0060`) RETURN n", r"MATCH (n) CR\u0045ATE (m) RETURN n"]:
            self.assertTrue(validate_cypher(query, step(constraints=[])))

    def test_constraint_in_return_is_not_a_filter(self):
        self.assertIn("missing_required_filter:name", validate_cypher("MATCH (d) RETURN d.name = 'T1D'", step()))

    def test_optional_predicate_does_not_restrict_mandatory_nodes(self):
        query = "MATCH (g:gene) OPTIONAL MATCH (g)-[r]->(d:disease) WHERE d.name='T1D' RETURN g,r,d"
        self.assertIn("missing_required_filter:name", validate_cypher(query, step()))

    def test_external_function_cannot_access_network_or_execute_cypher(self):
        query = "RETURN apoc.cypher.runFirstColumn('CREATE (n) RETURN n', {}, true)"
        self.assertIn("external_function_not_allowed", validate_cypher(query, step(constraints=[])))

    def test_property_map_and_normalized_filter(self):
        self.assertFalse(validate_cypher("MATCH (d:disease {name:'T1D'}) RETURN d", step()))
        self.assertFalse(validate_cypher("MATCH (d:disease) WHERE toLower(d.name) = 't1d' RETURN d", step()))

    def test_wrong_case_without_normalization_not_accepted(self):
        self.assertTrue(validate_cypher("MATCH (d:disease {name:'t1d'}) RETURN d", step()))

    def test_missing_disease_filter(self):
        self.assertIn("missing_required_filter:name", validate_cypher("MATCH (g)-[r:effector_gene_of]->(d) RETURN g,r,d", step()))

    def test_or_not_and_union_cannot_weaken_required_filter(self):
        for query in [VALID.replace(" RETURN", " OR true RETURN"),
                      VALID.replace("WHERE", "WHERE NOT"), VALID + " UNION MATCH (g) RETURN g,null,null"]:
            self.assertTrue(validate_cypher(query, step()))

    def test_complete_query_rejects_all_slice_forms_and_limits(self):
        for query in [VALID + " LIMIT 10", VALID + " SKIP 1", "MATCH (g) RETURN collect(g)[..10]",
                      "MATCH (g) RETURN collect(g)[0..10]", "MATCH (g) RETURN collect(g)[5..]",
                      "MATCH (g) RETURN g ORDER BY rand()"]:
            self.assertIn("incomplete_limit_or_slice", validate_cypher(query, step(constraints=[])))
        self.assertFalse(validate_cypher("MATCH p=(n)-[*1..2]-(m) RETURN p", step(constraints=[])))
        self.assertFalse(validate_cypher(VALID + " LIMIT 10", step(complete=False)))

    def test_parameterized_and_literal_dependencies(self):
        params = {"dep_0": ["a", "b"]}
        for predicate in ["n.id IN $dep_0", "n.id IN ['b','a']"]:
            self.assertFalse(validate_cypher("MATCH (n) WHERE " + predicate + " RETURN n", step(constraints=[]), params))
        for predicate in ["n.id IN ['a']", "n.id IN ['a','b','c']", "n.id IN $wrong"]:
            self.assertTrue(validate_cypher("MATCH (n) WHERE " + predicate + " RETURN n", step(constraints=[]), params))
        self.assertTrue(validate_cypher("MATCH (n) RETURN n,$dep_0", step(constraints=[]), params))

    def test_numeric_and_in_constraints(self):
        spec = step(constraints=[{"property": "p", "operator": "<", "value": 0.05},
                                 {"property": "id", "operator": "IN", "value": ["a", "b"]}])
        self.assertFalse(validate_cypher("MATCH (n) WHERE n.p < 0.05 AND n.id IN ['b','a'] RETURN n", spec))

    def test_structured_planner_numeric_and_list_strings(self):
        spec = step(constraints=[{"property": "p", "operator": "<", "value": "0.05"},
                                 {"property": "id", "operator": "IN", "value": '["a", "b"]'}])
        self.assertFalse(validate_cypher("MATCH (n) WHERE n.p < 0.05 AND n.id IN ['b','a'] RETURN n", spec))

    def test_invented_gene_filter_cannot_narrow_complete_disease_answer(self):
        query = VALID.replace("WHERE", "WHERE g.name='CD80' AND")
        self.assertIn("unrequested_identity_filter:name", validate_cypher(query, step()))

    def test_entity_set_can_bind_separate_interaction_endpoints(self):
        spec = step(constraints=[{"property": "name", "operator": "IN", "value": '["GENE_A", "GENE_B"]'}])
        query = "MATCH (a:Gene)-[r:PHYSICAL_INTERACTION]-(b:Gene) WHERE a.name='GENE_A' AND b.name='GENE_B' RETURN a,r,b"
        self.assertEqual(validate_cypher(query, spec), [])
        for invalid in [query.replace("b.name", "a.name"), query.replace("GENE_B", "UNREQUESTED")]:
            self.assertIn("missing_required_filter:name", validate_cypher(invalid, spec))

    def test_distributed_constraint_must_appear_in_every_union_arm(self):
        spec = step(constraints=[{"property": "name", "operator": "IN", "value": '["A", "B"]'}])
        query = "MATCH (a)--(b) WHERE a.name='A' AND b.name='B' RETURN a,b UNION MATCH (a)--(b) WHERE a.name='A' RETURN a,b"
        self.assertIn("missing_required_filter:name", validate_cypher(query, spec))

    def test_driver_automatic_query_logging_is_disabled(self):
        logs = [logging.getLogger(name) for name in logging.Logger.manager.loggerDict if name == "neo4j" or name.startswith("neo4j.")]
        saved = [(log, log.level, log.propagate, log.disabled) for log in logs]
        try:
            suppress_driver_query_logging()
            self.assertFalse(logging.getLogger("neo4j.notifications").isEnabledFor(logging.WARNING))
            self.assertFalse(logging.getLogger("neo4j.io").isEnabledFor(logging.DEBUG))
        finally:
            for log, level, propagate, disabled in saved:
                log.setLevel(level)
                log.propagate = propagate
                log.disabled = disabled

    def test_schema_fingerprint_is_order_independent(self):
        self.assertEqual(schema_fingerprint(["gene", "disease"], ["a", "b"]),
                         schema_fingerprint(["disease", "gene", "gene"], ["b", "a"]))


class FakeAdapter(GraphAdapter):
    def __init__(self, batches):
        self.settings = SimpleNamespace(graph_version="rl-test", graph_timeout=1)
        self.identity_verified = True
        self.identity_check_time = time.monotonic()
        self.batches = list(batches)
        self.generated, self.retrieved, self.explained = [], [], []
        self.answer = {"nodes": [{"id": "a", "labels": ["gene"], "properties": {"data_source": "paper"}}],
                       "edges": [], "rows": [], "status": "complete", "truncated": False}
        self.explain_errors = []

    async def _generate(self, question, n):
        self.generated.append((question, n))
        value = self.batches.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def _explain(self, query, parameters):
        self.explained.append((query, parameters))
        return self.explain_errors

    async def _retrieve(self, query, parameters, limits=None):
        self.retrieved.append((query, parameters))
        return self.answer.copy()


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []

    async def emit(self, event_type, payload):
        self.events.append((event_type, payload))

    async def test_escalates_once_then_preserves_complete_result(self):
        adapter = FakeAdapter([[VALID + " LIMIT 10"], [VALID]])
        result = await adapter.execute(step(), {}, self.emit)
        self.assertEqual([n for _, n in adapter.generated], [1, 8])
        self.assertIn("Correct the previous validation failures", adapter.generated[1][0])
        self.assertEqual(len(adapter.retrieved), 1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["provenance"], [{"property": "data_source", "value": "paper"}])
        self.assertEqual([payload["stage"] for _, payload in self.events][-1], "querying_graph")

    async def test_confirmed_plan_recovers_requested_cell_without_weakening_guards(self):
        original = step(question="Is CFTR (gene) specifically enriched in ductal cells (GENE_ENRICHED_IN relation)?",
                        constraints=[{"property": "name", "operator": "=", "value": "CFTR"}])
        broad = "MATCH (g:Gene)-[r:GENE_ENRICHED_IN]->(c:anatomical_structure) WHERE g.name='CFTR' RETURN g,r,c"
        invented = broad.replace(" RETURN", " AND c.name='beta cell' RETURN")
        requested = broad.replace(" RETURN", " AND c.name='ductal cell' RETURN")
        adapter = FakeAdapter([[broad], [invented, requested]])
        result = await adapter.execute(original, {}, self.emit)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(adapter.retrieved, [(requested, {})])
        self.assertEqual([n for _, n in adapter.generated], [1, 8])
        self.assertIn("missing_required_filter:name", result["validation"][0]["reasons"])
        self.assertIn("unrequested_identity_filter:name", result["validation"][1]["reasons"])
        self.assertIn("Gene nodes named CFTR", adapter.generated[0][0])
        self.assertIn("anatomical_structure nodes named ductal cell", adapter.generated[0][0])
        self.assertEqual(result["question"], original["question"])
        self.assertEqual(original["constraints"], [{"property": "name", "operator": "=", "value": "CFTR"}])

    async def test_never_executes_after_two_invalid_batches(self):
        adapter = FakeAdapter([[VALID + " LIMIT 10"], [VALID + " LIMIT 20"]])
        result = await adapter.execute(step(), {}, self.emit)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(adapter.retrieved, [])
        self.assertEqual(len(adapter.generated), 2)

    async def test_candidate_filtering_ignores_nonempty_size_ranking(self):
        adapter = FakeAdapter([["MATCH (n) RETURN n", VALID]])
        result = await adapter.execute(step(), {}, self.emit)
        self.assertEqual(result["queries"][0]["cypher"], VALID)
        self.assertEqual(len(adapter.generated), 1)

    async def test_schema_warning_rejects_candidate(self):
        adapter = FakeAdapter([[VALID], [VALID]])
        adapter.explain_errors = ["schema_or_plan_warning:UnknownPropertyKeyWarning"]
        result = await adapter.execute(step(), {}, self.emit)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(adapter.retrieved)

    async def test_empty_dependency_never_becomes_unfiltered_query(self):
        adapter = FakeAdapter([])
        result = await adapter.execute(step(depends_on=["prior"]), {"prior": {"status": "empty", "nodes": []}}, self.emit)
        self.assertEqual(result["status"], "empty")
        self.assertFalse(adapter.generated)

    async def test_failed_dependency_stops(self):
        adapter = FakeAdapter([])
        result = await adapter.execute(step(depends_on=["prior"]), {"prior": {"status": "failed"}}, self.emit)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(adapter.generated)

    async def test_dependency_ids_are_bound_and_partial_propagates(self):
        query = "MATCH (n) WHERE n.id IN $dep_0 RETURN n"
        adapter = FakeAdapter([[query]])
        previous = {"prior": {"status": "partial", "nodes": [{"id": "b"}, {"id": "a"}]}}
        result = await adapter.execute(step(depends_on=["prior"], constraints=[]), previous, self.emit)
        self.assertEqual(adapter.retrieved[0][1], {"dep_0": ["a", "b"]})
        self.assertEqual(result["status"], "partial")

    async def test_valid_empty_result_is_not_relaxed_or_retried(self):
        adapter = FakeAdapter([[VALID]])
        adapter.answer.update(nodes=[], status="empty")
        result = await adapter.execute(step(), {}, self.emit)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(len(adapter.generated), 1)

    async def test_upstream_failure_not_retried_as_validation_failure(self):
        adapter = FakeAdapter([TimeoutError()])
        result = await adapter.execute(step(), {}, self.emit)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(adapter.generated), 1)

    async def test_cancellation_propagates(self):
        adapter = FakeAdapter([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await adapter.execute(step(), {}, self.emit)

    async def test_stale_or_missing_identity_blocks_generation(self):
        adapter = FakeAdapter([])
        adapter.identity_verified = False
        async def probe():
            return {"state": "unavailable", "error_category": "graph_anchor_identity_mismatch"}
        adapter.probe = probe
        result = await adapter.execute(step(), {}, self.emit)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(adapter.generated)


class FakeResult:
    def __init__(self, rows):
        self.rows = iter(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.rows)
        except StopIteration:
            raise StopAsyncIteration


class FakeTransaction:
    def __init__(self, rows):
        self.rows = rows
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def run(self, *args):
        return FakeResult(self.rows)

    async def rollback(self):
        self.rolled_back = True


class FakeSession:
    def __init__(self, tx):
        self.tx = tx

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def begin_transaction(self, **kwargs):
        return self.tx


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_node_limit_marks_truncated_and_rolls_back(self):
        graph = Graph()
        nodes = [Node(graph, "n" + str(i), i, ["gene"], {"id": str(i)}) for i in range(3)]
        tx = FakeTransaction([{"n": node} for node in nodes])
        adapter = object.__new__(GraphAdapter)
        adapter.settings = SimpleNamespace(graph_timeout=1, max_nodes=2, max_edges=5, max_bytes=10000)
        adapter._session = lambda: FakeSession(tx)
        result = await adapter._retrieve("MATCH (n) RETURN n", {})
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["truncated"])
        self.assertTrue(tx.rolled_back)
        self.assertEqual(len(result["nodes"]), 2)
        self.assertEqual(len(result["rows"]), 2)

    async def test_caps_apply_across_steps_and_reused_nodes_do_not_count_twice(self):
        graph = Graph()
        nodes = [Node(graph, "n" + str(i), i, ["gene"], {"id": str(i)}) for i in range(3)]
        tx = FakeTransaction([{"n": node} for node in nodes])
        adapter = object.__new__(GraphAdapter)
        adapter.settings = SimpleNamespace(graph_timeout=1, max_nodes=2, max_edges=5, max_bytes=10000)
        adapter._session = lambda: FakeSession(tx)
        result = await adapter._retrieve("MATCH (n) RETURN n", {}, {"known_node_ids": {"0"}, "used_bytes": 100})
        self.assertEqual(result["status"], "partial")
        self.assertEqual([n["id"] for n in result["nodes"]], ["0", "1"])

    async def test_scalar_rows_are_preserved_and_byte_limited(self):
        tx = FakeTransaction([{"count": 42}, {"value": "x" * 1000}])
        adapter = object.__new__(GraphAdapter)
        adapter.settings = SimpleNamespace(graph_timeout=1, max_nodes=2, max_edges=5, max_bytes=100)
        adapter._session = lambda: FakeSession(tx)
        result = await adapter._retrieve("RETURN 42 AS count", {})
        self.assertEqual(result["rows"], [{"count": 42}])
        self.assertEqual(result["status"], "partial")
        self.assertLessEqual(result["materialized_bytes"], 100)

    async def test_identity_requires_matching_manifest_and_anchor(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "identity.json"
            manifest = {"graph_version": "release", "neo4j_uri": "bolt://127.0.0.1:12687", "database": "neo4j",
                        "schema_sha256": schema_fingerprint(["gene"], ["relation"]),
                        "anchors": [{"label": "gene", "property": "id", "value": "stable", "count": 1}]}
            file.write_text(json.dumps(manifest))
            adapter = object.__new__(GraphAdapter)
            adapter.settings = SimpleNamespace(graph_identity_file=str(file), graph_version="release",
                                               neo4j_uri=manifest["neo4j_uri"], neo4j_database="neo4j")
            async def small(query, params=None):
                if "db.labels" in query:
                    return [{"label": "gene"}]
                if "db.relationshipTypes" in query:
                    return [{"relationshipType": "relation"}]
                return [{"count": 1}]
            adapter._small_query = small
            await adapter._verify_identity()
            self.assertTrue(adapter.identity_verified)
            self.assertFalse(adapter.identity_details["database_role_enforced"])
            manifest["graph_version"] = "different-release"
            file.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "graph_identity_mismatch"):
                await adapter._verify_identity()


class CypherHealthTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, data, health_status=200, info_status=200):
        calls = []

        def response(request):
            calls.append((request.method, request.url.path))
            if request.url.path == "/health":
                return httpx.Response(health_status, json=data)
            return httpx.Response(info_status, json={"model": "local-model", "prompt_version": "v0"})

        adapter = object.__new__(GraphAdapter)
        adapter.settings = SimpleNamespace(cypher_url="https://example.test", cypher_token="test-token")
        adapter.last_generation_success = None
        adapter.last_generation_error = None
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            adapter.http = client
            result = await adapter.probe_cypher()
        self.assertCountEqual(calls, [("GET", "/health"), ("GET", "/v1/info")])
        return result

    async def test_fractional_replica_counts_report_one_and_zero_failures(self):
        for count, state, available in [("2/2", "healthy", 2), ("1/2", "degraded", 1), ("0/2", "unavailable", 0)]:
            with self.subTest(count=count):
                result = await self.probe({"status": "ok", "backends_up": count})
                self.assertEqual(result["state"], state)
                self.assertEqual(result["healthy_replicas"], available)
                self.assertEqual(result["total_replicas"], 2)

    async def test_unavailable_http_responses_keep_reported_replica_counts(self):
        result = await self.probe({"status": "down", "backends_up": "0/2"}, health_status=503, info_status=503)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["healthy_replicas"], 0)
        self.assertEqual(result["total_replicas"], 2)
        self.assertFalse(result["authenticated"])

    async def test_integer_available_and_explicit_total(self):
        for available, total, state in [(3, 3, "healthy"), (2, 3, "degraded"), (0, 3, "unavailable")]:
            result = await self.probe({"status": "healthy", "backends_up": available, "backends_total": total})
            self.assertEqual(result["state"], state)
            self.assertEqual(result["healthy_replicas"], available)
            self.assertEqual(result["total_replicas"], total)

    async def test_integer_without_total_does_not_invent_replica_capacity(self):
        for value in [2, "2"]:
            result = await self.probe({"status": "ok", "backends_up": value})
            self.assertEqual(result["state"], "degraded")
            self.assertEqual(result["healthy_replicas"], 2)
            self.assertIsNone(result["total_replicas"])

    async def test_missing_and_malformed_counts_never_report_healthy(self):
        for count in [None, True, -1, 1025, "bad", "3/2", "1/0", "0/0", "1/99999", [2], {"up": 2}]:
            with self.subTest(count=count):
                result = await self.probe({"status": "ok", "backends_up": count})
                self.assertEqual(result["state"], "degraded")
                self.assertIsNone(result["healthy_replicas"])
                self.assertEqual(result["error_category"], "invalid_response")

    async def test_reported_degraded_status_is_not_overridden_by_counts(self):
        result = await self.probe({"status": "degraded", "backends_up": "2/2"})
        self.assertEqual(result["state"], "degraded")

    async def test_authentication_failure_is_separate_from_live_replicas(self):
        result = await self.probe({"status": "ok", "backends_up": "2/2"}, info_status=401)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["healthy_replicas"], 2)
        self.assertEqual(result["total_replicas"], 2)
        self.assertEqual(result["error_category"], "authentication")


if __name__ == "__main__":
    unittest.main()
