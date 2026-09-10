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
        self.annotation_sources = [{"values": ["KEGG", "Reactome"], "record_count": 8, "valued_records": 8}]
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
            if query.startswith("MATCH ()-[r:`FUNCTION_ANNOTATION`]"):
                return deepcopy(self.annotation_sources)
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
    assert len(graph.calls) == len(PUBLIC_CATALOG_LABELS) + 1
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
    assert len(graph.calls) == len(PUBLIC_CATALOG_LABELS) + 1


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


def contextual_graph():
    graph = FakeGraph()
    graph.rows['Gene'].extend([
        {'id': 'ENSG00000102575', 'name': 'ACP5', 'synonyms': ['HPAP'], 'labels': ['Gene']},
        {'id': 'ENSG00000120370', 'name': 'GORAB', 'synonyms': ['GO'], 'labels': ['Gene']},
        {'id': 'ENSG00000170827', 'name': 'CELP', 'synonyms': ['CELL'], 'labels': ['Gene']},
        {'id': 'ENSG00000164935', 'name': 'DCSTAMP', 'synonyms': ['FIND'], 'labels': ['Gene']},
        {'id': 'ENSG00000198523', 'name': 'PLN', 'labels': ['Gene']},
        {'id': 'novel-gene', 'name': 'UNSEEN', 'synonyms': ['newAlias7'], 'labels': ['Gene']},
    ])
    graph.rows['GO_term'] = [
        {'id': 'GO:0008150', 'name': 'biological_process', 'go_domain': 'biological_process', 'labels': ['GO_term']},
        {'id': 'go-pathway', 'name': 'Focal adhesion', 'go_domain': 'cellular_component', 'labels': ['GO_term']},
    ]
    return graph


@pytest.mark.parametrize('question,alias,role', [
    ('How many HPAP donors have stage-3 T1D?', 'hpap', 'recorded_dataset_source'),
    ('Find HPAP donors with spleen standalone scRNA-seq only.', 'find', 'request_verb'),
    ('Please find CFTR expression', 'find', 'request_verb'),
    ('Can you find CFTR expression?', 'find', 'request_verb'),
    ('What biological-process GO annotations are recorded for CFTR?', 'go', 'ontology_vocabulary'),
    ('Count single-cell RNA-seq samples for HPAP.', 'single-cell', 'assay_vocabulary'),
    ('Compare cell types where CFTR is expressed.', 'cell', 'entity_class_vocabulary'),
])
def test_context_words_do_not_become_gene_anchors_but_preserve_catalog_provenance(question, alias, role):
    mentions = make_index(contextual_graph()).match(question)
    mention = next(m for m in mentions if m['requested'] == alias)
    assert mention['state'] == 'incidental'
    assert mention['candidates'] == []
    assert mention['incidental_candidates'][0]['entity_type'] == 'Gene'
    assert mention['context_role']['kind'] == role


@pytest.mark.parametrize('alias,id', [('HPAP', 'ENSG00000102575'), ('GO', 'ENSG00000120370'), ('FIND', 'ENSG00000164935'), ('CELL', 'ENSG00000170827'), ('PLN', 'ENSG00000198523')])
def test_explicit_gene_roles_preserve_real_ambiguous_word_aliases(alias, id):
    mentions = make_index(contextual_graph()).match('Show gene ' + alias + ' expression in donor samples.')
    assert id in candidate_ids(mentions)
    mention = next(m for m in mentions if any(c['id'] == id for c in m['candidates']))
    assert mention['state'] == 'resolved'


def test_new_gene_like_alias_is_not_suppressed_by_a_general_sample_question():
    matches = make_index(contextual_graph()).match('Is newAlias7 expressed in HPAP donor samples?')
    assert 'novel-gene' in candidate_ids(matches)
    assert 'ENSG00000102575' not in candidate_ids(matches)


@pytest.mark.parametrize('wording', ['pancreatic lymph node (PLN)', 'PLN (pancreatic lymph node)'])
def test_user_parenthetical_expansion_disambiguates_tissue_from_same_named_gene(wording):
    matches = make_index(contextual_graph()).match('How many ' + wording + ' samples have RNA?')
    assert candidate_ids(matches) == {'UBERON_0015865'}
    assert any(any(c['id'] == 'ENSG00000198523' for c in m.get('incidental_candidates', [])) for m in matches)
    assert all(m['state'] == 'resolved' for m in matches)


def test_unexpanded_cross_type_pln_stays_ambiguous():
    mention = make_index(contextual_graph()).match('PLN')[0]
    assert mention['state'] == 'ambiguous'
    assert candidate_ids([mention]) == {'UBERON_0015865', 'ENSG00000198523'}


