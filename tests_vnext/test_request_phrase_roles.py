from copy import deepcopy
import asyncio
import hashlib

import pytest

from pankagent_vnext.semantic_decision import (
    apply_phrase_roles, covers, effective_scope, phrase_roles, record, scope_text,
)
from pankagent_vnext.semantic_registry import resolve, _unresolved_tissue_role, _unresolved_assay_role


VOCAB = {'inventory_complete': True, 'inventory_sha256': 'fixture',
         'sources': [], 'donor_sources': [], 'sample_sources': [],
         'modalities': ['scRNA-seq', 'snMultiomics'],
         'tissues': [{'id': 'tissue1', 'name': 'spleen'}]}


def step(question, roles=(), constraints=()):
    item = {'id': 's1', 'question': question, 'relation_types': [],
            'constraints': deepcopy(list(constraints)), 'depends_on': [], 'complete': True,
            'semantic_request': {'source': 'user_request', 'question': question}}
    item['model_scope_decision'] = record(item, question, phrase_roles(question, list(roles)))
    return item


def assay(value='scRNA-seq', operator='='):
    return {'entity_type': 'Sample_node', 'property': 'data_modality',
            'operator': operator, 'value': value}


def test_example_is_not_assay_filter_and_raw_question_is_preserved():
    question = 'Briefly tell me how many kinds of samples are available (like scRNAseq).'
    value = step(question, [{'text': 'scRNAseq', 'role': 'example'}], [assay()])
    result = resolve(value, VOCAB, 'PanKgraph_08_04')
    assert not result['semantic_issues']
    assert not result['constraints']
    assert result['sample_requirements']['modality_groups'] == []
    assert result['question'] == result['semantic_request']['question'] == question
    assert result['request_scope_changes'][0]['removed_constraints'] == [assay()]
    assert value['constraints'] == [assay()]


def test_of_samples_is_not_an_unknown_tissue_but_named_unknown_tissue_stays_blocked():
    assert not _unresolved_tissue_role('What kinds of samples are available?', VOCAB, [])
    assert _unresolved_tissue_role('Find mysterytissue samples', VOCAB, [])
    assert _unresolved_tissue_role('Find samples from mysterytissue', VOCAB, [])


@pytest.mark.parametrize('question', [
    'What sample types are recorded?', 'Which sample types are recorded?',
    'Describe the different sample types.', 'Which kinds of samples are recorded?',
    'What sample categories are available?', 'Show various samples.',
])
def test_descriptive_sample_question_words_do_not_become_tissues(question):
    assert not _unresolved_tissue_role(question, VOCAB, [])


@pytest.mark.parametrize('question', [
    'Which mysterytissue samples are recorded?',
    'What samples from mysterytissue are recorded?',
    'What mysterytissue sample types are recorded?',
    'Which samples from mysterytissue are recorded?',
])
def test_descriptive_question_still_preserves_unknown_named_tissue(question):
    assert _unresolved_tissue_role(question, VOCAB, [])


@pytest.mark.parametrize('question', [
    'What experimental assay types are represented in PanKgraph?',
    'Which assay types are recorded?', 'Which data modality categories are available?',
    'What modality kinds exist?', 'List the recorded assay types.',
    'Describe experimental assay types in the graph.',
])
def test_assay_category_question_does_not_invent_an_unresolved_assay(question):
    assert not _unresolved_assay_role(question, False)


@pytest.mark.parametrize('question', [
    'Find samples with assay MysterySeq.',
    'Find samples with assay type MysterySeq.',
    'Which assay type is MysterySeq?', 'Show assay types = MysterySeq.',
    'What experimental assay types are represented for MysterySeq samples?',
    'List recorded assay types using MysterySeq.',
    'What assay types are recorded; find samples with assay MysterySeq.',
])
def test_assay_category_projection_does_not_erase_unknown_actual_assay(question):
    assert _unresolved_assay_role(question, False)


