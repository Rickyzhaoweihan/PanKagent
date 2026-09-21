"""Offline scientific and API boundaries for existing recorded coloc results."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from pankagent_vnext.graph import validate_cypher
from pankgraph_results.coloc import (
    CATALOG_COUNT, CATALOG_QUERY, GWAS_MATCH, QTL_MATCH, MEMBER_RETURN,
    ColocExplorer, T1D, coloc_router, record_from_row,
)
from pankgraph_results.resources import ResourceError, ResourceManager

RELEASE = "PanKgraph_test_release"
GENE = "ENSG00000138031"
CS = GENE + "__ADCY3__credibleSet1"
HEADER = "snp\tpip\tnominal_p\teffect_allele\tother_allele\tslope\tlbf\n"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def core(**properties):
    return {"gene_id": GENE, "gene_name": "ADCY3", "disease_id": T1D, "disease_name": "Type 1 diabetes",
        "properties": {"qtl_signal_id": CS, "gwas_signal_id": "ADCY3__credibleSet1__selected",
            "coloc_dataset": "t1d_eQTL-inspire_coloc", "data_source": "HIRN_T1D_QTL_GWAS", "data_version": "v1.0",
            "gwas_lead_vars": "rs1", "qtl_lead_vars": "rs1", "nsnp": 1273,
            "pp_h0_abf": .001, "pp_h1_abf": .002, "pp_h2_abf": .003, "pp_h3_abf": .194, "pp_h4_abf": .8,
            **properties}}


def member(rid="rs1", *, role="gwas", size=2, pip=.5, **props):
    return {"variant_id": rid, "chromosome": "2", "start": 99, "end": 100, "assembly": "GRCh38.p14",
        "coordinate_system": "0-based-half-open", "coordinate_system_verified": True,
        "ref": "A", "alt": "G", "properties": {"pip": pip,
            "p_value" if role == "gwas" else "nominal_p": .0001,
            "effect_allele": "G", "non_effect_allele" if role == "gwas" else "other_allele": "A",
            "slope": .2 if role == "qtl" else None,
            "credible_set_size" if role == "gwas" else "n_snp": size,
            "data_source": "GWAS_finemapping_V1" if role == "gwas" else "INSPIRE; SusieR",
            "data_version": "source-v1", **props}}


def tsv(ids=("rs1", "rs2"), pip=.5):
    return (HEADER + "".join(f"{rid}\t{pip}\t0.0001\tG\tA\t0.2\t5\n" for rid in ids)).encode()


class FakeQuery:
    def __init__(self, *, catalog=None, gwas=None, qtl=None, page_size=1000):
        self.data = {"catalog": [core()] if catalog is None else catalog,
            "gwas": [member(), member("rs2")] if gwas is None else gwas,
            "qtl": [member(role="qtl")] if qtl is None else qtl}
        self.settings = SimpleNamespace(max_rows=page_size)
        self.calls = []
        self.fail = set()

    async def execute_query(self, query, parameters, step_id="", **kwargs):
        self.calls.append((query, copy.deepcopy(parameters), step_id))
        assert not validate_cypher(query, {"complete": False, "constraints": []}, parameters)
        kind = step_id.split("_")[1]
        if kind in self.fail:
            raise RuntimeError("private upstream error must not escape")
        values = self.data[kind]
        rows = [{"total": len(values)}] if step_id.endswith("_count") else values[parameters["offset"]:parameters["offset"] + parameters["page_size"]]
        return {"rows": copy.deepcopy(rows), "status": "complete" if rows else "empty", "truncated": False,
            "graph_version": RELEASE}

    async def close(self):
        pass


class FakeResources:
    def __init__(self, tmp_path, raw=None, error=None):
        self.path = tmp_path / "source.tsv"
        self.path.write_bytes(tsv() if raw is None else raw)
        self.error, self.calls = error, []

    async def download(self, source, credible_set):
        self.calls.append((source, credible_set))
        if self.error:
            raise self.error
        return self.path, "text/tab-separated-values", "source.tsv"

    async def close(self):
        pass


async def no_coordinates(ids):
    return {}


def explorer(tmp_path, *, query=None, resources=None, coordinates=no_coordinates, manifest=""):
    return ColocExplorer(query or FakeQuery(), resources or FakeResources(tmp_path), coordinates, RELEASE,
        SimpleNamespace(public_path="/pankgraph-vnext", resource_timeout=.2, coloc_extract_manifest=manifest))


async def detail(service):
    rid = (await service.catalog())["records"][0]["id"]
    return await service.detail(rid)


def test_catalog_record_identity_and_missing_posteriors_are_not_zero():
    row = core(pp_h0_abf=0, pp_h1_abf=None, pp_h2_abf=float("nan"), pp_h3_abf=-.1, pp_h4_abf=1.1)
    result = record_from_row(row, RELEASE)
    assert len(result["id"]) == 64
    assert result["posteriors"] == {"h0": 0, "h1": None, "h2": None, "h3": None, "h4": None}
    assert result["notices"]
    assert record_from_row(core(), RELEASE)["id"] == result["id"]
    assert record_from_row(core(), "new-release")["id"] != result["id"]
    assert result["gwas_credible_set_id"] == "ADCY3__credibleSet1"
    assert result["nsnp"] == 1273  # Original analysis denominator, never membership count.


@pytest.mark.anyio
async def test_catalog_all_records_paginates_caches_and_never_filters_on_h4(tmp_path):
    rows = [core(qtl_signal_id=CS + str(i), pp_h4_abf=.2) for i in range(23)]
    query = FakeQuery(catalog=rows, page_size=4)
    service = explorer(tmp_path, query=query)
    result = await service.catalog()
    assert len(result["records"]) == 23
    assert result["coverage"]["complete"]
    assert len(query.calls) == 7
    assert await service.catalog() == result
    assert len(query.calls) == 7
    assert all(call[1]["disease_id"] == T1D for call in query.calls)


@pytest.mark.anyio
async def test_full_membership_not_lead_only_and_not_graph_display_bound(tmp_path):
    ids = ["rs" + str(i) for i in range(1, 403)]
    query = FakeQuery(gwas=[member(rid, size=402) for rid in ids], page_size=100)
    result = await detail(explorer(tmp_path, query=query))
    assert result["coverage"]["gwas_count"] == 402
    assert result["coverage"]["qtl_count"] == 2
    assert result["coverage"]["shared_count"] == 2
    assert result["coverage"]["complete"] is True
    assert len(result["graph"]["nodes"]) == 404
    gwas_calls = [call for call in query.calls if call[2] == "coloc_gwas_members"]
    assert len(gwas_calls) == 5
    for sql, params, _ in gwas_calls:
        assert "r.credible_set_id=$signal_id" in sql
        assert "v.id IN" not in sql
        assert params["signal_id"] == "ADCY3__credibleSet1"
        assert params["data_source"] == "GWAS_finemapping_V1"
    qtl_calls = [call for call in query.calls if call[2] == "coloc_qtl_members"]
    assert qtl_calls[0][1]["data_source"] == "INSPIRE; SusieR"
    assert qtl_calls[0][1]["tissue_id"] == "UBERON_0000006"
    assert qtl_calls[0][1]["gene_id"] == GENE


@pytest.mark.anyio
async def test_denied_qtl_file_preserves_record_and_marks_graph_lead_incomplete(tmp_path):
    query = FakeQuery(qtl=[member(role="qtl", size=28)])
    service = explorer(tmp_path, query=query, resources=FakeResources(tmp_path, error=ResourceError("access_denied")))
    result = await detail(service)
    assert result["record"]["posteriors"]["h4"] == .8
    assert result["coverage"]["gwas_complete"] is True
    assert result["coverage"]["qtl_complete"] is False
    assert result["coverage"]["qtl_expected"] == 28
    assert result["coverage"]["qtl_omitted"] == 27
    assert result["coverage"]["qtl_count"] == 1
    assert result["status"] == "partial"
    assert result["sources"][-1]["error_category"] == "access_denied"
    assert any(edge["~type"] == "SIGNAL_COLOC_WITH" for edge in result["graph"]["edges"])


@pytest.mark.anyio
async def test_valid_but_short_file_cannot_be_claimed_complete_and_zero_graph_is_unknown(tmp_path):
    query = FakeQuery(gwas=[], qtl=[member(role="qtl", size=28)])
    result = await detail(explorer(tmp_path, query=query, resources=FakeResources(tmp_path, raw=tsv(["rs1"]))))
    assert not result["coverage"]["gwas_complete"]
    assert result["coverage"]["gwas_expected"] is None
    assert not result["coverage"]["qtl_complete"]
    assert result["coverage"]["qtl_expected"] == 28
    assert any("file contains 1 rows" in text for text in result["notices"])


@pytest.mark.anyio
async def test_missing_graph_fields_and_float_rounding_not_conflicting_raw_stats(tmp_path):
    row = member(role="qtl", pip=.50000000000001)
    row["properties"].pop("slope")
    row["properties"].pop("other_allele")
    result = await detail(explorer(tmp_path, query=FakeQuery(qtl=[row])))
    assert result["coverage"]["qtl_complete"]
    assert result["variants"][0]["qtl"]["pip"] == .5
    assert result["variants"][0]["qtl"]["slope"] == .2


@pytest.mark.anyio
async def test_true_source_conflict_withholds_stats_and_exposes_both_original_sources(tmp_path):
    result = await detail(explorer(tmp_path, query=FakeQuery(qtl=[member(role="qtl", pip=.9)])))
    assert not result["coverage"]["qtl_complete"]
    variant = result["variants"][0]
    assert variant["qtl"]["member"] is True
    assert variant["qtl"]["pip"] is None
    assert any("conflicting variants" in text for text in result["notices"])
    graph_qtl = next(edge for edge in result["graph"]["edges"] if edge["~type"] == "PART_OF_QTL_SIGNAL")
    assert graph_qtl["~properties"]["pip"] == .9
    assert result["sources"][-1]["download_url"].startswith("/pankgraph-vnext/api/resources/download?")


@pytest.mark.anyio
async def test_missing_independent_graph_branch_does_not_erase_primary_or_raw_qtl(tmp_path):
    query = FakeQuery()
    query.fail.add("gwas")
    result = await detail(explorer(tmp_path, query=query))
    assert result["coverage"]["qtl_count"] == 2
    assert result["coverage"]["qtl_complete"] is True
    assert result["coverage"]["gwas_count"] == 0
    assert result["coverage"]["gwas_complete"] is False
    assert result["record"]["posteriors"]["h4"] == .8
    assert "private upstream" not in json.dumps(result)


@pytest.mark.anyio
async def test_unregistered_coloc_source_cannot_borrow_membership_or_raw_objects(tmp_path):
    query = FakeQuery(catalog=[core(data_source="unregistered-analysis")])
    service = explorer(tmp_path, query=query)
    result = await detail(service)
    assert result["record"]["data_source"] == "unregistered-analysis"
    assert result["coverage"]["gwas_count"] == 0
    assert result["coverage"]["qtl_count"] == 0
    assert not result["coverage"]["complete"]
    assert service.resources.calls == []
    assert all(call[2].startswith("coloc_catalog") for call in query.calls)


@pytest.mark.anyio
async def test_non_rs_members_remain_with_contract_bed_coordinates_and_conflicts_withheld(tmp_path):
    query = FakeQuery(gwas=[member(), member("2:25107712_CT_C")])
    async def coords(ids):
        return {"rs1": {"chrom": "2", "pos": 111, "verified": True, "assembly": "GRCh38", "source": "dbSNP157_GRCh38.p14"},
            "rs2": {"chrom": "2", "pos": 999, "verified": True, "assembly": "GRCh37"}}
    result = await detail(explorer(tmp_path, query=query, coordinates=coords))
    by_id = {row["id"]: row for row in result["variants"]}
    assert by_id["2:25107712_CT_C"]["position"] == 100
    assert by_id["2:25107712_CT_C"]["coordinate_source"] == "configured_graph_GRCh38_BED"
    assert by_id["rs1"]["position"] is None
    assert by_id["rs2"]["position"] is None
    assert any("conflicting GRCh38" in text for text in result["notices"])
    assert result["coordinate_build"] == "GRCh38"


@pytest.mark.anyio
async def test_missing_or_wrong_assembly_does_not_acquire_fabricated_positions(tmp_path):
    query = FakeQuery()
    for rows in (query.data["gwas"], query.data["qtl"]):
        for row in rows:
            row["assembly"] = "GRCh37"
    result = await detail(explorer(tmp_path, query=query))
    assert result["coordinate_build"] is None
    assert result["coverage"]["coordinate_count"] == 0
    assert all(row["position"] is None for row in result["variants"])


@pytest.mark.anyio
async def test_legacy_graph_starts_do_not_invent_false_conflicts_or_non_rs_positions(tmp_path):
    query = FakeQuery(gwas=[member(), member("2:25107712_CT_C")])
    for rows in (query.data["gwas"], query.data["qtl"]):
        for row in rows:
            row.pop("coordinate_system")
            row.pop("coordinate_system_verified")
    async def coords(ids):
        return {"rs1": {"chrom": "2", "pos": 99, "verified": True, "assembly": "GRCh38", "source": "dbSNP157_GRCh38.p14"}}
    result = await detail(explorer(tmp_path, query=query, coordinates=coords))
    variants = {row["id"]: row for row in result["variants"]}
    assert variants["rs1"]["position"] == 99
    assert variants["rs1"]["coordinate_source"] == "dbSNP157_GRCh38.p14"
    assert variants["2:25107712_CT_C"]["position"] is None
    assert not any("conflicting GRCh38" in note for note in result["notices"])
    assert any("lack a verified coordinate convention" in note for note in result["notices"])


@pytest.mark.anyio
async def test_multiple_membership_versions_are_explicitly_ambiguous(tmp_path):
    qtl = [member(role="qtl", data_version="v1"), member(role="qtl", data_version="v2")]
    result = await detail(explorer(tmp_path, query=FakeQuery(qtl=qtl)))
    assert not result["coverage"]["qtl_complete"]
    source = next(source for source in result["sources"] if source["label"] == "Neo4j QTL signal membership")
    assert source["source_versions"] == ["v1", "v2"]
    assert source["status"] == "partial"


def manifest(tmp_path, record, *, role="qtl", path="extract.tsv", raw=None, **changes):
    raw = tsv() if raw is None else raw
    (tmp_path / "extract.tsv").write_bytes(raw)
    item = {"record_id": record["id"], "role": role, "relative_path": path,
        "sha256": hashlib.sha256(raw).hexdigest(), "assembly": "GRCh38", "scope": "credible_set",
        "signal_id": record["qtl_signal_id" if role == "qtl" else "gwas_credible_set_id"],
        "source_version": "verified-extract-v1", "label": "Pre-indexed exact set", **changes}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 1, "graph_version": RELEASE, "extracts": [item]}))
    return str(path)


@pytest.mark.anyio
async def test_exact_versioned_extract_and_download_avoid_s3_and_large_scans(tmp_path):
    record = record_from_row(core(), RELEASE)
    path = manifest(tmp_path, record)
    service = explorer(tmp_path, manifest=path)
    result = await detail(service)
    assert service.resources.calls == []
    assert result["sources"][-1]["sha256"] == hashlib.sha256(tsv()).hexdigest()
    assert result["sources"][-1]["coverage"]["scope"] == "credible_set"
    raw, _ = await service.extract_download(record["id"], "qtl")
    assert raw == tsv()


@pytest.mark.parametrize("change", [
    {"relative_path": "../outside.tsv"}, {"relative_path": "/tmp/arbitrary.tsv"},
    {"scope": "region"}, {"assembly": "GRCh37"}, {"sha256": "0" * 64}, {"signal_id": "unrelated"},
])
@pytest.mark.anyio
async def test_invalid_extract_is_not_used_and_never_hides_fallback_or_failure(tmp_path, change):
    record = record_from_row(core(), RELEASE)
    path = manifest(tmp_path, record, **change)
    service = explorer(tmp_path, manifest=path)
    result = await detail(service)
    assert service.resources.calls == [("inspire_eqtl", CS)]
    assert result["sources"][-1]["url"].startswith("https://pank-s3-to-share.s3.us-east-1.amazonaws.com/")
    assert not result["coverage"]["qtl_complete"]
    with pytest.raises((ResourceError, OSError)):
        await service.extract_download(record["id"], "qtl")


@pytest.mark.anyio
async def test_existing_resource_manager_exact_key_denial_is_not_empty_evidence(tmp_path):
    resources = ResourceManager(tmp_path / "resources")
    await resources._client.aclose()
    calls = []
    def remote(request):
        calls.append(str(request.url))
        return httpx.Response(403)
    resources._client = httpx.AsyncClient(transport=httpx.MockTransport(remote))
    try:
        result = await detail(explorer(tmp_path, resources=resources))
        assert calls == ["https://pank-s3-to-share.s3.us-east-1.amazonaws.com/1_eQTL-inspire-susie/" + CS + ".txt"]
        assert not result["coverage"]["qtl_complete"]
        assert result["record"]["gene_id"] == GENE
    finally:
        await resources.close()


@pytest.mark.anyio
async def test_routes_return_finite_data_stable_ids_and_safe_errors(tmp_path):
    service = explorer(tmp_path)
    app = FastAPI()
    app.include_router(coloc_router(service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/api/coloc/records")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        rid = response.json()["records"][0]["id"]
        result = await client.get("/api/coloc/records/" + rid)
        assert result.status_code == 200
        assert result.json()["version"] == 1
        assert (await client.get("/api/coloc/records/not-an-id")).status_code == 404
        assert (await client.post("/api/coloc/records")).status_code == 405
        assert (await client.get("/api/coloc/records/" + rid + "/download/qtl")).status_code == 404
        service._catalog = None
        service.query.fail.add("catalog")
        error = await client.get("/api/coloc/records")
        assert error.status_code == 503
        assert "private upstream" not in error.text


@pytest.mark.anyio
async def test_results_app_auth_prefix_and_no_inference_or_layout_side_effects(tmp_path):
    from tests_results.test_app import service as results_service
    query, resources = FakeQuery(), FakeResources(tmp_path)
    async with results_service(tmp_path, query=query, resources=resources, testing=False) as runtime:
        path = "/pankgraph-vnext/api/coloc/records"
        assert (await runtime.client.get(path)).status_code == 401
        assert query.calls == []
        response = await runtime.client.get(path, auth=("demo", "synthetic-test-password"))
        assert response.status_code == 200
        assert len(response.json()["records"]) == 1
        assert runtime.gateway.calls == 0
        assert runtime.layout.calls == 0
        assert resources.calls == []
        assert runtime.upstream_calls == []
        assert runtime.runtime.tasks == {}


@pytest.mark.anyio
async def test_explicit_response_bound_retains_partial_counts(tmp_path, monkeypatch):
    import pankgraph_results.coloc as module
    monkeypatch.setattr(module, "MAX_MEMBERS", 2)
    query = FakeQuery(gwas=[member(), member("rs3")])
    result = await detail(explorer(tmp_path, query=query))
    assert result["coverage"]["variant_count"] == 2
    assert result["coverage"]["omitted_count"] == 1
    assert result["coverage"]["complete"] is False
    assert not result["coverage"]["gwas_complete"]
    assert not result["coverage"]["qtl_complete"]
