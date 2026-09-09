import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pankagent_vnext.grounding_inventory import (
    PUBLIC_CATALOG_LABELS, build_inventory, inventory_identity, load_inventory,
    stable_digest, write_inventory, catalog_query, observe_schema,
)
from pankagent_vnext.preplanning_grounding import (
    EntityIndex, Grounder, ground_question, grounding_guidance, relevant_schema,
)


class FakeGraph:
    def __init__(self):
        self.settings = SimpleNamespace(graph_version="PanKgraph_08_04")
        self.calls = []
        self.active = 0
        self.maximum = 0
        self.identity_checks = 0
        self.rows = {label: [] for label in PUBLIC_CATALOG_LABELS}
        self.rows["Gene"] = [
            {"id": "ENSG00000138031", "name": "ADCY3", "labels": ["Gene"], "synonyms": ["AC3"]},
            {"id": "ENSG00000001626", "name": "CFTR", "labels": ["Gene"]},
            {"id": "gene-one", "name": "ONE", "labels": ["Gene"], "synonyms": ["shared alias"]},
            {"id": "gene-two", "name": "TWO", "labels": ["Gene"], "synonyms": ["shared alias"]},
            {"id": "gene-was", "name": "WAS", "labels": ["Gene"]},
        ]
        self.rows["anatomical_structure"] = json.loads(
            (Path(__file__).parent / "fixtures/anatomy_20260907.json").read_text())
        self.rows["disease"] = [{"id": "MONDO_0005147", "name": "type 1 diabetes", "labels": ["disease", "ontology"]},
                                {"id": "MONDO_0005148", "name": "type 2 diabetes", "labels": ["disease", "ontology"]}]
        self.rows["kegg"] = [{"id": "hsa_04510", "name": "Focal adhesion", "labels": ["kegg"]}]
        self.rows["reactome"] = [{"id": "reactome-example", "name": "Focal adhesion", "labels": ["reactome"]}]

    def preview_identity(self):
        return {"graph_version": self.settings.graph_version, "identity_manifest_sha256": "manifest-one",
                "neo4j_uri": "bolt://private:password@localhost:1", "neo4j_database": "pankgraph"}

    async def _ensure_identity(self):
        self.identity_checks += 1

    async def _small_query(self, query, params=None):
        self.calls.append((query, params))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0)
            if "n.id IN $identifiers" in query:
                return [{"id": value, "labels": ["variants"]} for value in params["identifiers"] if value == "rs13393590"]
            if query.startswith("MATCH (n) UNWIND"):
                return [{"label": "Gene", "properties": ["id", "name"]}]
            if query.startswith("MATCH (a)-[r]->(b)"):
                return [{"source": ["Gene"], "relation": "SIGNAL_COLOC_WITH", "target": ["disease"], "properties": ["pp_h4_abf"]}]
            label = query.split("`")[1]
            return deepcopy(self.rows[label])
        finally:
            self.active -= 1

    async def semantic_vocabulary(self):
        return {"inventory_complete": True, "stages": ["Stage 1: recorded", "Stage 3: recorded"],
                "sources": ["HPAP"], "modalities": ["snMultiomics"], "assay_donor_sources": {"snMultiomics": ["HPAP"]}}


def make_index(graph=None):
    graph = graph or FakeGraph()
    inventory = asyncio.run(build_inventory(graph))
    return EntityIndex(inventory)


def candidate_ids(mentions):
    return {value["id"] for mention in mentions for value in mention["candidates"]}


def test_full_catalog_build_is_read_only_and_has_two_read_cap():
    graph = FakeGraph()
    inventory = asyncio.run(build_inventory(graph))
    assert inventory["catalog_complete"] is True
    assert inventory["counts"]["Gene"] == 5
    assert graph.maximum == 2
    assert len(graph.calls) == len(PUBLIC_CATALOG_LABELS)
    assert all(" LIMIT " not in query and "SKIP" not in query for query, _ in graph.calls)
    assert "password" not in json.dumps(inventory)
    assert "donor" not in inventory["counts"] and "Sample_node" not in inventory["counts"]
    assert all(set(value) <= {"id", "name", "entity_type", "labels", "aliases"} for value in inventory["records"])


def test_grounding_resolves_live_gene_variant_disease_before_plan():
    graph = FakeGraph()
    payload = asyncio.run(ground_question(graph, "Does AC3 coloc with T1D GWAS rs13393590 and molecular QTL?"))
    assert payload["status"] == "ready"
    assert {"ENSG00000138031", "MONDO_0005147", "rs13393590"} <= candidate_ids(payload["mentions"])
    coloc = payload["schema"]["relations"]["SIGNAL_COLOC_WITH"]
    assert all("Gene" in item["source"] and "disease" in item["target"] for item in coloc["paths"])
    query, parameters = graph.calls[-1]
    assert "$identifiers" in query and "rs13393590" not in query
    assert parameters == {"identifiers": ["rs13393590"]}


