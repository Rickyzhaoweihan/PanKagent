from copy import deepcopy
import time

import pytest

from pankagent_vnext.planning_scope import scope_issue

RELEASE = 'PanKgraph_08_04'


def mention(text, kind, identifier, name=None, **extra):
    return {'requested': text, 'state': 'resolved', 'candidates': [
        {'entity_type': kind, 'id': identifier, 'name': name or text, 'labels': [kind], 'match_kind': 'recorded_name'}], **extra}


def grounding(*mentions):
    return {'status': 'ready', 'version': 'preplanning-grounding-2',
            'identity': {'graph_release': RELEASE}, 'mentions': list(mentions)}


def constraint(kind, prop, value, **extra):
    return {'entity_type': kind, 'property': prop, 'value': value, 'operator': '=', **extra}


def step(*constraints, relation='PART_OF_QTL_SIGNAL', **extra):
    return {'id': 's1', 'relation_types': [relation], 'constraints': list(constraints), 'depends_on': [], **extra}


def plan(*steps):
    return {'steps': list(steps), 'clarification': None}


GENE = mention('GCLC', 'Gene', 'ENSG00000001084')
PANCREAS = mention('pancreas', 'anatomical_structure', 'UBERON_0001264')
QUESTION = 'Show all QTL evidence for GCLC in pancreas.'


def test_raw_direct_tissue_cannot_be_dropped_by_interpretation():
    value = plan(step(constraint('Gene', 'id', GENE['candidates'][0]['id'])))
    value['interpreted_question'] = 'Show QTL evidence for GCLC.'
    assert scope_issue(QUESTION, grounding(GENE, PANCREAS), value) == 'missing_requested_scope:tissue:pancreas:PART_OF_QTL_SIGNAL'


@pytest.mark.parametrize('tissue', [
    constraint('anatomical_structure', 'id', 'UBERON_0001264'),
    constraint('anatomical_structure', 'name', 'Pancreas'),
    constraint(None, 'tissue', 'pancreas'),
    constraint(None, 'tissue_id', 'UBERON_0001264', relationship_type='PART_OF_QTL_SIGNAL'),
    constraint(None, 'tissue_name', 'Pancreas', relationship_type='PART_OF_QTL_SIGNAL'),
])
def test_verified_tissue_id_name_and_compiler_alias_are_equivalent(tissue):
    value = plan(step(constraint('Gene', 'name', 'GCLC'), tissue))
    original = deepcopy(value)
    assert scope_issue(QUESTION, grounding(GENE, PANCREAS), value) is None
    assert value == original


@pytest.mark.parametrize('tissue', [
    constraint('Gene', 'tissue_name', 'Pancreas'),
    constraint(None, 'tissue_name', 'Islet'),
    constraint(None, 'tissue_name', 'Pancreas', relationship_type='HAS_SAMPLE'),
    constraint('disease', 'id', 'UBERON_0001264'),
])
def test_wrong_tissue_value_owner_or_relation_cannot_cover_request(tissue):
    assert 'missing_requested_scope:tissue:' in scope_issue(QUESTION, grounding(GENE, PANCREAS), plan(step(constraint('Gene', 'name', 'GCLC'), tissue)))


def test_tissue_on_unrelated_sample_step_does_not_cover_qtl_scope():
    value = plan(step(constraint('Gene', 'name', 'GCLC')),
                 step(constraint('anatomical_structure', 'name', 'pancreas'), relation='HAS_SAMPLE', id='samples'))
    assert scope_issue(QUESTION, grounding(GENE, PANCREAS), value).endswith('PART_OF_QTL_SIGNAL')


@pytest.mark.parametrize('kind,identifier,name,relation', [
    ('Gene', 'ENSG00000138031', 'ADCY3', 'SIGNAL_COLOC_WITH'),
    ('variants', 'rs13393590', 'rs13393590', 'PART_OF_GWAS_SIGNAL'),
    ('disease', 'MONDO_0005147', 'T1D', 'EFFECTOR_GENE_OF'),
])
def test_named_anchor_loss_is_detected_and_id_name_equivalence_supported(kind, identifier, name, relation):
    raw = 'Show evidence for ' + name
    data = grounding(mention(name, kind, identifier))
    assert scope_issue(raw, data, plan(step(relation=relation)))
    for field, value in [('id', identifier), ('name', name)]:
        assert scope_issue(raw, data, plan(step(constraint(kind, field, value), relation=relation))) is None


