import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pankgraph_results.resource_registry import SOURCES, TSV_COLUMNS, source_for
from pankgraph_results.resources import ResourceManager, ResourceError, parse_association_tsv


HEADER = "\t".join(TSV_COLUMNS) + "\n"
DATA = (HEADER + "rs100\t0.9\t0.00001\tA\tG\t0.2\t3.5\nrs101\t0.1\t0.03\tC\tT\t-0.1\t1.2\n").encode()
SET = "ENSG00000001084__GCLC__credibleSet1"


def evidence(source="GTEx; SusieR", credible=SET, relation="PART_OF_QTL_SIGNAL", **properties):
    return {"graph_version": "test-rl", "nodes": [
        {"id": "rs100", "labels": ["sequence_variant"], "properties": {}},
        {"id": "ENSG00000001084", "labels": ["gene"], "properties": {"name": "GCLC"}}],
        "edges": [{"start_id": "rs100", "end_id": "ENSG00000001084", "type": relation,
                   "properties": {"data_source": source, "credible_set": credible, **properties}}]}


async def manager(tmp_path, handler, **settings):
    instance = ResourceManager(tmp_path, settings=SimpleNamespace(**settings))
    await instance._client.aclose()
    instance._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return instance


def run(coro):
    return asyncio.run(coro)


def test_exact_source_aliases_and_unverified_gwas():
    assert source_for("splicing; GTEx").prefix == "1_sQTL-gtex-susie"
    assert source_for("exon; INSPIRE").prefix == "1_exonQTL-inspire-susie"
    assert source_for("GTEx unknown") is None
    assert source_for("T1D").verified is False
    assert len(SOURCES) == 5
    for malformed in ("../private", "a/b", "https://elsewhere/x", "a.txt", "a?x=1"):
        with pytest.raises(ValueError):
            SOURCES[0].object_key(malformed)


def test_schema_numbers_and_bounds_are_strict():
    rows = parse_association_tsv(DATA)
    assert rows[0]["pip"] == .9
    for data, category in ((DATA.replace(b"nominal_p", b"p_value"), "schema_mismatch"),
                           (DATA.replace(b"0.9", b"NaN"), "invalid_statistic"),
                           (DATA + DATA.splitlines(keepends=True)[1], "duplicate_variant_in_set"),
                           (HEADER.encode(), "empty_resource")):
        with pytest.raises(ResourceError, match=category):
            parse_association_tsv(data)
    with pytest.raises(ResourceError, match="row_limit"):
        parse_association_tsv(DATA, max_rows=1)


def test_download_index_asset_and_cache_are_source_scoped(tmp_path):
    async def scenario():
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=DATA, headers={"ETag": '"v1"', "Last-Modified": "Wed, 04 Jun 2025 18:48:36 GMT"})
        m = await manager(tmp_path, handler)
        result = await m.resolve(evidence(tissue_name="pancreas", condition="non-diabetic", data_version="v1", pmid="34127860"))
        assert result["status"] == "partial"  # Data usable; coordinates unavailable.
        group = result["resource_groups"][0]
        assert group["context"]["tissue_name"] == "pancreas"
        assert group["context"]["condition"] == "non-diabetic"
        assert group["schema"] == list(TSV_COLUMNS)
        assert group["etag"] == '"v1"'
        assert result["resources_tabs"]["references"]["pmid:34127860"]["href"].endswith("34127860/")
        assert "image_url" not in result["resources_tabs"]["empirical_evidence"]
        path, media, name = await m.asset(group["asset_id"])
        assert path.read_bytes() == DATA and media == "text/tab-separated-values" and name.endswith(".txt")
        rows = await m.indexed_lookup(snp="rs100", data_source="GTEx; SusieR")
        assert rows["rows"][0]["gene"] == "ENSG00000001084"
        assert rows["rows"][0]["lead_definition"] == "maximum_pip_in_downloaded_set"
        assert not rows["coverage"]["exhaustive"]
        assert not (await m.indexed_lookup(snp="rs100", data_source="INSPIRE; SusieR"))["rows"]
        assert not (await m.indexed_lookup(snp="rs100' OR 1=1 --"))["rows"]
        await m.resolve(evidence())
        assert len(requests) == 1
        assert requests[0].url.path == "/1_eQTL-gtex-susie/" + SET + ".txt"
        before = len(requests)
        m.snapshot()
        assert len(requests) == before
        await m.close()
        # The local index and asset registry survive restart.
        reopened = ResourceManager(tmp_path)
        assert len((await reopened.indexed_lookup(gene="ENSG00000001084"))["rows"]) == 2
        await reopened.close()
    run(scenario())


@pytest.mark.parametrize("code,category", [(404, "not_found"), (403, "access_denied"), (500, "upstream_http_error")])
def test_remote_failure_is_explicit_and_never_fabricates_data(tmp_path, code, category):
    async def scenario():
        m = await manager(tmp_path, lambda request: httpx.Response(code, text="untrusted upstream error details"))
        result = await m.resolve(evidence())
        assert result["status"] == "unavailable" and not result["assets"]
        assert result["resource_groups"][0]["error_category"] == category
        assert "untrusted" not in json.dumps(result)
        assert not (await m.indexed_lookup(snp="rs100"))["rows"]
        await m.close()
    run(scenario())


