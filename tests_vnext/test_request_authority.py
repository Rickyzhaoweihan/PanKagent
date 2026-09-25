"""Raw wording and helper authority through real orchestration, without paid calls."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pankagent_vnext import request_context as ctx
from pankagent_vnext.graph import GraphAdapter, validate_cypher
from pankagent_vnext.graph_contract import generation_request
from pankagent_vnext.investigations import group_plan
from pankagent_vnext.semantic_registry import donor_intent, semantic_intent
from pankagent_vnext.planning_session import run, helper_payload
from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.config import Settings
from tests_vnext.test_claude_led_planning import gateway, graph
from tests_vnext.test_answer_synthesis import MockClaude, detection_evidence
from tests_vnext.test_runtime import service, Gateway, wait_state

RAW = '  What is T1D?\n'
EFFECTIVE = 'Describe type 1 diabetes as recorded in the graph.'
CONTEXT = {'original_raw_question': RAW, 'current_raw_input': RAW,
           'revision_instruction': None, 'effective_question': EFFECTIVE, 'raw_status': 'available'}
STEP = {'id': 's1', 'question': EFFECTIVE, 'relation_types': [], 'depends_on': [],
        'constraints': [{'entity_type': 'disease', 'property': 'id', 'operator': '=', 'value': 'MONDO_0005147'}],
        'complete': True, 'evidence_combination': 'independent'}
PLAN = {'interpreted_question': EFFECTIVE, 'steps': [STEP], 'clarification': None}


def test_context_never_fabricates_historical_raw_and_revision_wins():
    old = ctx.from_run({'question': 'derived text'}, {})
    assert old['original_raw_question'] is old['current_raw_input'] is None
    assert old['raw_status'] == 'unavailable_historical'
    revised = ctx.from_run({'question': 'Count all HPAP donors'}, {
        'original_question': 'Count HPAP stage 3 donors', 'current_raw_input': '  Remove stage. ',
        'revision_instruction': '  Remove stage. ', 'parent_run_id': 'old'})
    assert revised['original_raw_question'] == 'Count HPAP stage 3 donors'
    assert revised['current_raw_input'] == revised['revision_instruction'] == '  Remove stage. '
    assert revised['effective_question'] == 'Count all HPAP donors'
    assert 'supersedes removed requirements' in ctx.AUTHORITY


def test_context_concurrent_requests_and_server_owned_plan_fields():
    async def worker(word):
        token = ctx.bind({**CONTEXT, 'current_raw_input': word})
        try:
            await asyncio.sleep(0)
            plan = ctx.attach({**PLAN, 'request_context': {'original_raw_question': 'model spoof'}}, ctx.current())
            assert plan['request_context']['current_raw_input'] == word
            assert plan['steps'][0]['request_context']['original_raw_question'] == RAW
        finally:
            ctx.reset(token)
    async def check():
        await asyncio.gather(worker('first'), worker('second'))
        assert ctx.current()['raw_status'] == 'unavailable_historical'
    asyncio.run(check())


@pytest.mark.parametrize('question', ['What is T1D?', 'Define type 1 diabetes.', 'What does T2D mean in this graph?'])
def test_disease_definition_has_no_implicit_cohort_scope(question):
    step = {**deepcopy(STEP), 'question': question}
    assert not donor_intent(step) and not semantic_intent(step)
    # Even stale helper metadata cannot make this an inventory task.
    step['semantic_registry'] = {'donor_required': True}
    query = "MATCH (d:disease) WHERE d.id = 'MONDO_0005147' RETURN d"
    assert validate_cypher(query, step) == []
    unrelated = query.replace('(d:disease)', '(d:disease)-[r:HAS_DONOR]->(n:donor)').replace('RETURN d', 'RETURN d,r,n')
    assert 'unrequested_mandatory_relation:HAS_DONOR' in validate_cypher(unrelated, step)
    assert 'Required connected schema paths' not in generation_request(step, question)
    assert 'DONOR-ONLY' not in generation_request(step, question)
    assert group_plan({'steps': [step]})['display_groups'][0]['id'] == 'entity'


def test_entity_only_sibling_does_not_inherit_cohort_processing():
    step = {**deepcopy(STEP), 'semantic_request': {'source': 'user_request',
            'question': 'Define T1D and separately count HPAP donors.'}}
    assert not donor_intent(step) and not semantic_intent(step)
    assert group_plan({'steps': [{**STEP, 'constraints': [{'entity_type': 'donor',
        'property': 't1d_stage', 'value': 'Stage 1'}]}]})['display_groups'][0]['id'] == 'cohort'


@pytest.mark.parametrize('question,relations', [
    ('Count T1D donors', ['HAS_DONOR']), ('Count HPAP stage 1 donors', []),
    ('Find samples for T1D donors', ['HAS_SAMPLE']), ('Find T1D samples', ['HAS_SAMPLE'])])
def test_real_cohort_intent_survives(question, relations):
    assert donor_intent({'question': question, 'constraints': [], 'relation_types': relations})


def test_advice_can_be_discarded_through_actual_preparation_and_repair():
    async def check():
        g = graph([{'id': 'MONDO_0005147', 'name': 'type 1 diabetes', 'labels': ['disease']}])
        g.semantic_vocabulary = AsyncMock(side_effect=AssertionError('Disease definition must not ask for donor vocabulary'))
        accepted = deepcopy(PLAN)
        accepted['steps'][0]['constraints'][0].update(property='name', value='type 1 diabetes')
        accepted['advisory_decisions'] = [{'rule_id': 'M04.local_draft', 'disposition': 'discarded'}]
        proposals = 0
        async def prepare(plan):
            nonlocal proposals
            proposals += 1
            if proposals == 1:
                return {**plan, 'steps': [{**plan['steps'][0], 'runtime_binding_issues': ['temporary_binding_failure']}]}
            return await g.prepare_plan(plan, AsyncMock())
        draft = {**PLAN, 'steps': [{**STEP, 'relation_types': ['HAS_DONOR']}]}
        model, calls = gateway([('record_plan', accepted)])
        token = ctx.bind(CONTEXT)
        try:
            result = await run(model, EFFECTIVE, json.dumps({'advisory_suggestions': {'local_draft': draft}}),
                'Plan', {'type': 'object', 'properties': {}}, 1600,
                lambda p, _: deepcopy(p), preparer=prepare)
        finally:
            ctx.reset(token)
        assert not result.get('clarification'), result.get('proposal_issue', result)
        assert len(calls) == 2
        assert result['steps'][0]['relation_types'] == []
        assert not result['steps'][0].get('semantic_registry')
        assert result['tool_suggestion_decisions'][0]['disposition'] == 'discarded'
        assert result['tool_suggestion_decisions'][1]['disposition'] == 'discarded_or_replaced'
        assert result['steps'][0]['request_context'] == CONTEXT
        for call in calls:
            assert json.loads(call['messages'][0]['content'])['request_context'] == CONTEXT
        g.semantic_vocabulary.assert_not_called()
    asyncio.run(check())


def test_tool_fact_advice_and_diagnostic_sections():
    result = helper_payload({'status': 'complete', 'items': [{'reference': 'nodes.disease'}],
                            'interpretation_rules': [{'id': 'rule'}], 'query_patterns': [{'id': 'path'}]}, 'inspect_schema')
    assert result['verified_facts']['items'] == [{'reference': 'nodes.disease'}]
    assert result['advisory_suggestions']['query_patterns'] == [{'id': 'path'}]
    assert not result['diagnostics']
    unavailable = helper_payload({'status': 'unavailable', 'diagnostic': 'E01'}, 'resolve_entities')
    assert unavailable['diagnostics'][0] == {'rule_id': 'resolve_entities', 'status': 'unavailable', 'reason': 'E01', 'blocking': False}


def test_every_gateway_input_including_stream_carries_raw_wording(tmp_path):
    async def check():
        g = ClaudeGateway(Settings(state_dir=tmp_path, anthropic_key='fake'))
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            tool = kwargs['tool_choice']['name']
            values = {'record_plan': PLAN, 'interpret_revision': {'new_question': EFFECTIVE,
                      'execution': 'parallel_extend', 'reason': 'explicit revision', 'recommended_question': ''},
                      'repair_query': {'cypher': "MATCH (d:disease) RETURN d"},
                      'verify_plan': {'approved': True, 'issues': []}}
            # Keep this test independent of the review tool's display name.
            payload = values.get(tool, {'approved': True, 'issues': []})
            return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {}), stop_reason='end_turn',
                                   content=[SimpleNamespace(type='tool_use', name=tool, input=deepcopy(payload))])
        g.client.messages.create = create
        token = ctx.bind(CONTEXT)
        try:
            plan = await g.plan(EFFECTIVE, [])
            assert not plan.get('clarification'), plan
            await g.interpret_revision(EFFECTIVE, 'Keep definition only', plan)
            await g.repair_cypher(plan['steps'][0], 'EXPANDED GPU PROMPT', ['syntax'], 'MATCH bad')
            await g.review_grounded_plan(EFFECTIVE, plan, {})
            for call in calls:
                body = json.loads(call['messages'][0]['content'])
                assert body['request_context'] == CONTEXT
                assert body['request_authority'] == ctx.AUTHORITY
            mock = MockClaude(['Definition ', '[G1].'])
            g.client.messages.stream = mock.stream
            prepared = g.prepare_answer(EFFECTIVE, detection_evidence())
            assert json.loads(prepared.body)['request_context'] == CONTEXT
            text = ''.join([part async for part in g.synthesize(EFFECTIVE, detection_evidence(), prepared=prepared)])
            assert text == 'Definition [G1].'
            assert json.loads(mock.stream_calls[0]['messages'][0]['content'])['request_context'] == CONTEXT
            assert len(calls) == 4 and len(mock.stream_calls) == 1
        finally:
            ctx.reset(token)
            await g.close()
    asyncio.run(check())


def test_runtime_revision_saved_refresh_and_answer_keep_raw(tmp_path):
    class Capture(Gateway):
        def __init__(self):
            super().__init__()
            self.contexts = []
        async def plan(self, question, history):
            self.contexts.append(('plan', ctx.current()))
            return {**deepcopy(PLAN), 'interpreted_question': question}
        async def interpret_revision(self, question, instruction, parent):
            self.contexts.append(('revision', ctx.current()))
            return {'new_question': EFFECTIVE, 'execution': 'chain_restart', 'reason': 'Definition only', 'recommended_question': ''}
        async def synthesize(self, question, evidence):
            self.contexts.append(('answer', ctx.current()))
            yield 'Definition [G1].'
    async def check():
        g = Capture()
        async with service(tmp_path, gateway=g) as (client, runtime, *_):
            original = '  What is T1D and how many donors are linked?\n'
            created = (await client.post('/v2/plans', json={'question': original})).json()
            prior = await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            instruction = '  Remove donor retrieval; define T1D only.\n'
            revised = (await client.post('/v2/plans/'+prior['plan_id']+'/revise', json={
                'question': prior['question'], 'revision_mode': 'instruction', 'revision_instruction': instruction})).json()
            saved = await wait_state(client, revised['run_id'], {'awaiting_confirmation'})
            context = saved['plan']['request_context']
            assert context['original_raw_question'] == original
            assert context['current_raw_input'] == context['revision_instruction'] == instruction
            assert context['effective_question'] == EFFECTIVE
            assert saved['plan']['steps'][0]['request_context'] == context
            assert runtime.store.audit_metadata(saved['run_id'])['current_raw_input'] == instruction
            response = await client.post('/v2/plans/'+saved['plan_id']+'/confirm', json={})
            assert response.status_code == 202, response.text
            final = await wait_state(client, saved['run_id'], {'completed', 'failed'})
            assert final['status'] == 'completed', final
            assert g.contexts[-1] == ('answer', context)
            assert g.contexts[0][1]['original_raw_question'] == original
            assert g.contexts[1][1]['current_raw_input'] == instruction
    asyncio.run(check())


def test_large_raw_context_is_retained_during_formatter_input_compaction(tmp_path):
    from tests_vnext.test_oversized_answer_context import large_evidence
    from pankagent_vnext.evidence_context import MAX_BYTES
    async def check():
        g = ClaudeGateway(Settings(state_dir=tmp_path, anthropic_key='fake'))
        long_context = {**CONTEXT, 'original_raw_question': '糖' * 5800,
                        'current_raw_input': 'Keep the requested scope. ' * 200}
        token = ctx.bind(long_context)
        try:
            prepared = g.prepare_answer(EFFECTIVE, large_evidence())
            assert json.loads(prepared.body)['request_context'] == long_context
            assert len(prepared.body.encode()) <= MAX_BYTES
        finally:
            ctx.reset(token)
            await g.close()
    asyncio.run(check())