def test_single_gene_is_preserved_in_each_independent_gene_category():
    value = plan(step(constraint('Gene', 'name', 'GCLC')), step(relation='ASSOCIATED_WITH_GO', id='go'))
    assert scope_issue('Show GCLC QTL and GO evidence.', grounding(GENE), value) == 'missing_requested_scope:Gene:GCLC:go'


def test_dependent_gene_discovery_does_not_filter_every_partner_back_to_anchor():
    value = plan(step(constraint('Gene', 'name', 'GCLC'), relation='PHYSICAL_INTERACTION'),
                 step(relation='PART_OF_QTL_SIGNAL', id='partner_qtl', depends_on=['s1']))
    assert scope_issue('Show QTL for the physical interaction partners of GCLC.', grounding(GENE), value) is None


def test_separate_coloc_roles_preserve_named_variant_without_filtering_gene_disease_edge():
    gene = mention('ADCY3', 'Gene', 'ENSG00000138031')
    snp = mention('rs13393590', 'variants', 'rs13393590')
    disease = mention('T1D', 'disease', 'MONDO_0005147', 'type 1 diabetes')
    value = plan(step(constraint('Gene', 'name', 'ADCY3'), constraint('disease', 'id', 'MONDO_0005147'), relation='SIGNAL_COLOC_WITH'),
                 step(constraint('variants', 'id', 'rs13393590'), constraint('disease', 'name', 'type 1 diabetes'), relation='PART_OF_GWAS_SIGNAL', id='gwas'),
                 step(constraint('Gene', 'id', 'ENSG00000138031'), constraint('variants', 'id', 'rs13393590'), id='qtl'))
    assert scope_issue('Does ADCY3 coloc with the T1D signal rs13393590?', grounding(gene, snp, disease), value) is None
    value['steps'][1]['constraints'] = value['steps'][1]['constraints'][1:]
    value['steps'][2]['constraints'] = value['steps'][2]['constraints'][:1]
    assert scope_issue('Does ADCY3 coloc with the T1D signal rs13393590?', grounding(gene, snp, disease), value).startswith('missing_requested_scope:variants:')


def test_disease_context_encoded_by_t1d_measurement_or_donor_stage_does_not_require_disease_node():
    data = grounding(mention('T1D', 'disease', 'MONDO_0005147', 'type 1 diabetes'))
    assert scope_issue('Show T1D differential expression', data, plan(step(relation='T1D_DEG_IN'))) is None
    assert scope_issue('Find T1D stage 3 donors', data, plan(step(constraint('donor', 't1d_stage', 'Stage 3'), relation='HAS_DONOR'))) is None


def test_incidental_anatomy_and_explicit_examples_are_not_forced_into_filters():
    data = grounding(GENE, PANCREAS)
    assert scope_issue('Show GCLC pathways relevant to pancreas biology', data, plan(step(constraint('Gene', 'name', 'GCLC'), relation='FUNCTION_ANNOTATION'))) is None
    data = grounding(GENE, mention('ADCY3', 'Gene', 'ENSG00000138031'))
    assert scope_issue('Show GCLC QTL; unrelated example ADCY3', data, plan(step(constraint('Gene', 'name', 'GCLC')))) is None


@pytest.mark.parametrize('state', ['ambiguous', 'qualified', 'not_found'])
def test_unresolved_mentions_do_not_become_guessed_filters(state):
    tissue = {**PANCREAS, 'state': state}
    assert scope_issue(QUESTION, grounding(GENE, tissue), plan(step(constraint('Gene', 'name', 'GCLC')))) is None


def test_old_or_unavailable_grounding_and_real_clarification_are_left_to_existing_flow():
    data = grounding(GENE, PANCREAS)
    data['identity']['graph_release'] = 'other'
    assert scope_issue(QUESTION, data, plan(step())) is None
    assert scope_issue(QUESTION, {'status': 'unavailable'}, plan(step())) is None
    assert scope_issue(QUESTION, grounding(GENE, PANCREAS), {'steps': [], 'clarification': 'Which of the two tissues?'}) is None


def test_direct_sample_tissue_scope_uses_same_generic_contract():
    pln = mention('PLN', 'anatomical_structure', 'UBERON_0015865', 'pancreaticosplenic lymph node')
    question = 'How many PLN RNA samples are indexed?'
    assert scope_issue(question, grounding(pln), plan(step(relation='HAS_SAMPLE')))
    assert scope_issue(question, grounding(pln), plan(step(constraint('anatomical_structure', 'id', 'UBERON_0015865'), relation='HAS_SAMPLE'))) is None


