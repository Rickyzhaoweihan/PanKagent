"""Shared QTL tissue constraints must survive independent evidence checks."""
import asyncio

import pytest

from test_preplanning_grounding import contextual_graph
from pankagent_vnext.preplanning_grounding import ground_question

from pankagent_vnext import planning_scope as candidate


def two_genes():
    return {'steps': [
        {'id': 'first', 'relation_types': ['PART_OF_QTL_SIGNAL'], 'depends_on': [], 'constraints': [
            {'entity_type': 'Gene', 'property': 'name', 'value': 'ADCY3'}]},
        {'id': 'second', 'relation_types': ['PART_OF_QTL_SIGNAL'], 'depends_on': [], 'constraints': [
            {'entity_type': 'Gene', 'property': 'name', 'value': 'CFTR'},
            {'entity_type': None, 'property': 'tissue_name', 'value': 'Pancreas'}]},
    ]}


@pytest.mark.parametrize('question', [
    'Compare pancreatic QTL evidence for ADCY3 and CFTR.',
    'Compare pancreatic eQTL evidence for ADCY3 and CFTR.',
    'Compare pancreatic sQTL evidence for ADCY3 and CFTR.',
    'Compare QTL evidence in pancreas for ADCY3 and CFTR.',
    'Compare eQTL signals from the pancreas for ADCY3 and CFTR.',
    'Show sQTL associations within human pancreas for ADCY3 and CFTR.',
])
def test_shared_tissue_must_survive_in_every_independent_gene_check(question):
    grounding = asyncio.run(ground_question(contextual_graph(), question))
    plan = two_genes()
    assert candidate.scope_issue(question, grounding, plan).startswith('missing_requested_scope:tissue:')
    plan['steps'][0]['constraints'].append({'entity_type': None, 'property': 'tissue_name', 'value': 'Pancreas'})
    assert candidate.scope_issue(question, grounding, plan) is None


@pytest.mark.parametrize('question', [
    'Show ADCY3 QTL from any tissue; CFTR QTL in pancreas.',
    'Show CFTR QTL in pancreas; ADCY3 QTL from any tissue.',
])
def test_one_gene_specific_tissue_does_not_narrow_another_gene(question):
    grounding = asyncio.run(ground_question(contextual_graph(), question))
    assert candidate.scope_issue(question, grounding, two_genes()) is None


def test_sample_scope_does_not_become_shared_qtl_scope():
    words = candidate.phrase_tokens('Compare pancreatic samples and QTL evidence for ADCY3 and CFTR')
    assert candidate._shared_qtl_tissue(words, [(1, 2)], [7, 9]) is False


@pytest.mark.parametrize('family', ['qtl','eqtl','sqtl','exonqtl','qtls','eqtls','sqtls','exonqtls'])
def test_direct_tissue_recognizes_all_qtl_family_forms(family):
    words = candidate.phrase_tokens('pancreas ' + family + ' for ADCY3 and CFTR')
    assert candidate._direct_tissue(words, [(0, 1)])
    assert candidate._shared_qtl_tissue(words, [(0, 1)], [3, 5])