def test_changed_remote_schema_invalidates_prior_index_and_etag(tmp_path):
    async def scenario():
        responses = [(200, DATA, '"v1"'), (304, b"", '"v1"'), (200, b"unexpected\tcolumns\n", '"v2"')]
        requests = []
        def handler(request):
            requests.append(request)
            status, raw, tag = responses.pop(0)
            return httpx.Response(status, content=raw, headers={"ETag": tag})
        m = await manager(tmp_path, handler, resource_ttl_seconds=0)
        await m.resolve(evidence())
        result = await m.resolve(evidence())
        assert result["resource_groups"][0]["cache"] == "revalidated"
        assert requests[1].headers["If-None-Match"] == '"v1"'
        result = await m.resolve(evidence())
        assert result["resource_groups"][0]["error_category"] == "schema_mismatch"
        assert not (await m.indexed_lookup(snp="rs100"))["rows"]
        await m.close()
    run(scenario())


def test_local_corruption_forces_refetch_and_oversized_objects_reject(tmp_path):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=DATA)
        m = await manager(tmp_path, handler)
        result = await m.resolve(evidence())
        path, _, _ = await m.asset(result["assets"][0]["asset_id"])
        path.write_bytes(b"corrupt")
        with pytest.raises(KeyError):
            await m.asset(result["assets"][0]["asset_id"])
        await m.resolve(evidence())
        assert len(calls) == 2
        m.max_bytes = 2
        m.ttl = 0
        result = await m.resolve(evidence())
        assert result["resource_groups"][0]["error_category"] == "object_too_large"
        await m.close()
    run(scenario())


def test_registry_blocks_wrong_sources_relations_and_gene_sets_without_fetch(tmp_path):
    async def scenario():
        calls = []
        m = await manager(tmp_path, lambda request: calls.append(request) or httpx.Response(200, content=DATA))
        for item, category in ((evidence(source="other study"), "source_or_credible_set_unmapped"),
                               (evidence(relation="PART_OF_GWAS_SIGNAL"), "source_relation_mismatch"),
                               (evidence(credible="../../private"), "invalid_credible_set"),
                               (evidence(credible="ENSG00000000001__OTHER__credibleSet1"), "gene_context_mismatch")):
            result = await m.resolve(item)
            assert result["resource_groups"][0]["error_category"] == category
        assert not calls
        await m.close()
    run(scenario())


def test_modality_links_and_reference_safety_do_not_invent_cell_conditions(tmp_path):
    async def scenario():
        m = await manager(tmp_path, lambda request: pytest.fail("No resource network call for RNA"))
        data = evidence(relation="RNA_DETECTED_IN", publication_url="javascript:alert(1)")
        result = await m.resolve(data)
        assert result["status"] == "not_applicable"
        links = json.dumps(result["resources_tabs"])
        assert "scRNA" in links and "scATAC" not in links and "javascript" not in links
        assert "T1D" not in links and "beta" not in links.lower()
        data["edges"][0]["type"] = "ATAC_ENRICHED_IN"
        result = await m.resolve(data)
        links = json.dumps(result["resources_tabs"])
        assert "scATAC" in links and "scRNA" not in links
        await m.close()
    run(scenario())


def test_duplicate_exact_key_requests_share_single_fetch_and_limit_is_visible(tmp_path):
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(request)
            await asyncio.sleep(.01)
            return httpx.Response(200, content=DATA)
        m = await manager(tmp_path, handler, resource_max_objects=1)
        first, second = await asyncio.gather(m.resolve(evidence()), m.resolve(evidence()))
        assert len(calls) == 1
        data = evidence()
        data["edges"].append(evidence(source="INSPIRE; SusieR")["edges"][0])
        result = await m.resolve(data)
        assert any(group.get("error_category") == "resolve_object_limit" for group in result["resource_groups"])
        await m.close()
    run(scenario())


def test_direct_download_allows_only_registered_key_and_modern_aliases(tmp_path):
    async def scenario():
        calls = []
        m = await manager(tmp_path, lambda request: calls.append(request) or httpx.Response(200, content=DATA))
        path, _, name = await m.download("1_eQTL-gtex-susie", SET)
        assert path.read_bytes() == DATA and name == SET + ".txt"
        with pytest.raises(KeyError):
            await m.download("https://attacker.invalid", SET)
        with pytest.raises(KeyError):
            await m.download("1_eQTL-gtex-susie", "../not-allowed")
        for alias in ("credibleset", "credible_set_id"):
            data = evidence(tissue_id="UBERON:0001264", data_version="2026-01")
            props = data["edges"][0]["properties"]
            props[alias] = props.pop("credible_set")
            result = await m.resolve(data)
            assert result["resource_groups"][0]["context"]["tissue_id"] == "UBERON:0001264"
        assert len(calls) == 1
        await m.close()
    run(scenario())


