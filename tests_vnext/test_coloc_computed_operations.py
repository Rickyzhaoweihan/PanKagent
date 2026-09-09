from copy import deepcopy

import pytest

from pankagent_vnext.coloc_scope import compile_comparisons, normalize_plan, summarize_linkage, RELEASE
from test_coloc_scope import separated_plan, evidence, GENE, DISEASE, VARIANT


def source_plan():
    value = separated_plan()
    for step in value['steps']:
        for record in step['resolved_entities']:
            record['name'] = {GENE: 'ADCY3', DISEASE: 'type 1 diabetes', VARIANT: VARIANT}[record['id']]
    value['steps'].append({'id': 'compare', 'question':
        'Do the recorded GWAS signal identifier (from rs13393590-T1D), the QTL signal identifier '
        '(from rs13393590-ADCY3), and the SIGNAL_COLOC_WITH signal identifier (ADCY3-T1D) match exactly?',
        'relation_types': ['PART_OF_GWAS_SIGNAL', 'PART_OF_QTL_SIGNAL', 'SIGNAL_COLOC_WITH'],
        'depends_on': ['s1', 's1_gwas', 's1_qtl'], 'constraints': [], 'complete': True,
        'evidence_combination': 'cooccurrence', 'graph_version': RELEASE})
    value['display_groups'][0]['step_ids'].append('compare')
    return value


def test_verified_terminal_comparison_reuses_three_checks_and_keeps_full_trace():
    original = source_plan()
    snapshot = deepcopy(original)
    value = compile_comparisons(original, RELEASE)
    assert original == snapshot
    assert [step['id'] for step in value['steps']] == ['s1', 's1_gwas', 's1_qtl']
    op = value['computed_operations'][0]
    assert op['original_step'] == original['steps'][-1]
    assert op['step_ids'] == {'primary': 's1', 'gwas': 's1_gwas', 'qtl': 's1_qtl'}
    assert op['no_new_retrieval'] is True
    assert value['display_groups'][0]['step_ids'] == ['s1', 's1_gwas', 's1_qtl']
    assert value['display_groups'][0]['computed_operation_ids'] == ['compare']
    actual = summarize_linkage(value, evidence())
    assert actual['computed_operations'][0]['status'] == 'complete'
    assert actual['groups'][0]['primary_record_count'] == 2
    assert actual['groups'][0]['records'][0]['match_kinds'] == [
        'recorded_qtl_lead', 'verified_gwas_credible_set_member', 'verified_qtl_credible_set_member']
    assert compile_comparisons(value, RELEASE) == value
    assert compile_comparisons(normalize_plan(value, RELEASE), RELEASE) == value


@pytest.mark.parametrize('suffix', [
    ' Retrieve additional signals in pancreas.', ' Only use GTEx.', ' Require PIP > 0.1.',
    ' Find the other variants.', ' Count how many donors match.', ' Use the top 5.',
    ' Prove the variant is causal.', ' Exclude exonQTL evidence.',
])
def test_new_textual_retrieval_or_filter_is_never_removed(suffix):
    original = source_plan()
    original['steps'][-1]['question'] += suffix
    assert compile_comparisons(original, RELEASE) == original


@pytest.mark.parametrize('mutation', ['constraint', 'missing_resolution', 'different_gene', 'partial',
                                    'duplicate_scope', 'downstream', 'ranking', 'unknown_release'])
def test_unverified_or_new_dependent_work_remains_executable(mutation):
    original = source_plan()
    if mutation == 'constraint':
        original['steps'][-1]['constraints'] = [{'property': 'pip', 'operator': '>', 'value': '.1'}]
    elif mutation == 'missing_resolution':
        original['steps'][0]['resolved_entities'] = []
    elif mutation == 'different_gene':
        original['steps'][0]['constraints'][0]['value'] = 'ENSG00000001084'
        original['steps'][0]['resolved_entities'][0]['id'] = 'ENSG00000001084'
        original['steps'][0]['resolved_entities'][0]['requested']['value'] = 'ENSG00000001084'
    elif mutation == 'partial':
        original['steps'][0]['complete'] = False
    elif mutation == 'duplicate_scope':
        extra = deepcopy(original['steps'][0]); extra['id'] = 'other'; original['steps'].insert(0, extra)
    elif mutation == 'downstream':
        original['steps'].append({'id': 'later', 'depends_on': ['compare']})
    elif mutation == 'ranking':
        original['steps'][-1]['ranking_contract'] = {'top_n': 1}
    release = 'other' if mutation == 'unknown_release' else RELEASE
    assert compile_comparisons(original, release) == original


@pytest.mark.parametrize('mutation', ['failure', 'truncated', 'scalar_only', 'stale'])
def test_computed_outcomes_do_not_claim_complete_for_unverified_evidence(mutation):
    value = compile_comparisons(source_plan(), RELEASE)
    prior = evidence()
    if mutation == 'failure':
        prior['s1_qtl']['status'] = 'failed'
    elif mutation == 'truncated':
        prior['s1_qtl']['truncated'] = True
    elif mutation == 'scalar_only':
        prior['s1_qtl']['edges'] = []; prior['s1_qtl']['rows'] = [{'count': 1}]
    elif mutation == 'stale':
        value['computed_operations'][0]['digest'] = 'old'
    actual = summarize_linkage(value, prior)
    assert actual['computed_operations'][0]['status'] != 'complete'
