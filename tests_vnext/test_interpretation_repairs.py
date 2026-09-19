import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from pankagent_vnext.answer_router import AnswerSkillRouter

BUNDLE = Path(__file__).resolve().parents[1] / 'pankagent_vnext/answer_skills'


def test_active_bundle_preserves_original_sources_and_validates_all_checksums():
    router = AnswerSkillRouter(BUNDLE)
    assert router.manifest['bundle_version'] == '1.9.2'
    original = {'sha256': {'upstream/schema_skill.json': 'f5e817a481c45cd2f59f9a8634a26cbea03e4caee24f8fcaa8d2c951dbe37ac3', 'upstream/functional_data_interpretation_skill.json': 'd5abc065f9990bcb07be5c189c84d54167195f2d72abe18adb96d99d84218b80', 'upstream/general_interpretation.json': '04cbefac55095f2fa652c8ce2c0ad2ee89b09400268bdab81277eb3c32c92668'}}
    for name, digest in router.manifest['sha256'].items():
        assert hashlib.sha256((BUNDLE / name).read_bytes()).hexdigest() == digest
        if name.startswith('upstream/'):
            assert digest == original['sha256'][name]


def test_recorded_stage_route_has_no_diagnostic_definitions_or_forced_clinical_tables():
    guidance = AnswerSkillRouter(BUNDLE).select([{'nodes': [{'id': 'synthetic-donor', 'labels': ['donor'], 'properties': {'t1d_stage': 'Stage 3: recorded'}}]}]).guidance
    assert 'Report t1d_stage as recorded metadata' in guidance
    assert 'sufficient evidence' not in guidance
    assert 'Do not add individual antibody, HbA1c, C-peptide' in guidance
    assert 'not evidence that the Neo4j record lacks metadata' in guidance


def test_go_evidence_categories_and_formal_names_survive_selection():
    glossary = json.loads((BUNDLE / 'bim/evidence_glossary.json').read_text())['terms']
    assert glossary['IBA'].startswith('Inferred from Biological aspect of Ancestor:')
    assert glossary['TAS'].startswith('Traceable Author Statement:')
    assert 'not a direct-assay code' in glossary['TAS']
    assert 'does not establish that experiments are absent' in glossary['TAS']
    assert 'without individual manual review' in glossary['IEA']
    guidance = AnswerSkillRouter(BUNDLE).select([{'nodes': [{'id': 'GO:synthetic', 'labels': ['GO_term'], 'properties': {}}], 'edges': [{'type': 'ASSOCIATED_WITH_GO', 'properties': {'go_evidence_code': 'IBA'}}]}]).guidance
    assert 'TAS is an author statement' in guidance
    assert 'Count unique GO terms separately from annotation records' in guidance


def test_qtl_guidance_separates_indexed_records_from_signal_width_and_molecular_trait():
    guidance = AnswerSkillRouter(BUNDLE).select([{'edges': [{'type': 'PART_OF_QTL_SIGNAL', 'properties': {'n_snp': 78, 'pip': .135}}]}]).guidance
    assert 'not the number of variants in the underlying credible set' in guidance
    assert 'Tissue scopes the molecular association' in guidance
    assert 'expression/eQTL is explicitly recorded' in guidance
    assert 'One PIP does not establish the other credible-set probabilities' in guidance


def test_saved_qtl_negative_claims_have_source_specific_prompt_boundaries(monkeypatch, tmp_path):
    """Verify real preparation supplies the fix; no model reasoning is simulated."""
    from tests_vnext.test_answer_synthesis import gateway_with_mock

    fixture = json.loads((Path(__file__).with_name('fixtures') / 'qtl_saved_answer_scope.json').read_text())
    edges = fixture['edges']
    identifiers = {edge['properties']['credible_set'] for edge in edges}
    # Three full record IDs, but only two short suffixes; shortening them lost
    # source/phenotype identity in the saved answer's "unnamed set" table.
    assert len(identifiers) == 3
    assert len({value.rsplit('__', 1)[-1] for value in identifiers}) == 2
    assert {edge['properties']['data_source'] for edge in edges} == {'exon; INSPIRE', 'INSPIRE; SusieR'}
    assert all(not set(edge['properties']) & {'qtl_type', 'molecular_phenotype', 'phenotype'} for edge in edges)
    assert set(fixture['negative_answer_excerpts']) == {'phenotype', 'shared_signal', 'unnamed'}
    nodes = [{'id': identifier, 'labels': ['variants'], 'properties': {'id': identifier}}
             for identifier in sorted({edge['start_id'] for edge in edges})]
    nodes.append({'id': edges[0]['end_id'], 'labels': ['Gene'],
                  'properties': {'id': edges[0]['end_id'], 'name': 'PLEKHM1'}})
    evidence = {'s1': {'step_id': 's1', 'status': 'complete', 'graph_version': 'PanKgraph_08_04',
                       'nodes': nodes, 'edges': deepcopy(edges), 'rows': [], 'truncated': False}}
    original = deepcopy(evidence)

    async def check():
        gateway, fake, _ = gateway_with_mock(monkeypatch, tmp_path, [])
        try:
            prepared = gateway.prepare_answer(fixture['question'], evidence)
            matched = next(block['text'] for block in prepared.system if '[edge.part_of_qtl_signal]' in block['text'])
            assert 'A data_source label such as exon is a source annotation, not a verified molecular QTL class' in matched
            assert 'Do not infer splicing, sQTL or a splicing-related phenotype from exon alone, even as a suggestion' in matched
            assert 'not statistically independent signals' in matched
            assert 'Do not conclude not one shared signal solely because those identifiers differ' in matched
            assert 'do not call a set unnamed when a nonempty full identifier is recorded' in matched
            assert 'Repeated short suffixes such as credibleSet1' in matched
            body = json.loads(prepared.body)
            assert body['evidence'][0]['edges'] == edges
            assert prepared.profile['bundle_version'] == '1.9.2'
            assert prepared.profile['model_context']['mode'] == 'standard'
            assert evidence == original
            assert gateway.budget.snapshot()['calls'] == 0
            assert fake.stream_calls == fake.create_calls == []
            # Exercise the real size fallback with this same QTL evidence;
            # lowering the test-only threshold avoids a synthetic large graph.
            monkeypatch.setattr('pankagent_vnext.evidence_context.TARGET_BYTES', 1)
            limited = gateway.prepare_answer(fixture['question'], evidence)
            assert limited.profile['model_context']['mode'] == 'node_identity_only'
            assert limited.profile['model_context']['measurement_guidance_suppressed'] is True
            assert all('[edge.part_of_qtl_signal]' not in block['text'] for block in limited.system)
            assert all('splicing-related phenotype' not in block['text'] for block in limited.system)
            assert all('credible_set' not in block['text'] for block in limited.system)
            assert all('edges' not in step for step in json.loads(limited.body)['evidence'])
            assert evidence == original and fake.stream_calls == fake.create_calls == []
        finally:
            await gateway.close()
    asyncio.run(check())
