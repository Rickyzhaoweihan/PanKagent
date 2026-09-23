from copy import deepcopy

from pankagent_vnext.filter_recovery import recover_filter_failures
from pankagent_vnext.evidence_status import query_readiness, synthesis_evidence
from tests_vnext.test_partial_independent_readiness import result, preview


def proposal():
    return {'clarification': 'Filter rejected', 'recovery': {'category': 'scope_needs_clarification'},
            'steps': [
        {'id': 'coloc', 'question': 'Does this signal colocalize?', 'depends_on': [],
         'entity_resolution': {'state': 'needs_clarification', 'unknown_relations': []},
         'resolved_entities': [{'state': 'resolved', 'id': 'ADCY3'}],
         'constraints': [{'property': 'gwas_lead_vars', 'operator': 'CONTAINS',
                          'relationship_type': 'SIGNAL_COLOC_WITH', 'value': 'rs13393590'}],
         'semantic_issues': ['Filter unauthorized'],
         'runtime_binding_issues': ['missing_request_authorization:0:node.gwas_lead_vars']},
        {'id': 'qtl', 'question': 'Recorded QTL membership', 'depends_on': [], 'complete': True,
         'entity_resolution': {'state': 'resolved'}}]}


def test_rejected_filter_stays_rejected_but_verified_sibling_can_answer():
    original = proposal()
    snapshot = deepcopy(original)
    recovered = recover_filter_failures(original)
    assert original == snapshot
    assert recovered['clarification'] is None
    rejected = recovered['steps'][0]
    assert rejected['constraints'] == original['steps'][0]['constraints']
    assert rejected['semantic_issues'] == original['steps'][0]['semantic_issues']
    assert rejected['entity_resolution']['state'] == 'needs_clarification'
    assert 'lead variant' in rejected['filter_warning']['message']
    ready = query_readiness(recovered, preview(result('coloc', 'failed'), result('qtl')))
    assert ready['partial_ready'] and not ready['full_coverage']
    assert ready['blocked_step_ids'] == ['coloc']
    assert not query_readiness(recovered, preview(result('coloc', 'failed'), result('qtl', 'empty')))['ready']


def test_no_independent_evidence_or_unresolved_identity_keeps_recovery():
    for mode in ('dependent', 'context', 'single', 'unknown_entity', 'unknown_relation'):
        p = proposal()
        if mode == 'dependent': p['steps'][1]['depends_on'] = ['coloc']
        if mode == 'context': p['steps'][1]['purpose'] = 'context'
        if mode == 'single': p['steps'].pop()
        if mode == 'unknown_entity': p['steps'][0]['resolved_entities'][0]['state'] = 'ambiguous'
        if mode == 'unknown_relation': p['steps'][0]['entity_resolution']['unknown_relations'] = ['MADE_UP']
        assert recover_filter_failures(p) == p


def test_failed_filter_warning_survives_existing_synthesis_contract():
    warning = recover_filter_failures(proposal())['steps'][0]['filter_warning']
    failed = result('coloc', 'failed')
    failed.update(title=warning['message'], requested_scope={'filter_warning': warning},
                  nodes=[{'id': 'must-not-leak'}])
    compact = synthesis_evidence({'coloc': failed})['coloc']
    assert compact['requested_scope']['filter_warning'] == warning
    assert compact['title'] == warning['message']
    assert compact['nodes'] == [] and compact['status'] == 'unavailable'
