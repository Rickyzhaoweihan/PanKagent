"""Tissue-role repair changes the binding, never the requested biological set."""
from copy import deepcopy
import json

import pytest

from pankagent_vnext.planning_compile import compile_property_owners
from pankagent_vnext.planning_scope import scope_issue
from test_planning_compile import context, field, plan


SPLEEN = ('anatomical_structure', 'UBERON_0002106', 'spleen')
PANCREAS = ('anatomical_structure', 'UBERON_0001264', 'pancreas')


@pytest.mark.parametrize('tissue', [SPLEEN, PANCREAS])
def test_independent_assay_cohorts_keep_tissue_and_all_other_owned_filters(tissue):
    question = ('For HPAP donors with recorded stage 3 and ' + tissue[2] + ' samples, '
                'count BCR-seq and TCR-seq donors separately; do not require both assays.')
    steps = []
    for index, assay in enumerate(['BCR-seq', 'TCR-seq']):
        step = plan(field('anatomical_structure', tissue[1], 'Sample_node'),
                    field('data_source', 'HPAP', 'donor'),
                    field('t1d_stage', 'Stage 3', 'donor'),
                    field('data_modality', assay, 'Sample_node'))['steps'][0]
        step.update(id='s' + str(index), depends_on=[], complete=True)
        step['relation_types'].append('HAS_DONOR')
        steps.append(step)
    original = {'steps': steps, 'clarification': None}
    untouched = deepcopy(original)
    grounded = context(tissue)
    assert scope_issue(question, grounded, original).startswith('missing_requested_scope:tissue:')
    compiled, issue = compile_property_owners(original, grounded, question=question)
    assert issue is None and original == untouched
    assert scope_issue(question, grounded, compiled) is None
    for index, step in enumerate(compiled['steps']):
        assert step['depends_on'] == []
        assert step['constraints'][0] == field('id', tissue[1], 'anatomical_structure', owner_kind='node')
        assert [(c['entity_type'], c['property'], c['value']) for c in step['constraints'][1:]] == [
            ('donor', 'data_source', 'HPAP'), ('donor', 't1d_stage', 'Stage 3'),
            ('Sample_node', 'data_modality', ['BCR-seq', 'TCR-seq'][index])]
        assert step['constraint_compilation'][0]['requested'] == original['steps'][index]['constraints'][0]
        assert step['constraint_compilation'][0]['canonical_binding'] == step['constraints'][0]


@pytest.mark.parametrize('operator,value,expected', [
    ('=', SPLEEN[1], SPLEEN[1]), ('=', 'spleen', SPLEEN[1]),
    ('!=', SPLEEN[1], SPLEEN[1]), ('<>', 'spleen', SPLEEN[1]),
    ('IN', [SPLEEN[1], PANCREAS[1]], [SPLEEN[1], PANCREAS[1]]),
    ('NOT IN', '["spleen", "pancreas"]', [SPLEEN[1], PANCREAS[1]]),
])
def test_identity_predicates_retain_operator_list_shape_and_provenance(operator, value, expected):
    raw = plan(field('anatomical_structure', value, 'Sample_node', operator))
    compiled, issue = compile_property_owners(raw, context(SPLEEN, PANCREAS),
        question='Check spleen and pancreas samples with the requested exclusions.')
    assert issue is None
    result = compiled['steps'][0]['constraints'][0]
    assert result == field('id', expected, 'anatomical_structure', operator, owner_kind='node')
    assert compiled['steps'][0]['constraint_compilation'][0]['requested'] == raw['steps'][0]['constraints'][0]


@pytest.mark.parametrize('question', [
    'Find samples where Sample_node.anatomical_structure equals UBERON_0002106.',
    'Find spleen samples with raw anatomical_structure equal to UBERON_0002106.',
    'Check the sample metadata anatomical_structure property for spleen.',
    'Filter spleen samples by the anatomical_structure column.',
])
def test_explicit_raw_metadata_field_request_remains_literal(question):
    raw = plan(field('anatomical_structure', SPLEEN[1], 'Sample_node'))
    result, issue = compile_property_owners(raw, context(SPLEEN), question=question)
    assert issue is None
    assert result['steps'][0]['constraints'][0] == field(
        'anatomical_structure', SPLEEN[1], 'Sample_node', owner_kind='node')


@pytest.mark.parametrize('question', [None, 'Show spleen evidence.',
    'Find pancreas samples.', 'Show gene expression in spleen.', 'Show spleen disease diagnoses for donors.'])
def test_generated_step_cannot_invent_the_original_sample_tissue_role(question):
    raw = plan(field('anatomical_structure', SPLEEN[1], 'Sample_node'))
    raw['steps'][0]['question'] = 'Find spleen samples.'
    result, issue = compile_property_owners(raw, context(SPLEEN), question=question)
    assert issue is None
    assert result['steps'][0]['constraints'][0]['entity_type'] == 'Sample_node'


def test_donor_assay_phrase_supplies_sample_role_without_inventing_a_new_donor_filter():
    raw = plan(field('anatomical_structure', SPLEEN[1], 'Sample_node'))
    result, issue = compile_property_owners(raw, context(SPLEEN), question='Find spleen BCR-seq donors.')
    assert issue is None
    assert result['steps'][0]['constraints'] == [field('id', SPLEEN[1], 'anatomical_structure', owner_kind='node')]


@pytest.mark.parametrize('mutation', ['ambiguous', 'incomplete', 'missing', 'unrecorded'])
def test_unverified_tissue_identity_is_not_repaired(mutation):
    grounded = context(SPLEEN)
    raw = plan(field('anatomical_structure', SPLEEN[1], 'Sample_node'))
    if mutation == 'ambiguous':
        grounded['mentions'][0]['candidates'].append({'entity_type': 'anatomical_structure', 'id': 'another_tissue', 'name': 'spleen'})
    elif mutation == 'incomplete':
        grounded['mentions'][0]['identity_complete'] = False
    elif mutation == 'missing':
        grounded['mentions'] = []
    else:
        raw['steps'][0]['constraints'][0]['value'] = 'UNRECORDED_ID'
    result, issue = compile_property_owners(raw, grounded, question='Find spleen samples.')
    assert issue is None
    assert result['steps'][0]['constraints'][0]['entity_type'] == 'Sample_node'


@pytest.mark.parametrize('operator', ['CONTAINS', 'STARTS WITH', 'ENDS WITH'])
def test_text_matching_operators_do_not_become_ontology_identity_checks(operator):
    raw = plan(field('anatomical_structure', 'spleen', 'Sample_node', operator))
    result, issue = compile_property_owners(raw, context(SPLEEN), question='Find spleen samples.')
    assert issue is None
    assert result['steps'][0]['constraints'][0]['entity_type'] == 'Sample_node'
    assert result['steps'][0]['constraints'][0]['operator'] == operator


def test_repair_is_idempotent_and_other_relationship_families_do_not_acquire_it():
    raw = plan(field('anatomical_structure', SPLEEN[1], 'Sample_node'))
    first, issue = compile_property_owners(raw, context(SPLEEN), question='Find spleen samples.')
    second, issue2 = compile_property_owners(first, context(SPLEEN), question='Find spleen samples.')
    assert first == second and issue is issue2 is None
    raw['steps'][0]['relation_types'] = ['HAS_DONOR']
    result, issue = compile_property_owners(raw, context(SPLEEN), question='Find spleen samples.')
    assert result['steps'][0]['constraints'][0]['entity_type'] == 'Sample_node'
