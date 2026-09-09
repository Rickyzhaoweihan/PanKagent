from copy import deepcopy

import pytest

from pankagent_vnext.planning_requirements import requirements_issue
from test_planning_requirements import grounding, field, step, plan, GENE, NEW_GENE, GO


MF = field('GO_term', 'go_domain', 'molecular_function')


def context():
    result = grounding()
    result['mentions'].extend(grounding('molecular_function')['mentions'][-1:])
    return result


@pytest.mark.parametrize('question', [
    'Show biological-process GO annotations for GLIS3 and molecular-function GO annotations for CFTR.',
    'For GLIS3 show biological process GO annotations; for CFTR show molecular function GO annotations.',
    'GLIS3 biological-process annotations, CFTR molecular-function annotations.',
    'Show biological-process annotations for GLIS3 while showing molecular-function GO annotations for CFTR.',
])
def test_each_explicit_gene_domain_role_is_preserved(question):
    valid = plan(step(GENE, GO), step(NEW_GENE, MF, identifier='s2'))
    assert requirements_issue(question, context(), valid) is None
    swapped = plan(step(GENE, MF), step(NEW_GENE, GO, identifier='s2'))
    assert requirements_issue(question, context(), swapped).startswith('missing_requested_category_binding:')


@pytest.mark.parametrize('question', [
    'Show biological-process GO annotations for GLIS3 and all GO annotations for CFTR.',
    'GLIS3: biological-process GO annotations; CFTR: GO annotations across all domains.',
])
def test_explicit_unrestricted_other_gene_does_not_inherit_domain(question):
    valid = plan(step(GENE, GO), step(NEW_GENE, identifier='s2'))
    assert requirements_issue(question, grounding(), valid) is None
    narrowed = plan(step(GENE, GO), step(NEW_GENE, GO, identifier='s2'))
    assert requirements_issue(question, grounding(), narrowed).endswith('GO_term.go_domain:unrestricted')


@pytest.mark.parametrize('question', [
    'Show biological-process GO annotations for GLIS3 and CFTR.',
    'For both GLIS3 and CFTR show biological-process GO annotations.',
    'Show GO annotations in biological process for GLIS3 and CFTR.',
])
def test_shared_domain_phrase_applies_to_all_named_genes(question):
    assert requirements_issue(question, grounding(), plan(step(GENE, GO), step(NEW_GENE, GO, identifier='s2'))) is None
    combined = plan(step(field('Gene', 'id', '["ENSG00000107249", "ENSG00000001626"]', 'IN'), GO))
    assert requirements_issue(question, grounding(), combined) is None
    assert requirements_issue(question, grounding(), plan(step(GENE, GO))).startswith('missing_requested_category_binding:')


@pytest.mark.parametrize('question', [
    'Show biological-process and molecular-function GO annotations for GLIS3 and CFTR.',
    'For GLIS3 and CFTR show biological-process and molecular-function GO annotations.',
])
def test_shared_multiple_domains_may_be_split_but_not_lost(question):
    proposal = plan(step(GENE, GO), step(GENE, MF, identifier='s2'),
                    step(NEW_GENE, GO, identifier='s3'), step(NEW_GENE, MF, identifier='s4'))
    assert requirements_issue(question, context(), proposal) is None
    proposal['steps'].pop()
    assert requirements_issue(question, context(), proposal).startswith('missing_requested_category_binding:')


def test_other_evidence_clause_does_not_request_go_for_its_gene():
    question = 'Show biological-process GO annotations for GLIS3 and molecular QTL evidence for CFTR.'
    proposal = plan(step(GENE, GO), step(NEW_GENE, relation='PART_OF_QTL_SIGNAL', identifier='s2'))
    assert requirements_issue(question, grounding(), proposal) is None
    proposal['steps'].append(step(NEW_GENE, GO, identifier='unrequested'))
    assert requirements_issue(question, grounding(), proposal).startswith('missing_requested_category_scope:')


def test_crossed_role_cannot_be_hidden_in_combined_gene_and_domain_lists():
    question = 'Show biological-process GO annotations for GLIS3 and molecular-function GO annotations for CFTR.'
    proposal = plan(step(field('Gene', 'id', '["ENSG00000107249", "ENSG00000001626"]', 'IN'),
                         field('GO_term', 'go_domain', '["biological_process", "molecular_function"]', 'IN')))
    assert requirements_issue(question, context(), proposal).startswith('missing_requested_category_binding:')


def test_conflicting_and_predicates_are_not_counted_as_a_union():
    question = 'Show biological-process and molecular-function GO annotations for GLIS3.'
    assert requirements_issue(question, context(), plan(step(GENE, GO, MF))).startswith('missing_requested_category_binding:')
    assert requirements_issue('Show biological-process GO annotations for GLIS3.', grounding(),
                              plan(step(GENE, GO, field('GO_term', 'go_domain', 'biological_process', '!=')))).startswith('missing_requested_category_binding:')


@pytest.mark.parametrize('question', [
    'Show GLIS3 biological-process GO CFTR molecular-function annotations.',
    'Show GLIS3 biological-process GO annotations CFTR.',
])
def test_ambiguous_interleaved_roles_request_repair_instead_of_nearest_gene(question):
    proposal = plan(step(GENE, GO), step(NEW_GENE, MF, identifier='s2'))
    assert requirements_issue(question, context(), proposal).startswith('ambiguous_requested_category_scope:')
    assert requirements_issue(question, context(), plan(clarification='Which GO domain should apply to each gene?')) is None


def test_role_validation_does_not_mutate_question_grounding_or_proposal():
    question = 'Show biological-process GO annotations for GLIS3 and molecular-function GO annotations for CFTR.'
    data, proposal = context(), plan(step(GENE, GO), step(NEW_GENE, MF, identifier='s2'))
    before = deepcopy((data, proposal))
    assert requirements_issue(question, data, proposal) is None
    assert (data, proposal) == before