@pytest.mark.parametrize("wording", ["PLN", "pancreatic lymph node", "pancreatic LN", "PLN (pancreatic lymph node)"])
def test_pln_alias_does_not_add_nested_pancreas_or_lymph_tissue(wording):
    matches = make_index().match("How many " + wording + " samples have RNA?")
    assert candidate_ids(matches) == {"UBERON_0015865"}
    assert all(m["state"] == "resolved" for m in matches)


@pytest.mark.parametrize("record_index", range(58))
def test_every_recorded_anatomical_id_and_name_is_indexed(record_index):
    graph = FakeGraph()
    record = graph.rows["anatomical_structure"][record_index]
    index = make_index(graph)
    assert record["id"] in candidate_ids(index.match("Find " + record["id"]))
    assert record["id"] in candidate_ids(index.match("Find " + record["name"]))


def test_ambiguous_gene_alias_and_cross_collection_pathways_remain_ambiguous():
    index = make_index()
    shared = index.match("Describe shared alias")
    assert shared[0]["state"] == "ambiguous"
    assert candidate_ids(shared) == {"gene-one", "gene-two"}
    pathway = index.match("Focal adhesion annotations")
    assert pathway[0]["state"] == "ambiguous"
    assert candidate_ids(pathway) == {"hsa_04510", "reactome-example"}


def test_typo_or_new_entity_does_not_become_absence_or_guess():
    matches = make_index().match("Is MUC5A+ ductal cell linked to ADCY4?")
    assert "ENSG00000138031" not in candidate_ids(matches)
    assert "CL_0002079_MUC5B" not in candidate_ids(matches)


def test_common_words_do_not_resolve_as_gene_without_explicit_context():
    index = make_index()
    assert "gene-was" not in candidate_ids(index.match("What was expressed?"))
    assert "gene-was" in candidate_ids(index.match("Is WAS expressed?"))
    assert "gene-was" in candidate_ids(index.match("Is gene was expressed?"))


def test_terminal_positive_negative_cell_states_do_not_collapse():
    assert "CL_0002079_MUC5B" in candidate_ids(make_index().match("MUC5B+ ductal cells"))
    assert "CL_0002079_MUC5B" not in candidate_ids(make_index().match("MUC5B- ductal cells"))


def test_stage_inventory_does_not_invent_stage_two_or_expand_assay_automatically():
    payload = asyncio.run(ground_question(FakeGraph(), "Find T1D stage 2 donors with islet RNA samples"))
    assert payload["sample_terminology"]["stages"] == ["Stage 1: recorded", "Stage 3: recorded"]
    assert "constraints" not in payload
    assert "MONDO_0005148" not in candidate_ids(payload["mentions"])


def test_disk_cache_identity_and_digest_validation(tmp_path):
    graph = FakeGraph()
    inventory = asyncio.run(build_inventory(graph))
    path = tmp_path / "catalog.json"
    write_inventory(path, inventory)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_inventory(path, inventory_identity(graph))["content_digest"] == inventory["content_digest"]
    bad_identity = inventory_identity(graph) | {"graph_release": "other"}
    with pytest.raises(ValueError, match="stale_or_invalid"):
        load_inventory(path, bad_identity)
    damaged = deepcopy(inventory)
    damaged["records"][0]["name"] = "changed"
    path.write_text(json.dumps(damaged))
    with pytest.raises(ValueError, match="stale_or_invalid"):
        load_inventory(path, inventory_identity(graph))


def test_warm_cache_reuses_metadata_and_new_identity_invalidates():
    graph = FakeGraph()
    grounder = Grounder(graph)
    async def run():
        first = await grounder.resolve("CFTR")
        calls = len(graph.calls)
        second = await grounder.resolve("ADCY3")
        assert len(graph.calls) == calls
        assert first["catalog_digest"] == second["catalog_digest"]
        graph.settings.graph_version = "different-release"
        with pytest.raises(ValueError, match="release_mismatch"):
            await grounder.resolve("CFTR")
    asyncio.run(run())


def test_unavailable_grounding_is_not_a_zero_result_and_sanitizes_error():
    graph = FakeGraph()
    async def fail():
        raise RuntimeError("secret-password-with-query")
    graph._ensure_identity = fail
    result = asyncio.run(ground_question(graph, "CFTR expression"))
    assert result["status"] == "unavailable"
    assert result["error_category"] == "grounding_metadata_unavailable"
    assert "secret-password" not in json.dumps(result)
    assert "zero matches" not in json.dumps(result)
    assert "GENE_DETECTED_IN" in result["schema"]["relations"]