def test_domain_phrase_is_a_property_scope_not_ontology_root_identity():
    mentions = make_index(contextual_graph()).match('What biological-process GO annotations are recorded for CFTR?')
    mention = next(m for m in mentions if m['requested'] == 'biological process')
    assert mention['state'] == 'incidental'
    assert mention['context_role']['canonical_binding'] == {'entity_type': 'GO_term', 'property': 'go_domain', 'value': 'biological_process'}
    exact = make_index(contextual_graph()).match('Describe GO:0008150')
    assert 'GO:0008150' in candidate_ids(exact)


def test_fgsea_uses_only_release_supported_pathway_collections_without_discarding_provenance():
    matches = make_index(contextual_graph()).match('What fGSEA evidence is recorded for Focal adhesion?')
    mention = next(m for m in matches if m['requested'] == 'focal adhesion')
    assert candidate_ids([mention]) == {'hsa_04510', 'reactome-example'}
    assert mention['state'] == 'ambiguous'  # Both supported collections remain.
    assert mention['incidental_candidates'][0]['id'] == 'go-pathway'
    graph = contextual_graph()
    graph.rows['reactome'] = []
    mention = next(m for m in make_index(graph).match('Focal adhesion fGSEA') if m['requested'] == 'focal adhesion')
    assert mention['state'] == 'resolved'
    assert candidate_ids([mention]) == {'hsa_04510'}


@pytest.mark.parametrize('question', ['What pancreatic QTL evidence links ADCY3?', 'Show pancreatic eQTL evidence for CFTR.'])
def test_pancreatic_qtl_adjective_adds_verified_pancreas_tissue_anchor(question):
    matches = make_index(contextual_graph()).match(question)
    mention = next(m for m in matches if m['requested'] == 'pancreatic')
    assert mention['state'] == 'resolved'
    assert candidate_ids([mention]) == {'UBERON_0001264'}
    assert mention['context_role']['kind'] == 'qtl_tissue_adjective'


@pytest.mark.parametrize('question', ['pancreatic lymph node QTL', 'pancreatic islet QTL', 'pancreatic acinar cell QTL'])
def test_pancreatic_adjective_cannot_override_a_more_specific_anatomy(question):
    assert 'UBERON_0001264' not in candidate_ids(make_index(contextual_graph()).match(question))


def test_incidental_gene_aliases_do_not_trigger_planning_scope_guard():
    from pankagent_vnext.planning_scope import scope_issue
    question = 'Find HPAP donors with spleen standalone scRNA-seq only.'
    payload = asyncio.run(ground_question(contextual_graph(), question))
    plan = {'steps': [{'id': 'one', 'relation_types': ['HAS_SAMPLE'], 'constraints': [
        {'entity_type': 'anatomical_structure', 'property': 'id', 'value': 'UBERON_0002106'},
        {'entity_type': 'donor', 'property': 'data_source', 'value': 'HPAP'}]}]}
    assert scope_issue(question, payload, plan) is None


def test_source_context_comes_from_inventory_instead_of_a_fixed_hpap_word_rule():
    graph = contextual_graph()
    graph.rows['Gene'].append({'id': 'new-source-alias', 'name': 'NEWGENE', 'synonyms': ['NewStudy'], 'labels': ['Gene']})
    async def vocabulary():
        return {'inventory_complete': True, 'sources': ['NewStudy'], 'modalities': [], 'stages': []}
    graph.semantic_vocabulary = vocabulary
    index = make_index(graph)
    assert 'new-source-alias' not in candidate_ids(index.match('How many NewStudy donor samples exist?'))
    assert 'new-source-alias' in candidate_ids(index.match('What is the expression of gene NewStudy?'))
    ambiguous = index.match('What is NewStudy?')[0]
    assert ambiguous['state'] == 'ambiguous'
    assert ambiguous['context_role']['kind'] == 'dataset_source_or_gene'


@pytest.mark.parametrize('question,relation,owner,property_name', [
    ('What biological-process GO annotations are recorded for CFTR?', 'ASSOCIATED_WITH_GO', 'GO_term', 'go_domain'),
    ('What pancreatic QTL evidence is recorded for ADCY3?', 'PART_OF_QTL_SIGNAL', 'Gene', 'hgnc_symbol'),
    ('How many HPAP donors have stage-3 T1D?', 'HAS_DONOR', 'donor', 'data_source'),
    ('How many single-cell RNA-seq samples are recorded for pancreatic lymph node (PLN)?', 'HAS_SAMPLE', 'Sample_node', 'data_modality'),
])
def test_planner_guidance_retains_all_selected_owned_properties_at_7000_chars(question, relation, owner, property_name):
    from pankagent_vnext.release_schema import REGISTRY
    payload = asyncio.run(ground_question(contextual_graph(), question))
    text = grounding_guidance(payload, max_chars=7000)
    assert len(text) <= 7000 and 'context exceeds' not in text
    data = json.loads(text.split(':\n', 1)[1])
    if 'schema' in data:
        properties = data['schema']['relations'][relation]['properties']
        fields = data['schema']['nodes'][owner]
    else:
        properties = data['relationship_properties'][relation]
        fields = data['node_properties'][owner]
    assert set(properties) == set(REGISTRY['relations'][relation]['properties'])
    assert property_name in fields
    assert 'See complete release registry' not in text


