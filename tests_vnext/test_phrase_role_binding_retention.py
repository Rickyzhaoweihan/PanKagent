"""Illustrative values cannot erase independently requested canonical bindings."""
from copy import deepcopy

import pytest

from pankagent_vnext.semantic_decision import apply_phrase_roles, phrase_roles, record


def prepared(question, constraints, roles, **extra):
    step = {'id': 's1', 'question': question, 'constraints': deepcopy(constraints), **extra}
    step['model_scope_decision'] = record(step, question, phrase_roles(question, roles))
    return step


def predicate(owner, prop, value):
    return {'entity_type': owner, 'property': prop, 'operator': '=', 'value': value}


@pytest.mark.parametrize('value,example', [('1', '10x Multiome'), ('INS', 'INSR'),
                                          ('T1D', 'T1Dmarker'), ('rs123', 'rs1234')])
def test_short_values_do_not_match_substrings_of_example_identifiers(value, example):
    question = f'Find stage I donors and describe examples like {example}.'
    constraint = predicate('donor', 't1d_stage', value)
    step = prepared(question, [constraint], [{'text': 'stage I', 'role': 'filter'},
                                            {'text': example, 'role': 'example'}])
    assert apply_phrase_roles(step, question)['constraints'] == [constraint]


def test_canonical_gene_id_is_preserved_by_independent_verified_identity_fact():
    question = 'Tell me about CFTR and identifier formats, like ENSG00000001626.'
    constraint = predicate('Gene', 'id', 'ENSG00000001626')
    step = prepared(question, [constraint], [{'text': 'CFTR', 'role': 'anchor'},
        {'text': 'ENSG00000001626', 'role': 'example'}], request_phrase_identity_facts=[
        {'mention': 'CFTR', 'entity_type': 'Gene', 'id': 'ENSG00000001626', 'name': 'CFTR'}])
    assert apply_phrase_roles(step, question)['constraints'] == [constraint]


def test_canonical_alias_keeps_existing_constraint_compilation_from_real_filter():
    question = 'Find donors with type 1 diabetes and describe annotations like T1D status.'
    constraint = predicate('donor', 'diabetes_type', 'T1D')
    step = prepared(question, [constraint], [{'text': 'type 1 diabetes', 'role': 'filter'},
        {'text': 'T1D status', 'role': 'example'}], constraint_compilation=[{
        'canonical_binding': constraint,
        'requested': predicate('donor', 'diabetes_type', 'type 1 diabetes')}])
    assert apply_phrase_roles(step, question)['constraints'] == [constraint]


def test_unresolved_alias_conflict_requests_repair_instead_of_dropping_real_filter():
    question = 'Find donors with type 1 diabetes and describe annotations like T1D status.'
    constraint = predicate('donor', 'diabetes_type', 'T1D')
    step = prepared(question, [constraint], [{'text': 'type 1 diabetes', 'role': 'filter'},
        {'text': 'T1D status', 'role': 'example'}])
    with pytest.raises(ValueError, match='ambiguous_example_constraint_ownership'):
        apply_phrase_roles(step, question)
    assert step['constraints'] == [constraint]


def test_example_only_identity_fact_cannot_restore_an_example_filter():
    question = 'Explain identifier formats like ENSG00000001626.'
    constraint = predicate('Gene', 'id', 'ENSG00000001626')
    step = prepared(question, [constraint], [{'text': 'ENSG00000001626', 'role': 'example'}],
        request_phrase_identity_facts=[{'mention': 'ENSG00000001626', 'entity_type': 'Gene',
                                       'id': 'ENSG00000001626', 'name': 'CFTR'}])
    assert apply_phrase_roles(step, question)['constraints'] == []


def test_independent_tissue_does_not_prevent_removing_example_only_assay():
    question = 'What sample types are available from spleen, like scRNAseq?'
    tissue = predicate('anatomical_structure', 'id', 'tissue1')
    assay = predicate('Sample_node', 'data_modality', 'scRNA-seq')
    step = prepared(question, [tissue, assay], [{'text': 'spleen', 'role': 'filter'},
        {'text': 'scRNAseq', 'role': 'example'}], request_phrase_identity_facts=[
            {'mention': 'spleen', 'entity_type': 'anatomical_structure', 'id': 'tissue1', 'name': 'spleen'}])
    assert apply_phrase_roles(step, question)['constraints'] == [tissue]


def test_scope_compilation_without_circular_authorization_keeps_real_binding():
    from test_requested_scope_compile import grounding, plan, step as query_step
    from pankagent_vnext.planning_requirements import compile_requested_scope
    from pankagent_vnext.semantic_decision import effective_scope
    from pankagent_vnext.semantic_registry import attach_request_authorizations
    question = 'In pancreas, what molecular QTL evidence is recorded for GCLC? Explain identifiers like tissue1.'
    value = query_step()
    value['model_scope_decision'] = record(value, question, phrase_roles(question,
        [{'text': 'tissue1', 'role': 'example'}]))
    compiled, issue = compile_requested_scope(effective_scope(question, value), grounding(), plan(value))
    assert issue is None
    value = compiled['steps'][0]
    value['graph_version'] = 'PanKgraph_08_04'
    value['semantic_request'] = {'source': 'user_request', 'question': question}
    result = attach_request_authorizations(value)
    assert not result.get('semantic_issues')


def test_planning_records_retained_identity_facts_before_scope_cancellation():
    import asyncio
    from test_claude_led_planning import gateway, SCHEMA
    from pankagent_vnext.planning_session import run

    async def check():
        question = 'Tell me about CFTR and identifier formats like ENSG00000001626.'
        constraint = predicate('Gene', 'id', 'ENSG00000001626')
        proposal = {'interpreted_question': question, 'clarification': None,
            'request_phrase_roles': [{'text': 'CFTR', 'role': 'anchor'},
                {'text': 'ENSG00000001626', 'role': 'example'}],
            'steps': [{'id': 's1', 'question': question, 'constraints': [constraint],
                       'request_phrase_identity_facts': [{'mention': 'forged'}]}]}
        model, calls = gateway([('record_plan', proposal)])
        proof = {'mention': 'CFTR', 'entity_type': 'Gene', 'id': 'ENSG00000001626', 'name': 'CFTR'}
        def finalize(plan, choices):
            assert plan['steps'][0]['constraints'] == [constraint]
            assert plan['steps'][0]['request_phrase_identity_facts'] == [proof]
            return plan
        result = await run(model, question, '{}', 'test', SCHEMA, 500, finalize,
                           initial_proofs=[proof])
        assert len(calls) == 1
        assert result['steps'][0]['constraints'] == [constraint]
        assert result['steps'][0]['request_phrase_identity_facts'] == [proof]
    asyncio.run(check())
