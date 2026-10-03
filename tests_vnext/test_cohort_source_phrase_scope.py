"""Clinical donor modifiers are not dataset names in trusted sample requests."""
from copy import deepcopy

import pytest

from pankagent_vnext.semantic_registry import RELEASE, STAGES, resolve, _unresolved_source_role


VOCAB = {
    'stages': list(STAGES.values()),
    'sources': ['HPAP', 'StudyA'],
    'donor_sources': ['HPAP', 'StudyA'],
    'sample_sources': ['HPAP'],
    'modalities': ['scRNA-seq'],
    'tissues': [
        {'id': 'UBERON_0015865',
         'name': 'pancreaticosplenic lymph node (proxy for "pancreatic LN")'},
        {'id': 'UBERON_0002106', 'name': 'spleen'},
    ],
    'donor_diseases': [
        {'id': 'CURRENT_T1D', 'name': 'type 1 diabetes', 'synonyms': ['T1D']},
        {'id': 'CURRENT_T2D', 'name': 'type 2 diabetes', 'synonyms': ['T2D']},
    ],
    'inventory_sha256': 'cohort-source-phrase-fixture',
}


def resolved(question, vocabulary=None):
    return resolve({
        'id': 'samples', 'question': question, 'complete': True,
        'depends_on': [], 'relation_types': ['HAS_SAMPLE'], 'constraints': [],
        # The production guard must see the immutable request. Legacy unit
        # tests without this wrapper did not exercise the reported failure.
        'semantic_request': {'source': 'user_request', 'question': question},
    }, deepcopy(VOCAB if vocabulary is None else vocabulary), RELEASE)


def binding(owner, prop, value, operator='='):
    return {'entity_type': owner, 'property': prop, 'operator': operator, 'value': value}


@pytest.mark.parametrize('phrase', [
    'T1D stage 3 HPAP donors',
    'stage 3 T1D HPAP donors',
    'HPAP stage 3 T1D donors',
    'type 1 diabetes stage 3 HPAP donors',
    'HPAP donors with recorded T1D stage 3',
    'T1D stage 3 HPAP donors with recorded T1D stage 3',
])
def test_clinical_source_reordering_preserves_each_verified_filter(phrase):
    result = resolved(f'How many PLN scRNAseq samples from {phrase} available?')
    assert result['semantic_issues'] == []
    assert binding('donor', 'data_source', 'HPAP') in result['constraints']
    assert binding('donor', 't1d_stage', STAGES['3']) in result['constraints']
    assert binding('anatomical_structure', 'id', 'UBERON_0015865') in result['constraints']
    assert binding('Sample_node', 'data_modality', 'scRNA-seq') in result['constraints']
    assert not any(c['entity_type'] == 'disease' for c in result['constraints'])
    assert len(result['request_filter_bindings']) == len(result['constraints'])


@pytest.mark.parametrize('question', [
    'Find samples from T1D stage 3 HPAP donors.',
    'Find samples from stage 3 T1D HPAP donors.',
    'Find samples from T1D HPAP donors.',
])
def test_clinical_from_phrase_does_not_invent_an_unknown_tissue(question):
    result = resolved(question)
    assert result['semantic_issues'] == []
    assert not any(c['entity_type'] == 'anatomical_structure' for c in result['constraints'])


def test_explicit_diagnosis_is_preserved_in_addition_to_recorded_stage():
    result = resolved('Find PLN scRNA-seq samples from HPAP donors diagnosed with T1D who have recorded stage 3.')
    assert result['semantic_issues'] == []
    assert binding('disease', 'id', 'CURRENT_T1D') in result['constraints']
    assert binding('donor', 't1d_stage', STAGES['3']) in result['constraints']


@pytest.mark.parametrize('question', [
    'Find samples from donor source T1D.',
    'Find samples with sample source T1D.',
    'Find samples from source T1D.',
    'Find samples with source=T1D.',
    'Find samples from T1D stage 3 HPAP donors with donor source=T1D.',
    'Find samples with sample source spleen.',
    'Find samples with sample source scRNA-seq.',
])
def test_explicit_source_slots_do_not_accept_clinical_tissue_or_assay_labels(question):
    result = resolved(question)
    assert any('dataset source' in issue for issue in result['semantic_issues'])