def test_grounding_timeout_is_bounded():
    graph = FakeGraph()
    async def slow():
        await asyncio.sleep(1)
    graph._ensure_identity = slow
    result = asyncio.run(ground_question(graph, "CFTR", timeout_seconds=.01))
    assert result["error_category"] == "grounding_timeout"
    assert result["latency_ms"] < 200


def test_full_schema_observation_is_diagnostic_and_never_self_admitted():
    graph = FakeGraph()
    observed = asyncio.run(observe_schema(graph))
    assert observed["complete_scan"] is True
    assert observed["admission"] == "diagnostic_only_requires_registry_review"
    assert all("LIMIT" not in q for q, _ in graph.calls)
    with pytest.raises(ValueError):
        catalog_query("Gene`) DELETE n //")


def test_guidance_preserves_ambiguity_and_marks_omitted_properties():
    graph = FakeGraph()
    payload = asyncio.run(ground_question(graph, "What is the comprehensive profile of shared alias?"))
    text = grounding_guidance(payload)
    assert "ambiguous" in text and "gene-one" in text and "gene-two" in text
    compact = grounding_guidance(payload, max_chars=8000)
    assert len(compact) < 8200
    assert "gene-one" in compact and "gene-two" in compact
    assert "additional_available_relations" in compact


def test_parallel_first_queries_share_one_catalog_scan():
    graph = FakeGraph()
    grounder = Grounder(graph)
    async def run():
        return await asyncio.gather(grounder.resolve("CFTR"), grounder.resolve("ADCY3"))
    results = asyncio.run(run())
    assert all(result["status"] == "ready" for result in results)
    assert len(graph.calls) == len(PUBLIC_CATALOG_LABELS)


def test_inventory_and_plan_guidance_digest_ignore_collection_time():
    graph = FakeGraph()
    first = asyncio.run(build_inventory(graph))
    second = asyncio.run(build_inventory(graph))
    assert first["built_at"] != second["built_at"]
    assert first["content_digest"] == second["content_digest"]
    payload = asyncio.run(ground_question(graph, "ADCY3 coloc"))
    before = grounding_guidance(payload)
    payload["latency_ms"] = 999999.0
    assert grounding_guidance(payload) == before


def test_gpu_compact_guidance_keeps_ids_and_correct_directions_under_1500_chars():
    payload = asyncio.run(ground_question(FakeGraph(), "Does ADCY3 coloc with T1D GWAS rs13393590 and molecular QTL?"))
    guidance = grounding_guidance(payload, max_chars=1500, relation_types=["SIGNAL_COLOC_WITH"])
    assert len(guidance) <= 1500
    assert "ENSG00000138031" in guidance and "rs13393590" in guidance and "MONDO_0005147" in guidance
    assert '"SIGNAL_COLOC_WITH":["Gene -> disease"]' in guidance


def test_warmed_sample_terminology_needs_no_per_question_catalog_read():
    graph = FakeGraph()
    grounder = Grounder(graph)
    async def run():
        await grounder.warm()
        calls = len(graph.calls)
        async def unavailable():
            raise AssertionError("Warm vocabulary should already be available")
        graph.semantic_vocabulary = unavailable
        result = await grounder.resolve("Find stage III HPAP donors with islet RNA samples")
        assert len(graph.calls) == calls
        terminology = result["sample_terminology"]
        assert terminology["requested_stage"]["canonical_values"] == ["Stage 3: recorded"]
        assert terminology["requested_stage"]["property_owner"] == "donor.t1d_stage"
        assert terminology["assay_capabilities"]["snMultiomics"]["components"] == ["RNA", "ATAC"]
    asyncio.run(run())


def test_generic_questions_without_named_entities_still_receive_schema():
    for question, label in [("How many variants are available?", "variants"),
                            ("Which genes have disease evidence?", "Gene"),
                            ("What sample modalities are recorded?", "Sample_node")]:
        schema = relevant_schema(question, [])
        assert label in schema["nodes"]
        assert schema["relations"]
        assert schema["schema_is_guidance_not_capability_limit"] is True


def test_canonical_adcy3_precedes_adcy8_historical_adcy3_alias():
    graph = FakeGraph()
    graph.rows['Gene'].append({'id': 'ENSG_ADCY8_FIXTURE', 'name': 'ADCY8', 'labels': ['Gene'], 'synonyms': ['ADCY3']})
    matches = make_index(graph).match('Does ADCY3 have recorded coloc evidence?')
    assert matches[0]['state'] == 'resolved'
    assert candidate_ids(matches) == {'ENSG00000138031'}
    assert matches[0]['candidates'][0]['match_kind'] == 'recorded_name'


def test_alias_precedence_does_not_resolve_duplicate_canonical_names():
    graph = FakeGraph()
    graph.rows['Gene'].append({'id': 'second-canonical-id', 'name': 'ADCY3', 'labels': ['Gene']})
    matches = make_index(graph).match('ADCY3')
    assert matches[0]['state'] == 'ambiguous'
    assert candidate_ids(matches) == {'ENSG00000138031', 'second-canonical-id'}