def test_exact_assay_type_overview_resolves_without_unrequested_assay():
    raw_question = 'What experimental assay types are represented in PanKgraph?'
    value = step(raw_question)
    value['question'] = 'Retrieve the distinct recorded sample assay types.'
    result = resolve(value, VOCAB, 'PanKgraph_08_04')
    assert not result['semantic_issues'], result['semantic_issues']
    assert result['constraints'] == result['sample_requirements']['modality_groups'] == []
    assert result['semantic_registry']['donor_required'] is False


def test_sample_type_overview_preserves_explicit_source_and_stage_without_tissue():
    from test_sample_scope_recovery import VOCAB as clinical_vocab
    question = 'What sample types are recorded for HPAP stage 3 donors?'
    value = step(question, constraints=[
        {'entity_type':'donor','property':'data_source','operator':'=','value':'HPAP'},
        {'entity_type':'donor','property':'t1d_stage','operator':'=','value': next(v for v in clinical_vocab['stages'] if v.startswith('Stage 3:'))}])
    value['relation_types'] = ['HAS_SAMPLE']
    result = resolve(value, {**clinical_vocab, 'donor_sources':['HPAP'], 'inventory_sha256':'fixture-current'}, 'PanKgraph_08_04')
    assert not result['semantic_issues'], result['semantic_issues']
    assert {c['property'] for c in result['constraints']} == {'data_source', 't1d_stage'}
    assert all(c['entity_type'] == 'donor' for c in result['constraints'])
    assert next(c['value'] for c in result['constraints'] if c['property'] == 'data_source') == 'HPAP'
    assert result['sample_requirements']['modality_groups'] == []


@pytest.mark.parametrize('example_text', ['scRNAseq', 'like scRNAseq...'])
def test_exact_reported_question_prepares_without_example_filter(example_text):
    question = 'briefly tell me how many kinds of samples available in pankgraph? (like scRNAseq...)'
    result = resolve(step(question, [{'text': example_text, 'role': 'example'}]),
                     VOCAB, 'PanKgraph_08_04')
    assert not result['semantic_issues'] and not result['constraints']


def test_real_assay_tissue_and_exclusion_survive_example_cancellation():
    question = 'Find only scRNA-seq samples from spleen, excluding multiome assays; display annotations like library names.'
    value = step(question, [{'text': 'scRNA-seq', 'role': 'filter'},
                            {'text': 'spleen', 'role': 'filter'},
                            {'text': 'library names', 'role': 'example'}],
                 [assay(), assay('snMultiomics', '!='), {'entity_type':'anatomical_structure','property':'id','operator':'=','value':'tissue1'}])
    result = resolve(value, VOCAB, 'PanKgraph_08_04')
    assert not result['semantic_issues'], result['semantic_issues']
    assert any(c['property'] == 'id' and c['value'] == 'tissue1' for c in result['constraints'])
    assert assay() in result['constraints']
    assert assay('snMultiomics', '!=') in result['constraints']
    assert result['semantic_registry']['scope_authority'] == 'planner'


@pytest.mark.parametrize('question,text', [
    ('Find only scRNA-seq samples', 'scRNA-seq'),
    ('Find only: scRNA-seq samples', 'scRNA-seq'),
    ('Find samples from spleen', 'spleen'),
    ('Find samples from spleen', 'from spleen'),
    ('Find samples excluding multiome', 'multiome'),
    ('Find samples without multiome', 'without multiome'),
    ('Find donors with age > 50', 'age > 50'),
])
def test_model_role_is_not_overruled_by_fixed_filter_words(question, text):
    assert phrase_roles(question, [{'text': text, 'role': 'example'}])[0]['role'] == 'example'


@pytest.mark.parametrize('question,text', [
    ('Find scRNA-seq samples only.', 'scRNA-seq'),
    ('Find scRNA-seq samples from spleen.', 'scRNA-seq'),
    ('Find samples excluding both scRNA-seq and snMultiomics.', 'scRNA-seq'),
    ('Find only assays like scRNA-seq.', 'scRNA-seq'),
    ('Find assays like scRNA-seq only.', 'scRNA-seq'),
    ('Describe assays like scRNA-seq. Find snMultiomics samples.', 'snMultiomics'),
    ('Describe assays (like scRNAseq...). Find snMultiomics samples.', 'snMultiomics'),
    ('Describe assays like scRNAseq... Find snMultiomics samples.', 'like scRNAseq... Find snMultiomics samples'),
    ('Please say scRNA-seq.', 'scRNA-seq'),
    ('Find only assay types, say scRNA-seq.', 'scRNA-seq'),
])
def test_model_example_role_does_not_require_python_keyword_evidence(question, text):
    assert phrase_roles(question, [{'text': text, 'role': 'example'}])[0]['role'] == 'example'


