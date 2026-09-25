from copy import deepcopy
import pytest

from pankagent_vnext.semantic_decision import record
from pankagent_vnext.semantic_registry import attach_request_authorizations
from pankagent_vnext.query_templates import compile_query, runtime_binding_errors
from test_query_templates import step

QUESTION = 'For CFTR, does the T1D GWAS signal colocalize with a pancreas splicing QTL?'


def qtl():
    value = step('PART_OF_QTL_SIGNAL')
    value['constraints'].append({'property': 'tissue_id', 'operator': '=', 'value': 'UBERON_0001264',
        'entity_type': None, 'owner_role': 'part_of_qtl_signal', 'owner_kind': 'relationship',
        'relationship_type': 'PART_OF_QTL_SIGNAL'})
    value['semantic_request'] = {'source': 'user_request', 'question': QUESTION}
    value['model_scope_decision'] = record(value, QUESTION)
    return value


def test_model_tissue_choice_is_advisory_to_python_and_reaches_cypher():
    value = attach_request_authorizations(qtl())
    assert not value['semantic_issues']
    assert not runtime_binding_errors(value)
    assert value['preparation_advice'][0]['blocking'] is False
    assert value['request_filter_bindings'][1]['authorization_kind'] == 'claude_semantic_interpretation'
    compiled = compile_query(value)
    assert compiled is not None
    assert 'UBERON_0001264' in compiled['parameters'].values()
    assert 'tissue_id' in compiled['cypher']


@pytest.mark.parametrize('change', ['question', 'predicate', 'step', 'helper'])
def test_semantic_decision_does_not_authorize_later_unselected_scope(change):
    value = qtl()
    if change == 'question': value['semantic_request']['question'] = 'Find CFTR'
    elif change == 'predicate': value['constraints'][1]['value'] = 'other'
    elif change == 'step': value['id'] = 'another'
    else: value.pop('model_scope_decision')
    result = attach_request_authorizations(value)
    assert result['semantic_issues']
    assert any('missing_request_authorization:1:' in e for e in runtime_binding_errors(result))


def test_model_choice_does_not_override_database_identity_verification():
    value = qtl()
    value['resolved_entities'] = []
    result = attach_request_authorizations(value)
    assert 'missing_identity_resolution:0:Gene.name' in runtime_binding_errors(result)


@pytest.mark.parametrize('trace', [False, True])
def test_recorded_owner_normalization_preserves_the_model_selection(trace):
    value = qtl()
    original = deepcopy(value['constraints'][1])
    original.pop('owner_kind')
    original.pop('relationship_type')
    value['model_scope_decision']['constraints'][1] = original
    if trace:
        value['constraint_compilation'] = [{'constraint_index': 1, 'requested': original,
                                           'canonical_binding': deepcopy(value['constraints'][1])}]
    assert not runtime_binding_errors(attach_request_authorizations(value))


def test_supervisor_records_only_model_constraints_before_helper_expansion():
    import asyncio
    from test_claude_led_planning import gateway, SCHEMA
    from pankagent_vnext.planning_session import run
    from pankagent_vnext.semantic_decision import covers
    async def check():
        selected = {'id': 'qtl', 'question': QUESTION,
                    'constraints': [{'property': 'tissue_name', 'value': 'Pancreas'}]}
        proposal = {'interpreted_question': QUESTION, 'steps': [selected], 'clarification': None}
        g, calls = gateway([('record_plan', proposal)])
        def finalize(plan, choices):
            plan['steps'][0]['constraints'].append({'property': 'unrequested', 'value': 'helper'})
            plan['steps'][0]['model_scope_decision'] = {'source': 'forged'}
            return plan
        result = await run(g, QUESTION, '{}', 'test', SCHEMA, 500, finalize)
        prepared = result['steps'][0]
        assert len(calls) == 1
        assert covers(prepared, 0, prepared['constraints'][0], QUESTION)
        assert not covers(prepared, 1, prepared['constraints'][1], QUESTION)
    asyncio.run(check())