def test_public_go_categories_are_full_scan_cached_and_grounded_to_recorded_spelling():
    graph = contextual_graph()
    graph.rows['GO_term'].append({'id': 'another-go', 'name': 'Test', 'go_domain': 'molecular_function', 'labels': ['GO_term']})
    payload = asyncio.run(ground_question(graph, 'What biological-process GO annotations are recorded for CFTR?'))
    assert payload['schema']['categories']['GO_term.go_domain'] == ['biological_process', 'cellular_component', 'molecular_function']
    assert payload['category_metadata']['GO_term.go_domain']['state'] == 'checked'
    assert len(graph.calls) == len(PUBLIC_CATALOG_LABELS) + 1  # GO values use their existing catalog scan; source categories use one public aggregate read.
    assert any('n.go_domain AS go_domain' in query for query, _ in graph.calls)
    text = grounding_guidance(payload, max_chars=7000)
    assert 'biological_process' in text and 'molecular_function' in text
    role = next(m['context_role'] for m in payload['mentions'] if m['requested'] == 'biological process')
    assert role['canonical_binding']['value'] == 'biological_process'


def test_unavailable_go_category_metadata_never_invents_a_canonical_domain():
    graph = contextual_graph()
    for row in graph.rows['GO_term']:
        row.pop('go_domain')
    payload = asyncio.run(ground_question(graph, 'What biological-process GO annotations are recorded for CFTR?'))
    role = next(m['context_role'] for m in payload['mentions'] if m['requested'] == 'biological process')
    assert role['resolution_state'] == 'metadata_unavailable'
    assert 'canonical_binding' not in role
    assert payload['schema']['categories']['GO_term.go_domain'] == []


def test_annotation_source_inventory_is_complete_and_given_only_for_selected_relation():
    graph=FakeGraph()
    payload=asyncio.run(ground_question(graph,'What Reactome pathways contain CFTR?'))
    assert payload['schema']['categories']['FUNCTION_ANNOTATION.data_source']==['KEGG','Reactome']
    assert payload['category_metadata']['FUNCTION_ANNOTATION.data_source']['complete_scan'] is True
    assert 'Reactome' in grounding_guidance(payload,max_chars=7000)
    assert len([query for query,_ in graph.calls if 'collect(DISTINCT r.data_source)' in query])==1
    unrelated=asyncio.run(ground_question(graph,'Count HPAP donors.'))
    assert 'FUNCTION_ANNOTATION.data_source' not in unrelated['schema']['categories']


@pytest.mark.parametrize('rows',[[],[{'values':['Reactome'], 'record_count':8}],
    [{'values':['Reactome',None], 'record_count':8,'valued_records':8}]])
def test_unavailable_annotation_source_inventory_never_invents_recorded_values(rows):
    graph=FakeGraph();graph.annotation_sources=rows
    payload=asyncio.run(ground_question(graph,'What Reactome pathways contain CFTR?'))
    assert payload['status']=='ready'
    assert payload['schema']['categories']['FUNCTION_ANNOTATION.data_source']==[]
    assert payload['category_metadata']['FUNCTION_ANNOTATION.data_source']['state']=='metadata_unavailable'


@pytest.mark.parametrize('disease',['T1D','T2D'])
def test_disease_associated_genetic_evidence_is_a_relationship_role_not_new_disease_identity(disease):
    payload=asyncio.run(ground_question(FakeGraph(),f'Does the {disease}-associated GWAS signal rs13393590 coloc with ADCY3 QTL?'))
    mention=next(m for m in payload['mentions'] if any(c['entity_type']=='disease' for c in m['candidates']))
    assert mention['state']=='resolved' and mention['identity_complete'] is True
    assert mention['context_role']['kind']=='disease_association'
    assert mention['qualified_surface']==disease+'-associated'


@pytest.mark.parametrize('question',['T1D-stage GWAS evidence','T1D-associated cells','T1D-associated phenotype'])
def test_other_disease_qualifiers_are_not_promoted_to_complete_identity(question):
    payload=asyncio.run(ground_question(FakeGraph(),question))
    mention=next(m for m in payload['mentions'] if any(c['entity_type']=='disease' for c in m['candidates']))
    assert mention.get('identity_complete') is False


def test_gene_context_does_not_promote_grammatical_connectors():
    from pankagent_vnext.preplanning_grounding import _explicit_gene, phrase_tokens
    for word in ("in", "of", "for", "with", "from", "to", "and", "or"):
        words = phrase_tokens("marker gene " + word + " pancreatic cells")
        assert not _explicit_gene(words, 2, 3)
    assert _explicit_gene(phrase_tokens("gene was expressed"), 1, 2)
    assert _explicit_gene(phrase_tokens("symbol in"), 1, 2)