@pytest.mark.parametrize('question,text', [
    ('What sample types are recorded, like scRNA-seq?', 'scRNA-seq'),
    ('What sample types are recorded (for example scRNA-seq)?', 'for example scRNA-seq'),
    ('Describe sample types, e.g. scRNA-seq.', 'scRNA-seq'),
    ('Describe sample types such as scRNA-seq.', 'scRNA-seq'),
    ('briefly tell me how many kinds of samples available in pankgraph? (like scRNAseq...)', 'like scRNAseq...'),
    ('Describe evidence for genes such as MDA-5.', 'MDA-5'),
    ('Describe evidence for genes (like HLA-DRA…).', 'like HLA-DRA…'),
    ('What sample types are available, say scRNA-seq?', 'scRNA-seq'),
    ('What sample types are available (say scRNA-seq)?', 'say scRNA-seq'),
])
def test_example_cues_support_the_same_normal_request_roles(question, text):
    assert phrase_roles(question, [{'text': text, 'role': 'example'}])[0]['role'] == 'example'


def test_repeated_example_and_filter_occurrences_keep_real_filter():
    question = 'Describe assays like scRNA-seq; find only scRNA-seq samples.'
    roles = [{'text': 'scRNA-seq', 'role': 'example', 'occurrence_index': 0},
             {'text': 'scRNA-seq', 'role': 'filter', 'occurrence_index': 1}]
    value = step(question, roles, [assay()])
    assert apply_phrase_roles(value, question)['constraints'] == [assay()]
    assert 'only scRNA-seq' in scope_text(value, question)
    assert covers(value, 0, assay(), question)


@pytest.mark.parametrize('roles', [
    [{'text': 'missing', 'role': 'example'}],
    [{'text': 'INS', 'role': 'example'}],
    [{'text': 'INS', 'role': 'example', 'occurrence_index': True}],
    [{'text': 'INS', 'role': 'example', 'occurrence_index': 4}],
    [{'text': 'INS', 'role': 'other', 'occurrence_index': 0}],
    [{'text': 'INS', 'role': 'example', 'occurrence_index': 0},
     {'text': 'INS', 'role': 'filter', 'occurrence_index': 0}],
])
def test_invalid_or_overlapping_roles_fail_closed(roles):
    with pytest.raises(ValueError):
        phrase_roles('INS and INS', roles)


def test_roles_cannot_be_reused_for_another_question_or_step():
    question = 'Show assays like scRNA-seq.'
    value = step(question, [{'text': 'scRNA-seq', 'role': 'example'}], [assay()])
    assert apply_phrase_roles(value, question)['constraints'] == []
    assert not covers(value, 0, assay(), question)
    assert effective_scope(question + ' Updated.', value) == question + ' Updated.'
    changed = deepcopy(value); changed['id'] = 'another'
    assert effective_scope(question, changed) == question


def test_background_and_output_roles_cannot_erase_requested_topics():
    question = 'Describe INS. Return PIP as background context.'
    value = step(question, [{'text': 'PIP', 'role': 'output'},
                           {'text': 'background context', 'role': 'background'}])
    compiler_question = effective_scope(question, {'steps': [value]})
    assert 'PIP' in compiler_question
    assert 'background context' in compiler_question
    assert len(compiler_question) == len(question)


