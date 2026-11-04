"""Offline proposed v28 regression tests; v27 frozen runtime is not modified.

Run against a patched candidate by setting PANK_REVIEW_SOURCE to its source dir.
Unpatched v27 is expected to fail because replace is not declared/implemented.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

DEFAULT_SOURCE = Path(__file__).resolve().parent / "source"
sys.path.insert(0, os.environ.get("PANK_REVIEW_SOURCE", str(DEFAULT_SOURCE)))
from simple_agent.core import Agent, SchemaPack

QUESTION = "Find author A1."

def action(name, value):
    return {"type": "tool_use", "id": name, "name": name, "input": value}

def query(identity):
    return action("query", {"pattern": {"nodes": [{"key": "a", "label": "Author",
        "filters": [{"property": "id", "op": "eq", "value": identity}]}], "edges": []},
        "standardized_question": QUESTION})

def choose(qid=None, *, replace=None, purpose="requested"):
    args = {"selections": [{"query_id": qid, "purpose": purpose}] if qid else []}
    if replace is not None:
        args["replace"] = replace
    return action("select_results", args)

def finish(qid=None):
    return action("finish", {"status": "complete", "standardized_question": QUESTION,
        "answer": "Only the selected author records are requested evidence.",
        **({"query_ids": [qid]} if qid else {})})

def output(request):
    return json.loads(request["messages"][-1]["content"][0]["content"])

class Scripted:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []
    async def call(self, kind, request):
        self.requests.append((kind, copy.deepcopy(request)))
        if kind == "model":
            if not self.steps:
                raise AssertionError("Unexpected model call after recovery script")
            item = self.steps.pop(0)
            return {"content": item(request) if callable(item) else item}
        if kind != "query":
            raise AssertionError("No external tools authorized in this test")
        identity = request["parameters"]["p0"]
        return {"status": "complete", "row_count": 1, "truncated": False,
            "graph": {"nodes": [{"id": identity, "labels": ["Author"],
                "properties": {"id": identity}}], "edges": []}}

class ExplicitSelectionReplacementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "pack.json"
        path.write_text(json.dumps({"release": "offline", "nodes": {"Author": ["id"]},
                                   "relations": {}, "linked_evidence": {}}))
        self.pack = SchemaPack(path)
    def tearDown(self):
        self.temp.cleanup()

    async def prepare(self):
        return await Agent(self.pack, Scripted([[query("A1")], [finish("q1")]])).run(QUESTION)

    async def test_model_explicitly_retracts_mistaken_preconfirm_selection(self):
        transport = Scripted([[query("A2")], [choose("q1")], [query("A1")],
                              [choose("q2", replace=True)], [finish()]])
        result = await Agent(self.pack, transport).run(QUESTION)
        self.assertEqual(result["selected_queries"], [{"query_id": "q2", "purpose": "requested"}])
        self.assertEqual([n["id"] for n in result["graph"]["nodes"]], ["A1"])
        self.assertEqual(set(result["_query_results"]), {"q1", "q2"})
        self.assertEqual(result["_query_results"]["q1"]["graph"]["nodes"][0]["id"], "A2")
        self.assertIn('"A2"', json.dumps(result["_messages"]))
        self.assertEqual(result["metrics"]["model_calls"], 5)

    async def test_replace_empty_clears_unconfirmed_selection_not_query_history(self):
        def cleared(request):
            self.assertEqual(output(request)["selected_queries"], [])
            return [finish("q1")]
        result = await Agent(self.pack, Scripted([[query("A1")], [choose("q1")],
            [choose(replace=True)], cleared])).run(QUESTION)
        self.assertEqual(result["selected_queries"], [{"query_id": "q1", "purpose": "requested"}])
        self.assertEqual(set(result["_query_results"]), {"q1"})

    async def test_enrichment_replace_keeps_confirmed_seed_and_removes_new_mistaken_context(self):
        seed = await self.prepare()
        before = copy.deepcopy(seed)
        def replaced(request):
            self.assertEqual(output(request)["selected_queries"], [
                {"query_id": "q1", "purpose": "requested"}, {"query_id": "q3", "purpose": "context"}])
            return [finish()]
        result = await Agent(self.pack, Scripted([[query("wrong-context")],
            [choose("q2", purpose="context")], [query("useful-context")],
            [choose("q3", replace=True, purpose="context")], replaced])).run(
                QUESTION, phase="enrich", seed=seed)
        self.assertEqual({n["id"] for n in result["graph"]["nodes"]}, {"A1", "useful-context"})
        self.assertEqual(result["_messages"][:len(before["_messages"])], before["_messages"])
        self.assertEqual(result["_query_results"]["q1"]["pattern"], before["_query_results"]["q1"]["pattern"])
        self.assertEqual(set(result["_query_results"]), {"q1", "q2", "q3"})
        self.assertEqual(seed, before)

    async def test_invalid_replacement_does_not_clear_prior_selection(self):
        def feedback_then_finish(request):
            self.assertEqual(output(request)["status"], "tool_error")
            self.assertIn("Unknown query_id", output(request)["feedback"])
            return [finish()]
        result = await Agent(self.pack, Scripted([[query("A1")], [choose("q1")],
            [choose("not-executed", replace=True)], feedback_then_finish])).run(QUESTION)
        self.assertEqual(result["selected_queries"], [{"query_id": "q1", "purpose": "requested"}])
        self.assertEqual(result["error_records"][0]["input"]["replace"], True)

    async def test_replace_type_error_is_same_loop_feedback_without_conversion(self):
        def feedback_then_finish(request):
            self.assertIn("must be boolean", output(request)["feedback"])
            return [finish()]
        result = await Agent(self.pack, Scripted([[query("A1")], [choose("q1")],
            [choose(replace="true")], feedback_then_finish])).run(QUESTION)
        self.assertEqual(result["selected_queries"], [{"query_id": "q1", "purpose": "requested"}])
        self.assertEqual(result["error_records"][0]["input"]["replace"], "true")

if __name__ == "__main__":
    unittest.main()