@pytest.mark.parametrize('question', [
    'Find samples from UnknownStudy donors.',
    'Find samples from T1D stage 3 UnknownStudy donors.',
    'Find samples from T1D stage 3 UnknownStudy HPAP donors.',
    'Find samples from T1D UnknownClinicalLabel HPAP donors.',
    'Find samples from UnknownClinicalLabel HPAP donors.',
])
def test_known_clinical_prefix_cannot_hide_unknown_source_or_cohort(question):
    result = resolved(question)
    assert result['semantic_issues']
    assert any('dataset source' in issue or 'cohort label' in issue
               for issue in result['semantic_issues'])


def test_unknown_tissue_still_blocks_a_valid_clinical_cohort():
    result = resolved('Find samples from T1D stage 3 HPAP donors with tissue UnknownTissue.')
    assert any('sample tissue' in issue for issue in result['semantic_issues'])


def test_repeated_source_name_retains_separate_donor_and_sample_ownership():
    result = resolved('Find samples from T1D stage 3 HPAP donors; the sample source is HPAP.')
    assert result['semantic_issues'] == []
    assert binding('Sample_node', 'data_source', 'HPAP') in result['constraints']
    assert binding('donor', 'data_source', 'HPAP') in result['constraints']


def test_known_donor_source_is_not_assumed_to_be_a_valid_sample_source():
    result = resolved('Find samples with sample source StudyA.')
    assert any('donor/sample owner' in issue for issue in result['semantic_issues'])


def test_negative_source_is_retained_after_clinical_from_phrase():
    result = resolved('Find samples from T1D stage 3 donors excluding HPAP.')
    assert result['semantic_issues'] == []
    assert binding('donor', 'data_source', 'HPAP', '!=') in result['constraints']
    assert binding('donor', 't1d_stage', STAGES['3']) in result['constraints']


def test_conflicting_repeated_clinical_mentions_still_require_repair():
    result = resolved('Find samples from T1D HPAP donors but not T1D donors.')
    assert result['semantic_issues']
    assert not any(c['entity_type'] == 'disease' for c in result['constraints'])


def test_unverified_stage_cannot_authorize_a_clinical_source_exception():
    vocabulary = deepcopy(VOCAB)
    vocabulary['stages'] = []
    result = resolved('Find samples from T1D stage 3 HPAP donors.', vocabulary)
    assert result['semantic_issues']
    assert not any(c['property'] == 't1d_stage' for c in result['constraints'])


def test_general_source_description_is_not_a_source_filter():
    result = resolved('Find samples from T1D stage 3 HPAP donors and report source information.')
    assert result['semantic_issues'] == []
    assert binding('donor', 'data_source', 'HPAP') in result['constraints']


@pytest.mark.parametrize('question', [
    'Explain the source of the evidence for CFTR.',
    'Which data source is recorded for these samples?',
    'What sample source is recorded for these samples?',
    'Give me an introduction to the sample sources in PanKgraph.',
    'Count donors from the HPAP data source regardless of recorded T1D stage.',
])
def test_source_explanations_and_recorded_field_questions_are_not_filter_values(question):
    assert not _unresolved_source_role(question, VOCAB)
    assert not any('dataset source' in issue
                   for issue in resolved(question).get('semantic_issues', []))


def test_multiword_explicit_sample_source_uses_complete_recorded_value():
    vocabulary = deepcopy(VOCAB)
    vocabulary['sources'].append('Kaestner Lab_Upenn')
    vocabulary['sample_sources'].append('Kaestner Lab_Upenn')
    result = resolved('Find samples; sample source is Kaestner Lab_Upenn.', vocabulary)
    assert result['semantic_issues'] == []
    assert binding('Sample_node', 'data_source', 'Kaestner Lab_Upenn') in result['constraints']
    unknown = resolved('Find samples; sample source is Kaestner Lab_Unknown.', vocabulary)
    assert any('dataset source' in issue for issue in unknown['semantic_issues'])


def test_tissue_description_mask_preserves_verified_clinical_phrase_offsets():
    result = resolved('Which tissues are available? Find samples from T1D stage 3 HPAP donors.')
    assert not any('dataset source' in issue or 'sample tissue' in issue
                   for issue in result['semantic_issues'])
    assert binding('donor', 'data_source', 'HPAP') in result['constraints']
    assert binding('donor', 't1d_stage', STAGES['3']) in result['constraints']
