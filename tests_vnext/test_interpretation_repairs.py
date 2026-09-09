import hashlib
import json
from pathlib import Path

from pankagent_vnext.answer_router import AnswerSkillRouter

BUNDLE = Path(__file__).resolve().parents[1] / 'pankagent_vnext/answer_skills'


def test_active_bundle_preserves_original_sources_and_validates_all_checksums():
    router = AnswerSkillRouter(BUNDLE)
    assert router.manifest['bundle_version'] == '1.6.0'
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
