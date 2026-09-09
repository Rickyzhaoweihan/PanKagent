import asyncio
from copy import deepcopy

import pytest

from pankagent_vnext.coloc_scope import compile_comparisons, normalize_plan, summarize_linkage, RELEASE
from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.query_templates import compile_query
from test_coloc_computed_operations import source_plan
from test_coloc_scope import evidence, GENE, DISEASE
from test_graph import FakeAdapter


def mixed_plan(question=None):
    value = source_plan()
    for step in value['steps'][:3]:
        for entity in step['resolved_entities']:
            entity['labels'] = [entity['entity_type']]
    primary, gwas, qtl = value['steps'][:3]
    primary.update(depends_on=[gwas['id'], qtl['id']], evidence_combination='cooccurrence',
        question=question or 'Does gene ADCY3 have a recorded signal colocalization (SIGNAL_COLOC_WITH) '
        'with type 1 diabetes, and does its recorded gwas_signal_id/qtl_signal_id match the signals '
        'found for rs13393590 in steps s1_gwas and s1_qtl?')
    value['steps'] = [gwas, qtl, primary]
    value['display_groups'][0]['step_ids'] = ['s1_gwas', 's1_qtl', 's1']
    return value


@pytest.mark.parametrize('question', [None,
    'Does gene ADCY3 have a SIGNAL_COLOC_WITH relationship to type 1 diabetes, and does the recorded '
    'gwas_signal_id/qtl_signal_id match the signals identified in the prior steps?'])
def test_mixed_presence_and_comparison_preserves_primary_and_computes_linkage(question):
    original = mixed_plan(question)
    snapshot = deepcopy(original)
    actual = compile_comparisons(original, RELEASE)
    assert original == snapshot
    primary = actual['steps'][-1]
    assert primary['id'] == 's1' and primary['depends_on'] == []
    assert primary['constraints'] == original['steps'][-1]['constraints']
    assert primary['evidence_combination'] == 'independent'
    assert primary['complete'] is True
    assert 'resolution_key' not in primary
    operation = actual['computed_operations'][0]
    assert operation['id'] == 's1_compare'
    assert operation['depends_on'] == ['s1', 's1_gwas', 's1_qtl']
    assert operation['original_step'] == original['steps'][-1]
    assert actual['coloc_comparison_normalization']['original_steps'] == original['steps']
    assert actual['coloc_comparison_normalization']['rewritten_step_ids'] == ['s1']
    assert actual['display_groups'][0]['step_ids'] == ['s1_gwas', 's1_qtl', 's1']
    assert actual['display_groups'][0]['computed_operation_ids'] == ['s1_compare']
    assert all(compile_query(step) for step in actual['steps'])
    for step in actual['steps']:
        template = compile_query(step)
        assert validate_cypher(template['cypher'], step, template['parameters']) == []
    summary = summarize_linkage(actual, evidence())
    assert summary['groups'][0]['primary_record_count'] == 2
    assert summary['computed_operations'][0]['status'] == 'complete'
    assert compile_comparisons(actual, RELEASE) == actual
    assert compile_comparisons(normalize_plan(actual, RELEASE), RELEASE) == actual


@pytest.mark.parametrize('suffix', [' Only return matching signals.', ' In pancreas.',
    ' Require pp_h4_abf > 0.9.', ' Use the highest score.', ' Exclude GTEx.',
    ' Find additional variants.', ' Count the donor cohort.', ' Prove causality.'])
def test_mixed_check_never_drops_unknown_or_filtering_clause(suffix):
    original = mixed_plan()
    original['steps'][-1]['question'] += suffix
    assert compile_comparisons(original, RELEASE) == original


@pytest.mark.parametrize('mutation', ['extra_constraint', 'different_gene', 'unresolved_parent',
    'partial_parent', 'partial_primary', 'duplicate_parent', 'downstream', 'id_collision',
    'wrong_disease', 'unknown_release', 'ambiguous_labels'])
def test_only_verified_matching_pair_of_parent_scopes_can_be_split(mutation):
    original = mixed_plan()
    if mutation == 'extra_constraint':
        original['steps'][-1]['constraints'].append({'property': 'pp_h4_abf', 'operator': '>', 'value': .9})
    elif mutation == 'different_gene':
        original['steps'][1]['resolved_entities'][-1]['id'] = 'ENSG_OTHER'
    elif mutation == 'unresolved_parent':
        original['steps'][0]['resolved_entities'][0]['state'] = 'ambiguous'
    elif mutation == 'partial_parent':
        original['steps'][0]['complete'] = False
    elif mutation == 'partial_primary':
        original['steps'][-1]['complete'] = False
    elif mutation == 'duplicate_parent':
        extra = deepcopy(original['steps'][0]); extra['id'] = 'other'; original['steps'].insert(0, extra)
    elif mutation == 'downstream':
        original['steps'].append({'id': 'later', 'depends_on': ['s1']})
    elif mutation == 'id_collision':
        original['steps'].insert(0, {'id': 's1_compare', 'depends_on': []})
    elif mutation == 'wrong_disease':
        original['steps'][0]['constraints'][1]['value'] = 'MONDO_OTHER'
    elif mutation == 'ambiguous_labels':
        original['steps'][-1]['resolved_entities'][0]['state'] = 'ambiguous'
    assert compile_comparisons(original, 'other' if mutation == 'unknown_release' else RELEASE) == original


def test_failed_supporting_check_does_not_erase_primary_coloc_evidence():
    actual = compile_comparisons(mixed_plan(), RELEASE)
    prior = evidence(); prior['s1_qtl']['status'] = 'failed'
    summary = summarize_linkage(actual, prior)
    assert summary['groups'][0]['primary_record_count'] == 2
    assert summary['computed_operations'][0]['status'] == 'blocked'
    assert all(record['gwas_membership_verified'] for record in summary['groups'][0]['records'])


def test_prepare_plan_resigns_modified_primary_and_uses_template_without_models():
    async def check():
        class ResolvedAdapter(FakeAdapter):
            async def _resolve_constraint(self, constraint, index, step):
                return {'constraint_index': index, 'requested': deepcopy(constraint), 'state': 'resolved',
                    'graph_version': RELEASE, 'entity_type': constraint['entity_type'],
                    'labels': [constraint['entity_type']], 'id': constraint['value'],
                    'name': {GENE: 'ADCY3', DISEASE: 'type 1 diabetes'}.get(constraint['value'], constraint['value'])}
        graph = ResolvedAdapter([])
        graph.settings.graph_version = RELEASE
        graph.settings.grounded_query_policy = True
        graph.release_relations = {'SIGNAL_COLOC_WITH', 'PART_OF_GWAS_SIGNAL', 'PART_OF_QTL_SIGNAL'}
        async def emit(*args):
            pass
        actual = await graph.prepare_plan(mixed_plan(), emit)
        assert all(graph._resolution_verified(step) for step in actual['steps'])
        assert actual['steps'][-1]['depends_on'] == []
        result = await graph.execute(actual['steps'][-1], {}, emit)
        assert result['status'] == 'complete' and result['query_route'] == 'template'
        assert not graph.generated and len(graph.retrieved) == 1
    asyncio.run(check())