def test_corrupt_stage_stays_private_and_never_becomes_a_clinical_option():
    class CorruptStageGraph(FakeGraph):
        async def semantic_vocabulary(self):
            value = await super().semantic_vocabulary()
            value['stages'] = ['PanKbase', 'Stage 1: recorded', 'Stage 3: recorded', 'Stage 7: unsupported']
            return value
    graph = CorruptStageGraph()
    inventory = asyncio.run(build_inventory(graph))
    assert inventory['metadata_quality_diagnostics']['unrecognized_donor_stage_values'] == ['PanKbase', 'Stage 7: unsupported']
    assert inventory['sample_terminology']['stages'] == ['Stage 1: recorded', 'Stage 3: recorded']
    payload = asyncio.run(ground_question(graph, 'Find HPAP donors with stage 2 and islet RNA samples'))
    assert payload['sample_terminology']['stage_metadata_quality']['excluded_value_count'] == 2
    assert payload['sample_terminology']['requested_stage']['state'] == 'not_recorded'
    for limit in [14000, 7000, 4000]:
        guidance = grounding_guidance(payload, max_chars=limit)
        assert 'PanKbase' not in guidance and 'Stage 7' not in guidance
    from pankagent_vnext.query_recovery import stage_recovery
    recovery = stage_recovery('2', payload['sample_terminology'], 'PanKgraph_08_04')
    assert [suggestion['label'] for suggestion in recovery['suggestions']] == ['Use recorded stage 1', 'Use recorded stage 3']


def test_missing_stage_inventory_cannot_claim_no_recorded_stage():
    class MissingStageGraph(FakeGraph):
        async def semantic_vocabulary(self):
            value = await super().semantic_vocabulary()
            value['stages'] = None
            return value
    payload = asyncio.run(ground_question(MissingStageGraph(), 'Find stage 2 donors'))
    assert payload['sample_terminology']['requested_stage']['state'] == 'inventory_incomplete'
    assert payload['sample_terminology']['stage_metadata_quality']['state'] == 'inventory_unavailable'


@pytest.mark.parametrize('wording', ['PLN_B', 'PLN-B', 'pancreatic_lymph_node_B'])
def test_unknown_compound_qualifier_is_not_a_complete_pln_identity(wording):
    matches = make_index().match(f'Find {wording} samples')
    assert len(matches) == 1
    assert matches[0]['state'] == 'qualified' and matches[0]['identity_complete'] is False
    assert matches[0]['requested'] == wording
    assert matches[0]['unmatched_qualifier'] == 'b'
    assert candidate_ids(matches) == {'UBERON_0015865'}
    assert 'not a full identity resolution' in matches[0]['qualification_rule']


def test_recorded_exact_compound_alias_still_resolves_without_dropping_qualifier():
    graph = FakeGraph()
    graph.rows['anatomical_structure'].append({'id': 'recorded-qualified-region',
        'name': 'Recorded region B', 'synonyms': ['PLN_B'], 'labels': ['anatomical_structure']})
    matches = make_index(graph).match('Find PLN_B samples')
    assert matches[0]['state'] == 'resolved'
    assert candidate_ids(matches) == {'recorded-qualified-region'}


def test_canonical_precedence_never_eliminates_a_different_entity_type():
    graph = FakeGraph()
    graph.rows['anatomical_structure'].append({'id': 'different-type-alias',
        'name': 'Example cell type', 'synonyms': ['ADCY3'], 'labels': ['anatomical_structure']})
    matches = make_index(graph).match('ADCY3')
    assert matches[0]['state'] == 'ambiguous'
    assert candidate_ids(matches) == {'ENSG00000138031', 'different-type-alias'}


@pytest.mark.parametrize('changed_field', ['neo4j_uri', 'neo4j_database', 'identity_manifest_sha256'])
def test_source_identity_change_cannot_fall_back_to_a_stale_catalog(changed_field):
    graph = FakeGraph()
    async def run():
        first = await ground_question(graph, 'ADCY3')
        assert first['status'] == 'ready'
        before = graph.preview_identity()
        graph.preview_identity = lambda: before | {changed_field: 'changed-source'}
        async def failed_rebuild():
            raise ConnectionError('private connection details')
        graph._ensure_identity = failed_rebuild
        second = await ground_question(graph, 'ADCY3')
        assert second['status'] == 'unavailable' and second['mentions'] == []
        assert second['identity']['source_identity_digest'] != first['identity']['source_identity_digest']
        assert 'private connection details' not in json.dumps(second)
        assert 'ENSG00000138031' not in json.dumps(second)
    asyncio.run(run())
