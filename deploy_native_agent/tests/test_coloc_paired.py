import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source" / "gateway_results"))
from pankgraph_results.coloc_paired import read_members

RECORD = {"gene_id": "G", "qtl_signal_id": "G__set2"}
PROPERTIES = {"id": "edge", "snapshot_id": "snap"}


def retrieve(fault=None):
    async def page(request):
        assert request["filters"] == {"signal_role": "qtl"}
        assert request["ref"]["id"] == "edge"
        second = "cursor" in request
        result = {"release_id": "r", "state": "available", "object_id": "edge", "snapshot_id": "snap",
                  "records": [{"signal_role": "qtl", "object_id": "G", "snapshot_id": "snap",
                               "variant_id": "rs2" if second else "rs1", "credible_set_id": "key", "beta": None}],
                  "credible_sets": {"key": {"source_set": "G__set2", "object_id": "G", "n_members": 2,
                                            "description": "QTL", "data_source_url": "https://example.org",
                                            "completeness": "all source rows"}},
                  "has_more": not second, "next_cursor": None if second else "next"}
        if fault:
            fault(result)
        return result

    return asyncio.run(read_members(page, "r", RECORD, PROPERTIES, "qtl", 100))


def test_cursor_and_nullable_effect():
    rows, source, notes = retrieve()
    assert [row["snp"] for row in rows] == ["rs1", "rs2"]
    assert rows[0]["slope"] is None
    assert source["coverage"]["complete"] and not notes


@pytest.mark.parametrize("fault", [
    lambda p: p.update(snapshot_id="other"), lambda p: p.update(release_id="other"),
    lambda p: p["records"][0].update(signal_role="gwas"),
    lambda p: p["credible_sets"]["key"].update(source_set="other"),
    lambda p: p.update(has_more=False), lambda p: p["records"][0].update(variant_id="rs1"),
])
def test_inconsistent_pages_are_rejected(fault):
    with pytest.raises(ValueError):
        retrieve(fault)
