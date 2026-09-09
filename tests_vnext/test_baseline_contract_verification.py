"""Reach current lifecycle contracts without disabling their safety gates.

These supplement the preserved historical fixtures; every provider is mocked.
"""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import anthropic
import pytest

from pankagent_vnext.config import Settings
from pankagent_vnext.llm import ClaudeGateway
from test_llm import rejection
from test_plan_preview import PreviewGraph, multi_plan
from test_runtime import Gateway, new_plan, service, wait_state


def test_oversized_provider_plan_returns_scope_recovery_and_settles_usage(tmp_path):
    async def scenario():
        gateway = ClaudeGateway(Settings(state_dir=tmp_path, anthropic_key='synthetic-test-key'))
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {'input_tokens': 10, 'output_tokens': 10}),
                content=[SimpleNamespace(type='tool_use', name='record_plan', input={
                    'interpreted_question': 'Inspect the recorded gene evidence.',
                    'steps': [{'id': str(i), 'question': 'Inspect gene evidence.', 'depends_on': []} for i in range(13)]})])
        gateway.client.messages.create = create
        try:
            plan = await gateway.plan('Inspect the recorded gene evidence.', [])
            assert plan['steps'] == []
            assert plan['proposal_issue'] == 'plan_too_large'
            assert 'narrow' in plan['clarification'].lower()
            assert len(calls) == 1
            assert gateway.budget.snapshot()['pending_calls'] == 0
            assert gateway.budget.snapshot()['spent_usd'] > 0
        finally:
            await gateway.close()
    asyncio.run(scenario())


def test_definitive_stream_rejection_with_usable_evidence_releases_reservation(tmp_path):
    entered = []
    class RejectedStream:
        async def __aenter__(self):
            entered.append(True)
            raise rejection()
        async def __aexit__(self, *args):
            pass
    async def scenario():
        gateway = ClaudeGateway(Settings(state_dir=tmp_path, anthropic_key='synthetic-test-key'))
        gateway.client.messages.stream = lambda **kwargs: RejectedStream()
        evidence = {'s1': {'step_id': 's1', 'status': 'complete', 'nodes': [
            {'id': 'INS', 'labels': ['Gene'], 'properties': {'name': 'INS'}}], 'edges': [], 'rows': []}}
        try:
            with pytest.raises(anthropic.BadRequestError):
                async for _ in gateway.synthesize('Which cell types express INS?', evidence):
                    pass
            assert entered == [True]
            budget = gateway.budget.snapshot()
            assert budget['calls'] == 1
            assert budget['reserved_usd'] == budget['spent_usd'] == budget['pending_calls'] == 0
        finally:
            await gateway.close()
    asyncio.run(scenario())


def test_duplicate_confirmation_and_reconnect_do_not_duplicate_enabled_literature(tmp_path):
    async def scenario():
        async with service(tmp_path) as (client, runtime, gateway, graph, literature):
            created = await new_plan(client)
            responses = await asyncio.gather(*[client.post(f'/v2/plans/{created["plan_id"]}/confirm') for _ in range(4)])
            assert all(response.status_code == 202 for response in responses)
            run = await wait_state(client, created['run_id'], {'completed'})
            assert run['plan']['literature'] is True
            assert graph.calls == gateway.plans == gateway.syntheses == literature.calls == 1
            events = runtime.store.events_after(created['run_id'], 0)
            response = await client.get(created['events_url'], headers={'Last-Event-ID': str(events[-2]['sequence'])})
            assert response.status_code == 200
            assert response.text.count('data: ') == 1
            assert graph.calls == gateway.plans == gateway.syntheses == literature.calls == 1
            followup = await new_plan(client, 'What about GCG?', session_id=created['session_id'])
            assert followup['run_id'] != created['run_id']
            assert gateway.histories[-1] == [{'role': 'user', 'content': 'Which cell types express INS?'},
                                            {'role': 'assistant', 'content': run['graph_answer']}]
    asyncio.run(scenario())


@pytest.mark.parametrize('timeout', [False, True])
def test_confirmed_failure_retains_valid_preview_and_stops_without_false_success(tmp_path, timeout):
    async def scenario():
        graph = PreviewGraph()
        plan = multi_plan(dependent=True)
        plan['steps'] = plan['steps'][:2]
        plan['steps'][1]['purpose'] = 'context'  # optional late work does not invalidate checked primary readiness
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=plan), run_timeout=.08) as (client, runtime, gateway, graph, literature):
            created = await new_plan(client)
            ready = (await client.get(created['plan_url'])).json()
            assert ready['preview']['confirmation_eligible'] is True
            # Preserve the checked primary preview; expire only optional context.
            # Primary expiry must reject confirmation (covered by test_query_ready_gate).
            saved = runtime.store.get(created['run_id'])
            cache = deepcopy(saved['preview_cache'])
            cache['step_completed_epochs']['s2'] = 0
            runtime.store.update(created['run_id'], preview_cache=cache)
            if timeout:
                graph.block_step = 's2'
            else:
                graph.outcomes['s2'] = [ConnectionError('PRIVATE_TEST_ERROR')]
            response = await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            assert response.status_code == 202
            run = await wait_state(client, created['run_id'], {'partial', 'failed', 'completed'})
            assert run['status'] == 'partial'
            assert run['evidence']['nodes']
            assert run['evidence']['steps'][0]['status'] == 'complete'
            assert run['evidence']['preview_reuse']['reused_step_ids'] == ['s1']
            assert graph.previous[-1]['s1']['status'] == 'complete'
            assert graph.step_calls['s1'] == 1
            assert 'PRIVATE_TEST_ERROR' not in json.dumps(run)
            assert literature.calls == 0
            if timeout:
                assert run['error']['category'] == 'timeout'
                assert graph.cancelled == 1
                assert gateway.syntheses == 0
                assert run['evidence']['completeness'] == 'partial'
                assert run['evidence']['retrieval']['completeness'] == 'partial'
                assert run['evidence']['steps'][1]['step_id'] == 's2'
                assert run['evidence']['steps'][1]['execution_status'] == 'timed_out'
                assert run['evidence']['steps'][1]['attempted'] is True
            else:
                assert run['evidence']['steps'][1]['status'] == 'failed'
                assert run['evidence']['completeness'] == 'partial'
            events = runtime.store.events_after(created['run_id'], 0)
            assert events[-1]['type'] == 'terminal'
            assert events[-1]['payload']['status'] == 'partial'
    asyncio.run(scenario())


@pytest.mark.parametrize('profile', [True, False])
def test_existing_citation_persistence_checks_with_current_literature_fixture(monkeypatch, tmp_path, profile):
    import test_answer_synthesis as original
    async def available_literature(self, *args):
        return {'status': 'complete', 'perspectives': []}
    monkeypatch.setattr(original.UnrequestedLiterature, 'search', available_literature)
    # Retain all original citation, persistence, SSE, cost and single-call assertions.
    if profile:
        original.test_answer_profile_persists_and_replays_once_with_citation_filter(monkeypatch, tmp_path, False)
    else:
        original.test_missing_model_references_get_only_supplied_graph_evidence_footer(
            monkeypatch, tmp_path, ['complete', 'empty', 'complete'],
            ['The requested measurements are shown.'], [1, 3], 'completed')
