import asyncio
import hashlib
import json
import time
import unittest
from pathlib import Path

from pankgraph_results.layout import LayoutService, LAYOUT_VERSION


def fixture(count=25, dense=False):
    nodes = [{"id": f"n{i:03}", "labels": ["Gene" if i == 0 else "anatomical_structure"],
              "properties": {"name": f"Node{i}"}} for i in range(count)]
    edges = []
    for index in range(1, count):
        edges.append({"start_id": f"n{(index-1)//2:03}", "end_id": f"n{index:03}", "type": "GENE_DETECTED_IN", "properties": {}})
    if dense:
        for index in range(count):
            for offset in (2, 5, 9):
                other = (index + offset) % count
                edges.append({"start_id": f"n{index:03}", "end_id": f"n{other:03}", "type": "INTERACTS_WITH", "properties": {"offset": offset}})
    return {"nodes": nodes, "edges": edges, "graph_version": "test-release", "completeness": "complete"}


def overlaps(xy):
    values = list(xy.values())
    for index, left in enumerate(values):
        for right in values[index + 1:]:
            if abs(left["x"]-right["x"]) < (left["width"]+right["width"])/2-0.01 and abs(left["y"]-right["y"]) < (left["height"]+right["height"])/2-0.01:
                return True
    return False


class LayoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = LayoutService()

    async def asyncTearDown(self):
        await self.service.close()

    async def test_real_worker_regular_layout_and_routes(self):
        source = fixture(10)
        result = await self.service.layout(source, ["n000"])
        self.assertEqual(result["layout"]["status"], "optimized")
        self.assertEqual(result["layout"]["mode"], "kg_only")
        self.assertEqual(result["core_nodes"], ["n000"])
        self.assertEqual(len(result["edge_routes"]), 9)
        self.assertFalse(overlaps(result["xy_json"]))
        self.assertTrue(all(point["height"] == 16 for point in result["xy_json"].values()))
        self.assertTrue(all({"x", "y", "start_xy", "end_xy", "width", "height", "Level"} <= point.keys() for point in result["xy_json"].values()))
        self.assertEqual(set(result["edge_routes"]), {edge["~id"] for edge in result["combined_query_result"]["edges"]})

    async def test_cache_and_deterministic_fresh_results(self):
        source = fixture(10)
        first = await self.service.layout(source, ["n000"])
        second = await self.service.layout(source, ["n000"])
        self.assertTrue(second["layout"]["cache_hit"])
        self.assertEqual(first["xy_json"], second["xy_json"])
        self.assertEqual(first["edge_routes"], second["edge_routes"])
        independent = LayoutService()
        try:
            fresh = await independent.layout(source, ["n000"])
        finally:
            await independent.close()
        self.assertEqual(first["xy_json"], fresh["xy_json"])
        self.assertEqual(first["edge_routes"], fresh["edge_routes"])
        source["graph_version"] = "different"
        third = await self.service.layout(source, ["n000"])
        self.assertFalse(third["layout"]["cache_hit"])

    async def test_display_cap_preserves_focus_rare_type_and_evidence_completeness(self):
        source = fixture(140)
        source["nodes"][-1]["labels"] = ["RareBiologicalType"]
        service = LayoutService(max_nodes=25, timeout_seconds=0.001)
        try:
            result = await service.layout(source, ["n000"])
        finally:
            await service.close()
        self.assertEqual(len(result["combined_query_result"]["nodes"]), 25)
        self.assertIn("n139", result["xy_json"])
        self.assertIn("n000", result["xy_json"])
        self.assertEqual(result["full_evidence"]["node_count"], 140)
        self.assertEqual(result["display"]["hidden_node_count"], 115)
        self.assertEqual(result["display"]["scientific_completeness"], "complete")
        self.assertFalse(result["display"]["display_complete"])
        self.assertEqual(len(source["nodes"]), 140)

    async def test_real_deadline_kills_worker_and_keeps_health_responsive(self):
        service = LayoutService(timeout_seconds=0.001)
        started = time.monotonic()
        try:
            result = await service.layout(fixture(100, True), ["n000"])
            self.assertEqual(result["layout"]["status"], "fallback")
            self.assertEqual(result["layout"]["fallback_reason"], "deadline_exceeded")
            self.assertEqual(service.snapshot()["worker_active"], 0)
            self.assertLess(time.monotonic()-started, 2)
            self.assertFalse(overlaps(result["xy_json"]))
            self.assertEqual(len(result["xy_json"]), 100)
        finally:
            await service.close()

    async def test_cancellation_terminates_active_process(self):
        task = asyncio.create_task(self.service.layout(fixture(100, True), ["n000"]))
        for _ in range(100):
            if self.service.snapshot()["worker_active"]:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(self.service.snapshot()["worker_active"], 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.service.snapshot()["worker_active"], 0)

    async def test_large_graph_keeps_optimized_positions_and_partial_routes(self):
        started = time.monotonic()
        result = await self.service.layout(fixture(100), ["n000"])
        self.assertIn(result["layout"]["status"], {"optimized", "partial"})
        self.assertEqual(result["layout"]["engine"], "optimized_v1")
        self.assertEqual(len(result["xy_json"]), 100)
        self.assertEqual(len(result["edge_routes"]), 99)
        self.assertFalse(overlaps(result["xy_json"]))
        self.assertLess(time.monotonic()-started, 4)
        self.assertGreater(result["layout"]["metrics"]["optimized_route_count"], 0)
        self.assertEqual(result["layout"]["metrics"]["optimized_route_count"] + result["layout"]["metrics"]["fallback_route_count"], 99)

    async def test_parent_deadline_retains_the_last_complete_checkpoint(self):
        service = LayoutService(timeout_seconds=1)
        try:
            result = await service.layout(fixture(100), ["n000"])
            self.assertEqual(result["layout"]["status"], "partial")
            self.assertEqual(result["layout"]["engine"], "optimized_v1")
            self.assertEqual(result["layout"]["fallback_reason"], "deadline_exceeded")
            self.assertEqual(len(result["xy_json"]), 100)
            self.assertEqual(len(result["edge_routes"]), 99)
            self.assertEqual(service.snapshot()["worker_active"], 0)
        finally:
            await service.close()

    async def test_previous_layout_keeps_positions_and_rejects_changed_release(self):
        first = await self.service.layout(fixture(5), ["n000"])
        second = await self.service.layout(fixture(6), ["n000"], previous_layout=first)
        self.assertEqual(second["layout"]["previous_layout_status"], "accepted")
        if second["layout"]["status"] == "optimized":
            for nid, point in first["xy_json"].items():
                self.assertAlmostEqual(point["x"], second["xy_json"][nid]["x"], places=2)
                self.assertAlmostEqual(point["y"], second["xy_json"][nid]["y"], places=2)
        changed = fixture(6)
        changed["graph_version"] = "new-release"
        third = await self.service.layout(changed, ["n000"], previous_layout=first)
        self.assertEqual(third["layout"]["previous_layout_status"], "graph_version_mismatch")

    async def test_empty_partial_and_busy_status_are_truthful(self):
        source = {"nodes": [], "edges": [], "graph_version": "v1", "completeness": "partial"}
        empty = await self.service.layout(source)
        self.assertEqual(empty["layout"]["status"], "empty")
        self.assertEqual(empty["full_evidence"]["scientific_completeness"], "partial")
        self.service._busy = True
        result = await self.service.layout(fixture(5), ["n000"])
        self.service._busy = False
        self.assertEqual(result["layout"]["fallback_reason"], "worker_busy")
        self.assertEqual(self.service.snapshot()["state"], "degraded")

    async def test_edge_budget_and_self_loops_are_explicit_display_omissions(self):
        source = fixture(100, True)
        source["edges"] += [{"start_id": "n000", "end_id": "n001", "type": "MANY", "properties": {"index": index}} for index in range(40)]
        source["edges"].append({"start_id": "n000", "end_id": "n000", "type": "SELF", "properties": {}})
        self.service._busy = True
        result = await self.service.layout(source, ["n000"])
        self.service._busy = False
        self.assertEqual(len(result["combined_query_result"]["edges"]), 400)
        self.assertEqual(result["display"]["hidden_edge_count"], 40)
        self.assertEqual(result["display"]["self_loop_count"], 1)
        self.assertEqual(result["full_evidence"]["edge_count"], 440)


class VendorTests(unittest.TestCase):
    def test_pinned_modules_match_provenance_and_never_import_query_handler(self):
        root = Path(__file__).resolve().parents[1]/"pankgraph_results/vendor/graph_viewer"
        manifest = json.loads((root/"PROVENANCE.json").read_text())
        self.assertEqual(manifest["commit"], "362025db24b1d37223c3c44ccf02a55eb2756a42")
        for path, info in manifest["files"].items():
            source = (root/path).read_bytes()
            self.assertEqual(hashlib.sha256(source).hexdigest(), info["sha256"])
            self.assertNotIn(b"import neo4j", source)
            self.assertNotIn(b"import psycopg", source)
            self.assertNotIn(b"from app.index", source)


if __name__ == "__main__":
    unittest.main()