def test_changed_valid_object_replaces_rows_and_cache_eviction_is_explicit(tmp_path):
    async def scenario():
        bodies = [DATA, DATA.replace(b"rs100", b"rs102")]
        m = await manager(tmp_path, lambda request: httpx.Response(200, content=bodies.pop(0)), resource_ttl_seconds=0)
        first = await m.resolve(evidence())
        second = await m.resolve(evidence())
        assert first["assets"][0]["sha256"] != second["assets"][0]["sha256"]
        assert not (await m.indexed_lookup(snp="rs100"))["rows"]
        assert (await m.indexed_lookup(snp="rs102"))["rows"]
        await m.close()
        other = await manager(tmp_path / "small", lambda request: httpx.Response(200, content=DATA),
                              resource_cache_max_bytes=len(DATA) + 1)
        first = await other.resolve(evidence())
        await other.resolve(evidence(source="INSPIRE; SusieR"))
        with pytest.raises(KeyError):
            await other.asset(first["assets"][0]["asset_id"])
        assert not (await other.indexed_lookup(data_source="GTEx; SusieR"))["rows"]
        assert (await other.indexed_lookup(data_source="INSPIRE; SusieR"))["rows"]
        await other.close()
    run(scenario())


def test_plot_failure_retains_download_and_does_not_mark_untested_service_healthy(tmp_path, monkeypatch):
    async def scenario():
        m = await manager(tmp_path, lambda request: httpx.Response(200, content=DATA))
        await m.resolve(evidence(relation="GENE_ENRICHED_IN"))
        assert m.snapshot()["state"] == "unknown"
        async def coordinates(ids):
            return {variant: {"chrom": "1", "pos": index + 100, "assembly": "GRCh38", "verified": True, "source": "dbSNP"}
                    for index, variant in enumerate(ids)}
        m.coordinate_lookup = coordinates
        def broken_plot(*args, **kwargs):
            raise RuntimeError("private internal details")
        monkeypatch.setattr("pankgraph_results.resources.render_regional_plot", broken_plot)
        result = await m.resolve(evidence())
        assert result["status"] == "partial"
        assert result["resource_groups"][0]["plot"]["error_category"] == "plot_generation_failed"
        assert (await m.asset(result["assets"][0]["asset_id"]))[0].exists()
        assert "private internal" not in json.dumps(result)
        await m.close()
    run(scenario())


def test_seeded_asset_url_is_rebased_without_refetch_or_changing_bytes(tmp_path):
    async def scenario():
        m = await manager(tmp_path, lambda request: httpx.Response(200, content=DATA))
        original = await m.resolve(evidence())
        first = original["assets"][0]
        await m.close()
        mounted = ResourceManager(tmp_path, public_base="/pankgraph-vnext/api/resources")
        await mounted._client.aclose()
        mounted._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Must use seeded bytes")))
        result = await mounted.resolve(evidence())
        asset = result["assets"][0]
        assert asset["url"] == "/pankgraph-vnext/api/resources/" + first["asset_id"]
        assert asset["asset_id"] == first["asset_id"] and asset["sha256"] == first["sha256"]
        assert (await mounted.asset(asset["asset_id"]))[0].read_bytes() == DATA
        assert result["resources_tabs"]["empirical_evidence"]["download_url"] == asset["url"]
        await mounted.close()
    run(scenario())


def test_database_source_urls_are_external_and_preserve_exact_ensembl_release(tmp_path):
    async def scenario():
        archive = "https://jul2023.archive.ensembl.org/Homo_sapiens/Gene/Summary?g=ENSG00000001084"
        cl = "https://www.ebi.ac.uk/ols4/ontologies/cl/classes/CL%3A0000169"
        data = evidence(relation="GENE_DETECTED_IN")
        data["nodes"][1]["properties"].update(data_source="Ensembl release 110", data_source_url=archive)
        data["nodes"].append({"id": "CL_0000169", "labels": ["cell_type"], "properties": {"name": "type B pancreatic cell", "data_source_url": cl}})
        data["edges"][0]["properties"].update(data_source_url=cl, publication_url="https://pubmed.ncbi.nlm.nih.gov/34127860/")
        m = await manager(tmp_path, lambda request: pytest.fail("No download for source links"))
        result = await m.resolve(data)
        tabs = result["resources_tabs"]
        assert list(tabs["references"]) == ["pmid:34127860"]
        external = [link[2] for link in tabs["external_links"]]
        assert external.count(cl) == 1 and external.count(archive) == 1
        assert not any("www.ensembl.org" in url for url in external)
        assert not any(value["href"] in {archive, cl} for value in tabs["references"].values())
        data["edges"][0]["properties"]["data_source_url"] = "https://doi.org/10.1038/s41588-021-00880-5"
        result = await m.resolve(data)
        assert "doi:10.1038/s41588-021-00880-5" in result["resources_tabs"]["references"]
        await m.close()
    run(scenario())
