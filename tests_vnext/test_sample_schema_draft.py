"""Raw assay roles remain exact without depending on LLM plan wording."""
import asyncio
from copy import deepcopy

import pytest

from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.schema_drafting import compile_schema_draft
from test_pattern_planning import grounded
from test_planning_compiler_gateway import gateway_for
from test_sample_scope_recovery import VOCAB


def sample_grounding(question):
    value = grounded(question)
    value['sample_terminology'] = dict(deepcopy(VOCAB), inventory_complete=True)
    return value


def compile_question(question):
    return compile_schema_draft(question, sample_grounding(question))


def fields(step, prop):
    return [(c['entity_type'], c.get('operator', '='), c['value'])
            for c in step['constraints'] if c['property'] == prop]


@pytest.mark.parametrize('tissue,identifier', [('spleen', 'UBERON_0002106'), ('pancreas', 'UBERON_0001264')])
@pytest.mark.parametrize('assay', ['scRNA-seq', 'snRNA-seq'])
def test_named_standalone_assay_is_sample_witness_and_exclusion_is_not_donor_anti_join(tissue, identifier, assay):
    question = f'Find HPAP donors with {tissue} standalone {assay} only. Exclude multiome.'
    data = sample_grounding(question)
    original = deepcopy(data)
    result = compile_schema_draft(question, data)
    assert data == original and result['interpreted_question'] == question
    assert result['planning_route']['claude_calls'] == 0 and len(result['steps']) == 1
    step = result['steps'][0]
    assert step['relation_types'] == ['HAS_SAMPLE'] and not step['depends_on']
    assert set(fields(step, 'data_modality')) == {
        ('Sample_node', '=', assay), ('Sample_node', '!=', 'snMultiomics')}
    assert fields(step, 'data_source') == [('donor', '=', 'HPAP')]
    assert fields(step, 'id') == [('anatomical_structure', '=', identifier)]
    assert step['sample_requirements']['modality_groups'] == [[assay]]
    assert not step['sample_requirements']['separate_bindings']
    assert step['semantic_request']['question'] == question
    query = (f'MATCH (d:donor)-[r:HAS_SAMPLE]->(s:Sample_node)<-[tr:HAS_SAMPLE]-(t:anatomical_structure) '
             f'WHERE d.data_source="HPAP" AND t.id="{identifier}" '
             f'AND s.data_modality="{assay}" AND s.data_modality<>"snMultiomics" RETURN d,r,s,tr,t')
    assert validate_cypher(query, step) == []
    assert validate_cypher(query.replace('s.data_modality<>', 'd.data_modality<>'), step)
    assert validate_cypher(query.replace(f'AND s.data_modality="{assay}" ', ''), step)


@pytest.mark.parametrize('source,assay', [('StudyA', 'BCR-seq'), ('HPAP', 'TCR-seq')])
def test_recorded_non_rna_assay_also_supplies_sample_without_sample_noun(source, assay):
    result = compile_question(f'Find {source} donors with pancreas {assay}.')
    assert result['steps'][0]['relation_types'] == ['HAS_SAMPLE']
    assert fields(result['steps'][0], 'data_modality') == [('Sample_node', '=', assay)]
    assert fields(result['steps'][0], 'data_source') == [('donor', '=', source)]


@pytest.mark.parametrize('question', [
    'Find HPAP donors with spleen scRNA-seq only.',
    'Find HPAP donors without multiome.',
    'Find HPAP donors with spleen standalone scRNA-seq. Exclude donors with multiome.',
    'Find HPAP donors who do not have multiome but have spleen scRNA-seq.',
    'Find HPAP donors with spleen BCR unknown seq.',
    'Find HPAP donors with spleen scRNA-seq and TCR-seq.',
    'Find HPAP and StudyA donors with spleen BCR-seq.',
    'Find HPAP donors with spleen standalone scRNA-seq and age above 30.',
])
def test_ambiguous_or_unrecognized_donor_and_assay_roles_fall_back(question):
    assert compile_question(question) is None


def test_unverified_assay_and_incomplete_inventory_do_not_compile():
    question = 'Find HPAP donors with spleen standalone scRNA-seq only. Exclude multiome.'
    for missing in ['inventory_complete', 'scRNA-seq']:
        data = sample_grounding(question)
        if missing == 'inventory_complete':
            data['sample_terminology']['inventory_complete'] = False
        else:
            data['sample_terminology']['modalities'].remove(missing)
        assert compile_schema_draft(question, data) is None