def test_real_gateway_cancels_example_constraint_without_identity_trace_failure():
    from test_planning_compiler_gateway import gateway_for
    async def check():
        question = 'What kinds of samples are available, like scRNAseq?'
        proposal = {'interpreted_question': question, 'clarification': None,
                    'request_phrase_roles': [{'text': 'scRNAseq', 'role': 'example'}],
                    'steps': [{'id': 's1', 'question': '', 'relation_types': [],
                               'constraints': [assay()], 'depends_on': [], 'complete': True,
                               'evidence_combination': 'independent'}]}
        g, calls = gateway_for(lambda _: deepcopy(proposal))
        grounding = {'status': 'ready', 'identity': {'graph_release': 'PanKgraph_08_04'},
                     'sample_terminology': deepcopy(VOCAB), 'mentions': []}
        plan = await g.plan(question, [], grounding=grounding)
        assert len(calls) == 1, plan
        assert not plan.get('clarification') and not plan.get('proposal_issue'), plan
        task = plan['steps'][0]
        assert task['constraints'] == []
        assert task['request_scope_changes'][0]['removed_constraints'] == [assay()]
        assert resolve(task, VOCAB, 'PanKgraph_08_04')['constraints'] == []
    asyncio.run(check())


def test_scope_compiler_proof_retains_raw_execution_authorization_hash():
    from test_requested_scope_compile import grounding, plan, step as query_step
    from pankagent_vnext.planning_requirements import compile_requested_scope
    from pankagent_vnext.semantic_registry import attach_request_authorizations
    question = 'In pancreas, what molecular QTL evidence is recorded for GCLC? Explain annotations like tissue names.'
    value = query_step()
    value['model_scope_decision'] = record(value, question,
        phrase_roles(question, [{'text': 'tissue names', 'role': 'example'}]))
    compiled, issue = compile_requested_scope(effective_scope(question, value), grounding(), plan(value))
    assert issue is None
    value = compiled['steps'][0]
    value['graph_version'] = 'PanKgraph_08_04'
    value['semantic_request'] = {'source': 'user_request', 'question': question}
    result = attach_request_authorizations(value)
    assert not result.get('semantic_issues')
    assert any(binding['authorization_kind'] == 'verified_request_qtl_tissue_compilation'
               for binding in result['request_filter_bindings'])
    assert all(binding['request_sha256'] == hashlib.sha256(question.encode()).hexdigest()
               for binding in result['request_filter_bindings'])


def test_roles_are_server_recorded_before_finalize_and_survive_forged_helper_metadata():
    from test_claude_led_planning import gateway, SCHEMA
    from pankagent_vnext.planning_session import run

    async def check():
        question = 'What kinds of samples are available, like scRNA-seq?'
        selected = {'id': 's1', 'question': question, 'constraints': [assay()]}
        proposal = {'interpreted_question': question, 'steps': [selected], 'clarification': None,
                    'request_phrase_roles': [{'text': 'scRNA-seq', 'role': 'example'}]}
        g, calls = gateway([('record_plan', proposal)])
        def finalize(plan, choices):
            assert plan['steps'][0]['constraints'] == []
            assert 'scRNA-seq' not in effective_scope(question, plan)
            plan['steps'][0]['model_scope_decision'] = {'source': 'forged'}
            return plan
        result = await run(g, question, '{}', 'test', SCHEMA, 500, finalize)
        assert len(calls) == 1
        assert result['steps'][0]['constraints'] == []
        assert 'scRNA-seq' not in effective_scope(question, result)
        assert 'request_phrase_roles' in calls[0]['tools'][0]['input_schema']['properties']
    asyncio.run(check())


def test_compiled_descendants_keep_roles_without_gaining_model_filter_authorization():
    from test_claude_led_planning import gateway, SCHEMA
    from pankagent_vnext.planning_session import run

    async def check():
        question = 'Describe sample types like scRNA-seq.'
        proposal = {'interpreted_question': question, 'clarification': None,
                    'request_phrase_roles': [{'text': 'scRNA-seq', 'role': 'example'}],
                    'steps': [{'id': 'parent', 'question': question, 'constraints': []}]}
        model, _ = gateway([('record_plan', proposal)])
        derived_filter = {'entity_type': 'Gene', 'property': 'id', 'operator': '=', 'value': 'unreviewed'}
        def finalize(plan, choices):
            child = deepcopy(plan['steps'][0])
            child.update(id='parent_derived', constraints=[derived_filter])
            return {**plan, 'steps': [child]}
        result = await run(model, question, '{}', 'test', SCHEMA, 500, finalize)
        child = result['steps'][0]
        assert 'scRNA-seq' not in effective_scope(question, child)
        assert child['model_scope_decision']['step_id'] == child['id']
        assert child['constraints'] == [derived_filter]
        assert not covers(child, 0, derived_filter, question)
    asyncio.run(check())


