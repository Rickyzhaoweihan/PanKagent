import json

import pytest

from pankgraph_results.coordinates import CoordinateLookup


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_verified_shared_bed_coordinates_and_ambiguous_mapping():
    reader = CoordinateLookup()
    calls = []
    async def output(*args):
        calls.append(args)
        if args == ("metadata",):
            return json.dumps({"dbsnp_build": "157", "vcf": "/db/GCF_000001405.40.gz", "indexed": True}).encode()
        return b':ID,chr:String,start_loc:Int,data_version:String\nrs1,6,32186195,157\nrs2,1,20,157\nrs2,2,20,157\nrs2,1,20,157\n'
    reader._run = output
    found = await reader(["rs1", "rs2", "not-a-command"])
    assert found["rs1"]["pos"] == 32186196
    assert found["rs1"]["verified"] is True
    assert "rs2" not in found
    assert calls[1] == ("rsid", "rs1", "rs2", "--format", "csv")
    await reader(["rs1", "rs2"])
    assert len(calls) == 2


@pytest.mark.anyio
async def test_wrong_assembly_fails_without_querying_coordinates():
    reader = CoordinateLookup()
    calls = []
    async def output(*args):
        calls.append(args)
        return b'{"dbsnp_build":"157","vcf":"wrong.gz","indexed":true}'
    reader._run = output
    assert await reader(["rs1"]) == {}
    assert calls == [("metadata",)]
    assert reader.last_error == "RuntimeError"
