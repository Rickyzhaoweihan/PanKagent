from copy import deepcopy

import pytest

from pankagent_vnext.planning_requirements import requirements_issue
from pankagent_vnext.preplanning_grounding import _requested_relations, explicit_non_go_annotation_scope
from pankagent_vnext.revision_context import parent_context


def field(kind, prop, value, operator='=', **extra):
    return {'entity_type': kind, 'property': prop, 'value': value, 'operator': operator, **extra}


def step(*constraints, relation='ASSOCIATED_WITH_GO', identifier='s1', **extra):
    return {'id': identifier, 'question': '', 'relation_types': [relation], 'constraints': list(constraints),
            'depends_on': [], 'complete': True, 'evidence_combination': 'independent', **extra}


def plan(*steps, **extra):
    return {'steps': list(steps), 'clarification': None, **extra}


def grounding(domain='biological_process'):
    result = {'status': 'ready', 'identity': {'graph_release': 'PanKgraph_08_04'},
              'schema': {'categories': {'GO_term.go_domain': ['biological_process', 'molecular_function', 'cellular_component']}},
              'mentions': []}
    for name, identifier in [('GLIS3', 'ENSG00000107249'), ('CFTR', 'ENSG00000001626'),
                             ('GCLC', 'ENSG00000001084'), ('NFYA', 'ENSG00000001167')]:
        result['mentions'].append({'requested': name, 'state': 'resolved', 'candidates': [
            {'entity_type': 'Gene', 'id': identifier, 'name': name}]})
    if domain:
        result['mentions'].append({'requested': domain.replace('_', ' '), 'state': 'incidental',
            'context_role': {'kind': 'ontology_domain', 'resolution_state': 'resolved',
                             'canonical_binding': {'entity_type': 'GO_term', 'property': 'go_domain', 'value': domain}}})
    return result


GO = field('GO_term', 'go_domain', 'biological_process')
GENE = field('Gene', 'id', 'ENSG00000107249')
NEW_GENE = field('Gene', 'id', 'ENSG00000001626')


def history(parent, instruction='Use CFTR instead of GLIS3, keeping the biological-process annotation scope.'):
    normalized, _ = parent_context({'plan': parent})
    return [{'revision_context': {'parent_plan': normalized, 'instruction': instruction}}]


@pytest.mark.parametrize('constraint', [None, field('Gene', 'go_domain', 'biological_process'),
    field('GO_term', 'go_domain', 'molecular_function'),
    field('GO_term', 'go_domain', '["biological_process", "molecular_function"]', 'IN'),
    field('GO_term', 'go_domain', 'biological_process', '!=')])
def test_recorded_domain_cannot_be_missing_broadened_or_bound_to_wrong_owner(constraint):
    proposal = plan(step(NEW_GENE, *([constraint] if constraint else [])))
    assert requirements_issue('Show biological-process GO annotations for CFTR', grounding(), proposal).startswith('missing_requested_category_binding:')


@pytest.mark.parametrize('constraint', [GO, field('GO_term', 'go_domain', '["biological_process"]', 'IN')])
def test_exact_domain_predicates_match_recorded_category(constraint):
    assert requirements_issue('Show biological-process GO annotations for CFTR', grounding(), plan(step(NEW_GENE, constraint))) is None


def test_unavailable_category_is_not_invented_and_explicit_clarification_is_retained():
    data = grounding()
    data['schema']['categories'] = {}
    assert requirements_issue('GO annotations', data, plan(step(GENE))) is None
    assert requirements_issue('GO annotations for unresolved entity', grounding(), plan(clarification='Which exact gene?')) is None


def test_j01_actual_parent_shape_retains_domain_and_allows_name_to_id_equivalence():
    old = step(GENE, GO, purpose='primary', owner_kind='unused', resolved_entities=[
        {'constraint_index': 0, 'state': 'resolved', 'id': 'ENSG00000107249', 'name': 'GLIS3', 'entity_type': 'Gene', 'labels': ['Gene'], 'resolution_key': 'not-replayed'}])
    parent = plan(old)
    h = history(parent)
    before = deepcopy(h)
    assert requirements_issue(h[-1]['revision_context']['instruction'], grounding(), plan(step(field('Gene', 'name', 'CFTR'), GO)), h) is None
    assert h == before
    assert 'resolution_key' not in h[-1]['revision_context']['parent_plan']['steps'][0]['resolved_entities'][0]
    issue = requirements_issue(h[-1]['revision_context']['instruction'], grounding(), plan(step(NEW_GENE)), h)
    assert issue == 'revision_constraint_not_preserved:s1:GO_term.go_domain'


def test_revision_guard_alone_preserves_domain_when_not_repeated_in_instruction():
    h = history(plan(step(GENE, GO)), 'Replace GLIS3 with CFTR.')
    issue = requirements_issue('Replace GLIS3 with CFTR.', grounding(None), plan(step(NEW_GENE)), h)
    assert issue == 'revision_constraint_not_preserved:s1:GO_term.go_domain'