@pytest.mark.parametrize('assays', ['BCR-seq and TCR-seq', 'BCR-seq or TCR-seq',
    'standalone scRNA-seq and multiome', 'RNA and ATAC'])
def test_donor_sample_noun_does_not_turn_multiple_assays_into_implicit_union(assays):
    question = f'Find HPAP donors with spleen {assays} samples.'
    assert compile_question(question) is None


def test_multi_assay_or_question_reaches_general_planner_instead_of_typed_union_draft():
    question = 'Find HPAP donors with spleen BCR-seq or TCR-seq samples.'
    data = sample_grounding(question)
    async def check():
        gateway, calls = gateway_for(lambda _: {})
        result = await gateway.plan(question, [], grounding=data)
        assert calls and result.get('proposal_issue')
        assert result.get('planning_route', {}).get('kind') != 'verified_schema_pattern'
        assert not result.get('steps')
    asyncio.run(check())


@pytest.mark.parametrize('source,tissue,assays', [
    ('HPAP', 'spleen', ('BCR-seq', 'TCR-seq')),
    ('StudyA', 'pancreas', ('scRNA-seq', 'snRNA-seq')),
    ('HPAP', 'pancreas', ('scRNA-seq', 'snMultiomics')),
])
def test_explicit_separate_donor_counts_keep_two_exact_independent_assay_checks(source, tissue, assays):
    question = (f'For {source} donors with recorded stage-3 T1D and {tissue} samples, '
        f'compare the number with {assays[0]} against the number with {assays[1]}. '
        'Count donors separately for each assay; do not require both assays.')
    result = compile_question(question)
    assert result is not None and len(result['steps']) == 2
    assert result['interpreted_question'] == question and result['planning_route']['claude_calls'] == 0
    for step, assay in zip(result['steps'], assays):
        assert not step['depends_on'] and step['evidence_combination'] == 'independent'
        assert step['relation_types'] == ['HAS_SAMPLE']
        assert fields(step, 'data_source') == [('donor', '=', source)]
        assert fields(step, 't1d_stage') == [('donor', '=', VOCAB['stages'][2])]
        assert fields(step, 'id') == [('anatomical_structure', '=', 'UBERON_0002106' if tissue == 'spleen' else 'UBERON_0001264')]
        assert fields(step, 'data_modality') == [('Sample_node', '=', assay)]
        assert step['sample_requirements']['modality_groups'] == [[assay]]
        assert not step['sample_requirements']['paired'] and not step['sample_requirements']['separate_bindings']
        assert step['schema_draft_compilation']['requested'] == question
        assert step['semantic_request']['question'] == question


@pytest.mark.parametrize('main,suffix', [
    ('Compare HPAP spleen BCR-seq and TCR-seq donor counts.', 'Require both assays.'),
    ('Compare HPAP spleen BCR-seq and TCR-seq donor counts.', ''),
    ('Compare HPAP spleen BCR-seq and TCR-seq donor counts.', 'Count donors separately for each assay; require both assays.'),
    ('Compare HPAP spleen BCR-seq and TCR-seq donor counts.', 'Count donors separately for each assay; do not require both assays, only females.'),
    ('Compare HPAP spleen BCR-seq and TCR-seq donor counts excluding multiome.', 'Count donors separately for each assay.'),
    ('Compare HPAP spleen BCR-seq and TCR-seq donor counts with PIP above 0.5.', 'Count donors separately for each assay.'),
    ('Compare HPAP spleen BCR-seq and StudyA pancreas TCR-seq donor counts.', 'Count donors separately for each assay.'),
    ('Compare HPAP spleen BCR-seq and TCR-seq donor counts in the same donors.', 'Count donors separately for each assay.'),
])
def test_independent_count_route_does_not_accept_intersection_exclusion_or_mixed_scope(main, suffix):
    assert compile_question(main + ' ' + suffix) is None


@pytest.mark.parametrize('question', [
    'Find HPAP donors with spleen standalone scRNA-seq only. Exclude multiome.',
    'For HPAP donors with recorded stage-3 T1D and spleen samples, compare the number with BCR-seq against the number with TCR-seq. Count donors separately for each assay; do not require both assays.',
])
def test_gateway_accepts_scoped_draft_without_provider_call(question):
    data = sample_grounding(question)
    async def check():
        gateway, calls = gateway_for(lambda _: {})
        result = await gateway.plan(question, [], grounding=data)
        assert not calls and not result.get('proposal_issue') and result['steps']
        assert result['planning_route']['claude_calls'] == 0
    asyncio.run(check())