def test_scalar_category_plan_uses_real_preparation_without_required_relationships():
    from unittest.mock import AsyncMock
    from test_claude_led_planning import graph
    from test_planning_compiler_gateway import gateway_for
    from pankagent_vnext.graph import validate_cypher

    async def check():
        question = 'Briefly tell me how many kinds of samples are available in PanKgraph, like scRNA-seq.'
        proposal = {'interpreted_question': 'Which recorded sample types are available?',
                    'clarification': None,
                    'request_phrase_roles': [{'text': 'like scRNA-seq', 'role': 'example'}],
                    'steps': [{'id': 'samples',
                        'question': 'Which distinct recorded sample assay types exist among all samples in PanKgraph?',
                        'relation_types': [], 'constraints': [], 'depends_on': [],
                        'complete': True, 'evidence_combination': 'independent'}]}
        gateway, calls = gateway_for(lambda _: deepcopy(proposal))
        adapter = graph([])
        adapter.semantic_vocabulary = AsyncMock(return_value=deepcopy(VOCAB))
        grounding = {'status': 'ready', 'identity': {'graph_release': 'PanKgraph_08_04'},
                     'sample_terminology': deepcopy(VOCAB), 'mentions': [{
                         'requested': 'scrna seq', 'state': 'resolved', 'candidates': [{
                             'id': 'scRNA-seq', 'name': 'scRNA-seq', 'entity_type': 'data_modality'}]}]}
        async def prepare(plan):
            return await adapter.prepare_plan(plan, AsyncMock())
        result = await gateway.plan(question, [], grounding=grounding, preparer=prepare)
        assert len(calls) == 1, result
        assert not result.get('clarification') and not result.get('proposal_issue'), result
        prepared = result['steps'][0]
        assert prepared['constraints'] == prepared['relation_types'] == []
        assert prepared['semantic_registry']['donor_required'] is False
        assert validate_cypher('MATCH (s:Sample_node) RETURN DISTINCT s.data_modality AS data_modality', prepared) == []
        invented = 'MATCH (s:Sample_node) WHERE s.data_modality = "scRNA-seq" RETURN DISTINCT s.data_modality AS data_modality'
        assert validate_cypher(invented, prepared)
    asyncio.run(check())


def test_repaired_plan_retains_bounded_preparation_rejection_reasons():
    from test_claude_led_planning import gateway, SCHEMA
    from pankagent_vnext.planning_session import run

    async def check():
        question = 'What sample types are recorded?'
        proposal = {'interpreted_question': question, 'clarification': None,
                    'steps': [{'id': 'samples', 'question': question,
                               'relation_types': [], 'constraints': []}]}
        model, calls = gateway([('record_plan', proposal)])
        prepared = 0
        async def prepare(plan):
            nonlocal prepared
            prepared += 1
            if prepared == 1:
                plan['steps'][0]['semantic_issues'] = ['example_scope_not_removed', 'x' * 900]
                plan['steps'][0]['private_helper_payload'] = 'do not copy this'
            return plan
        result = await run(model, question, '{}', 'test', SCHEMA, 500,
                           lambda p, _: deepcopy(p), preparer=prepare)
        assert len(calls) == 2
        history = result['diagnostic_history']
        assert history[0]['reason'] == 'preparation_failed'
        assert history[0]['preparation_tasks'] == [{
            'step_id': 'samples', 'relation_types': [], 'constraint_count': 0,
            'reasons': ['example_scope_not_removed', 'x' * 500]}]
        assert 'private_helper_payload' not in str(history)
    asyncio.run(check())
