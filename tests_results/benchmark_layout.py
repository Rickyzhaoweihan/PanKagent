"""Offline layout benchmark: python -m tests_results.benchmark_layout."""
import asyncio
import json
import time

from pankgraph_results.layout import LayoutService
from tests_results.test_layout import fixture, overlaps


async def benchmark():
    rows = []
    for count in (25, 50, 100):
        for dense in (False, True):
            service = LayoutService(max_nodes=100, timeout_seconds=5)
            start = time.monotonic()
            result = await service.layout(fixture(count, dense), ["n000"])
            await service.close()
            row = {"nodes": count, "shape": "dense" if dense else "sparse",
                   "edges": len(result["combined_query_result"]["edges"]),
                   "status": result["layout"]["status"], "reason": result["layout"]["fallback_reason"],
                   "elapsed_ms": round((time.monotonic()-start)*1000, 2),
                   "displayed_nodes": len(result["xy_json"]), "node_overlap": overlaps(result["xy_json"]),
                   "edge_node_intersections": result["layout"]["metrics"].get("edge_node_intersection_count"),
                   "optimized_routes": result["layout"]["metrics"].get("optimized_route_count", 0),
                   "fallback_routes": result["layout"]["metrics"].get("fallback_route_count", 0),
                   "routed_edges": len(result["edge_routes"])}
            assert row["displayed_nodes"] == count and not row["node_overlap"]
            assert row["elapsed_ms"] < 6500, row
            rows.append(row)
            print(json.dumps(row), flush=True)
    return rows


if __name__ == "__main__":
    asyncio.run(benchmark())