def test_j02_actual_qtl_parent_shape_accepts_verified_tissue_id_name_equivalence():
    gclc = field('Gene', 'id', 'ENSG00000001084', owner_kind='node')
    tissue = field(None, 'tissue_id', 'UBERON_0001264', relationship_type='PART_OF_QTL_SIGNAL', owner_kind='relationship')
    parent = plan(step(gclc, tissue, relation='PART_OF_QTL_SIGNAL', purpose='primary', resolved_entities=[
        {'state': 'resolved', 'id': 'ENSG00000001084', 'name': 'GCLC', 'entity_type': 'Gene'},
        {'state': 'resolved', 'id': 'UBERON_0001264', 'name': 'Pancreas', 'entity_type': 'anatomical_structure'}]))
    h = history(parent, 'Use NFYA instead of GCLC, keeping the pancreas tissue constraint.')
    new = plan(step(field('Gene', 'name', 'NFYA'), field(None, 'tissue_name', 'Pancreas', relationship_type='PART_OF_QTL_SIGNAL'), relation='PART_OF_QTL_SIGNAL'))
    assert requirements_issue(h[-1]['revision_context']['instruction'], grounding(None), new, h) is None
    new['steps'][0]['constraints'][1]['value'] = 'Islet'
    assert requirements_issue(h[-1]['revision_context']['instruction'], grounding(None), new, h).startswith('revision_constraint_not_preserved:')


@pytest.mark.parametrize('change', ['operator', 'value', 'complete', 'category', 'extra_check'])
def test_replacement_only_revision_preserves_other_filters_categories_and_completeness(change):
    threshold = field(None, 'padj', '0.05', '<=', relationship_type='GENE_ENRICHED_IN')
    parent = plan(step(GENE, threshold, relation='GENE_ENRICHED_IN'))
    new = plan(step(NEW_GENE, deepcopy(threshold), relation='GENE_ENRICHED_IN'))
    if change == 'operator': new['steps'][0]['constraints'][1]['operator'] = '>='
    if change == 'value': new['steps'][0]['constraints'][1]['value'] = '0.1'
    if change == 'complete': new['steps'][0]['complete'] = False
    if change == 'category': new['steps'][0]['relation_types'] = ['GENE_DETECTED_IN']
    if change == 'extra_check': new['steps'].append(step(NEW_GENE, identifier='extra'))
    h = history(parent, 'Use CFTR instead of GLIS3 keeping all other filters.')
    assert requirements_issue(h[-1]['revision_context']['instruction'], grounding(None), new, h).startswith('revision_')


@pytest.mark.parametrize('instruction', [
    'Use CFTR instead of GLIS3 and broaden the GO scope.',
    'Show CFTR across all GO domains instead.',
    'Use CFTR instead of GLIS3, keeping tissue but remove the GO filter.',
])
def test_arbitrary_revision_is_not_mistaken_for_replacement_only(instruction):
    h = history(plan(step(GENE, GO)), instruction)
    assert requirements_issue(instruction, grounding(None), plan(step(NEW_GENE)), h) is None


def test_ambiguous_replacement_name_cannot_authorize_identity_substitution():
    data = grounding(None)
    data['mentions'].append({'requested': 'CFTR', 'state': 'resolved', 'candidates': [
        {'entity_type': 'Gene', 'id': 'different', 'name': 'CFTR'}]})
    h = history(plan(step(GENE, GO)), 'Replace GLIS3 with CFTR.')
    assert requirements_issue('Replace GLIS3 with CFTR.', data, plan(step(NEW_GENE)), h) is None


@pytest.mark.parametrize('question', [
    'Show pathway annotations for IL2RA and marker-cell annotations for GCG as separate evidence groups.',
    'Show Reactome pathway annotations for RFX6.',
    'Show marker gene annotations for MAFA.',
    'List KEGG and Reactome pathways for a gene.',
])
def test_explicit_pathway_and_marker_requests_do_not_admit_unrequested_go(question):
    assert explicit_non_go_annotation_scope(question)
    assert 'ASSOCIATED_WITH_GO' not in _requested_relations(question)
    proposal = plan(step(GENE, relation='FUNCTION_ANNOTATION'), step(GENE, identifier='extra', purpose='context'))
    assert requirements_issue(question, grounding(None), proposal).startswith('unrequested_evidence_category:')


@pytest.mark.parametrize('question', [
    'Show annotations for IL2RA.', 'Give a comprehensive gene profile.',
    'Show pathways and generic annotations for IL2RA.',
    'Show Reactome pathway annotations and GO molecular-function annotations for RFX6.',
    'Show marker annotations and GO annotations for MAFA.',
])
def test_generic_or_explicit_go_requests_remain_broad(question):
    assert not explicit_non_go_annotation_scope(question)
    if 'profile' not in question:
        assert 'ASSOCIATED_WITH_GO' in _requested_relations(question)


def test_stale_grounding_never_enforces_new_requirements():
    data = grounding()
    data['identity']['graph_release'] = 'other'
    assert requirements_issue('Show GO biological process', data, plan(step(GENE))) is None
