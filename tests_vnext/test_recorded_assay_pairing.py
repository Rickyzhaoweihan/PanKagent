"""Literal recorded assay names do not create an extra pairing requirement."""
from copy import deepcopy

import pytest

from pankagent_vnext.query_templates import compile_query
from pankagent_vnext.semantic_registry import (
    _assay_intent_values, _only_recorded_assay_intent, resolve,
)
from test_semantic_registry import prepared, VOCAB
from test_template_path_normalization import proposal, prepared as proved_step
from pankagent_vnext.query_templates import normalize_template_paths


@pytest.mark.parametrize('label', ['snMultiomics', 'snmultiomics', 'multiome', 'multiomics'])
def test_verified_single_assay_name_and_alias_are_literal_not_paired(label):
    question = f'How many PLN {label} samples from T1D stage 3 HPAP donors available?'
    vocabulary = {**deepcopy(VOCAB), 'assay_donor_sources': {'snMultiomics': ['HPAP']}}
    step = resolve({'question': question, 'constraints': [], 'relation_types': ['HAS_SAMPLE']},
                   vocabulary, 'PanKgraph_08_04')
    assert not step['semantic_issues']
    assert step['sample_requirements']['modality_groups'] == [['snMultiomics']]
    assert step['sample_requirements']['paired'] is False
    assert step['sample_requirements']['separate_bindings'] is False
    assert step['sample_requirements']['capability_scope_verified'] is False
    assert _assay_intent_values(question, vocabulary['modalities']) == ({'snMultiomics'}, set(), True)


@pytest.mark.parametrize('wording', [
    'paired RNA and ATAC multiome data',
    'joint RNA and ATAC snMultiomics samples',
    'snMultiomics samples with RNA and ATAC components',
    'RNA and ATAC multiome data',
])
def test_actual_pairing_and_component_requests_keep_paired_requirement(wording):
    question = 'Find HPAP stage 3 T1D donors with spleen ' + wording
    step = prepared(question)
    assert step['sample_requirements']['paired'] is True
    assert step['sample_requirements']['modality_groups'] == [['snMultiomics']]
    assert not _only_recorded_assay_intent(question, VOCAB['modalities'])
    # Genuine pairing must keep its strict scope instead of being converted
    # to the ordinary single-assay template.
    strict = proved_step(proposal()['steps'][0])
    strict['sample_requirements'] = step['sample_requirements']
    assert compile_query(strict) is None


def test_separate_assays_and_explicit_capability_expansion_remain_distinct():
    separate = prepared('Find HPAP stage 3 T1D donors with spleen RNA and ATAC data')
    assert separate['sample_requirements']['separate_bindings'] is True
    assert len(separate['sample_requirements']['modality_groups']) == 2
    broad = prepared('Find HPAP stage 3 T1D donors with spleen RNA data including multiome')
    assert broad['sample_requirements']['modality_groups'] == [['scRNA-seq', 'snMultiomics']]
    assert broad['sample_requirements']['paired'] is False
    assert broad['sample_requirements']['capability_scope_verified'] is True


def test_unknown_assay_and_unverified_alias_are_not_treated_as_recorded_literals():
    assert not _only_recorded_assay_intent('Find snMultiomics samples', ['scRNA-seq'])
    assert not _only_recorded_assay_intent('Find snMutiomics samples', VOCAB['modalities'])
    assert not _only_recorded_assay_intent('Find scRNA-seq with multiomics', ['scRNA-seq'])


def test_resolved_literal_multiomics_keeps_registered_same_sample_topology():
    question = 'How many PLN snMultiomics samples from T1D stage 3 HPAP donors available?'
    plan = proposal()
    plan['steps'][0]['question'] = question
    plan['steps'][0]['constraints'][2]['value'] = 'snMultiomics'
    normalized = normalize_template_paths(question, plan)['steps'][0]
    ready = proved_step(normalized)
    semantic = resolve({'question': question, 'constraints': [], 'relation_types': ['HAS_SAMPLE']},
                       deepcopy(VOCAB), 'PanKgraph_08_04')
    ready['sample_requirements'] = semantic['sample_requirements']
    compiled = compile_query(ready)
    assert compiled['template_id'] == 'donor_tissue_same_sample_records'
    assert compiled['parameters']['template_2'] == 'snMultiomics'