def test_scope_guard_is_local_and_fast():
    value = plan(step(constraint('Gene', 'name', 'GCLC'), constraint(None, 'tissue_name', 'Pancreas')))
    start = time.monotonic()
    for _ in range(100):
        assert scope_issue(QUESTION, grounding(GENE, PANCREAS), value) is None
    assert (time.monotonic() - start) / 100 < .02


@pytest.mark.parametrize('question', ['Use islet instead of PLN.', 'Replace PLN with islet.'])
def test_explicit_tissue_replacement_requires_new_not_old_scope(question):
    pln = mention('PLN', 'anatomical_structure', 'UBERON_0015865')
    islet = mention('islet', 'anatomical_structure', 'UBERON_0000006')
    data = grounding(pln, islet)
    wanted = plan(step(constraint('anatomical_structure', 'id', 'UBERON_0000006'), relation='HAS_SAMPLE'))
    assert scope_issue(question, data, wanted) is None
    old = plan(step(constraint('anatomical_structure', 'id', 'UBERON_0015865'), relation='HAS_SAMPLE'))
    assert scope_issue(question, data, old).startswith('missing_requested_scope:tissue:islet:')


def test_explicit_gene_replacement_keeps_other_requested_gene_and_rejects_old_only_plan():
    nfya = mention('NFYA', 'Gene', 'ENSG00000001167')
    gcg = mention('GCG', 'Gene', 'ENSG00000115263')
    data = grounding(GENE, nfya, gcg)
    question = 'Use NFYA instead of GCLC; keep GCG QTL.'
    wanted = plan(step(constraint('Gene', 'name', 'NFYA')), step(constraint('Gene', 'name', 'GCG'), id='s2'))
    assert scope_issue(question, data, wanted) is None
    wanted['steps'].pop()
    assert scope_issue(question, data, wanted).startswith('missing_requested_scope:Gene:GCG')


def test_ambiguous_or_negated_replacement_never_suppresses_the_old_identity():
    nfya = mention('NFYA', 'Gene', 'ENSG00000001167', state='ambiguous')
    value = plan(step(constraint('Gene', 'name', 'NFYA')))
    assert scope_issue('Use NFYA instead of GCLC.', grounding(GENE, nfya), value)
    nfya['state'] = 'resolved'
    assert scope_issue('Do not use NFYA instead of GCLC.', grounding(GENE, nfya), value)


def test_replaced_occurrence_does_not_erase_separate_positive_scope():
    nfya = mention('NFYA', 'Gene', 'ENSG00000001167')
    question = 'Use NFYA instead of GCLC; separately show GCLC marker annotations.'
    value = plan(step(constraint('Gene', 'name', 'NFYA')))
    assert scope_issue(question, grounding(GENE, nfya), value)


def test_one_shared_tissue_must_survive_in_each_independent_gene_check():
    nfya = mention('NFYA', 'Gene', 'ENSG00000001167')
    pancreatic = mention('pancreatic', 'anatomical_structure', 'UBERON_0001264', name='Pancreas')
    question = 'Compare pancreatic QTL for GCLC and NFYA.'
    value = plan(step(constraint('Gene', 'name', 'GCLC'), constraint(None, 'tissue_name', 'Pancreas')),
                 step(constraint('Gene', 'name', 'NFYA'), id='nfya'))
    assert scope_issue(question, grounding(GENE, nfya, pancreatic), value)
    value['steps'][1]['constraints'].append(constraint(None, 'tissue_id', 'UBERON_0001264'))
    assert scope_issue(question, grounding(GENE, nfya, pancreatic), value) is None


def test_tissue_specific_to_one_named_gene_is_not_forced_into_other_gene():
    nfya = mention('NFYA', 'Gene', 'ENSG00000001167')
    question = 'Show GCLC QTL in pancreas; show NFYA QTL in any tissue separately.'
    value = plan(step(constraint('Gene', 'name', 'GCLC'), constraint(None, 'tissue_name', 'Pancreas')),
                 step(constraint('Gene', 'name', 'NFYA'), id='nfya'))
    assert scope_issue(question, grounding(GENE, nfya, PANCREAS), value) is None
