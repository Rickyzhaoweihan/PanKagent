"""Queries, not a model-written plan, establish confirmation readiness."""
import asyncio
from copy import deepcopy
import time

import pytest

from pankagent_vnext.evidence_status import confirmation_eligible, query_readiness
from test_runtime import Gateway, PLAN, service, wait_state
from test_plan_preview import PreviewGraph, multi_plan


async def create(client, question='Which cell types express INS?'):
    response = await client.post('/v2/plans', json={'question': question})
    assert response.status_code == 202
    return response.json()


def ready_events(runtime, run_id):
    return [item for item in runtime.store.events_after(run_id, 0) if item['type'] in ('plan_validated', 'plan_ready')]


def test_all_primary_checks_execute_before_review_or_confirmation(tmp_path):
    async def scenario():
        graph = PreviewGraph(block_step='s3')
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=multi_plan())) as (client, runtime, gateway, _, literature):
            created = await create(client)
            await asyncio.wait_for(graph.blocked.wait(), 1)
            pending = runtime.store.get(created['run_id'])
            assert pending['status'] == 'planning'
            assert not pending['plan'].get('review_ready')
            assert not ready_events(runtime, created['run_id'])
            assert (await client.post(f"/v2/plans/{created['plan_id']}/confirm")).status_code == 409
            graph.release.set()
            ready = await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            assert graph.calls == 3
            assert ready['preview']['query_readiness']['verified_step_ids'] == ['s1', 's2', 's3']
            assert ready['plan']['review_ready'] is True
            assert [item['type'] for item in ready_events(runtime, created['run_id'])] == ['plan_validated', 'plan_ready']
            assert gateway.syntheses == literature.calls == 0
            assert (await client.post(f"/v2/plans/{created['plan_id']}/confirm")).status_code == 202
            await wait_state(client, created['run_id'], {'completed'})
            assert graph.calls == 3 and gateway.syntheses == 1
    asyncio.run(scenario())


def test_dependencies_checked_in_order_and_reused_without_new_graph_calls(tmp_path):
    async def scenario():
        graph = PreviewGraph()
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=multi_plan(dependent=True))) as (client, runtime, gateway, *_):
            created = await create(client)
            ready = await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            assert graph.step_calls == {'s1': 1, 's2': 1, 's3': 1}
            assert any(previous.get('s1', {}).get('status') == 'complete' for previous in graph.previous)
            assert ready['preview']['query_readiness']['ready']
            replies = await asyncio.gather(*[client.post(f"/v2/plans/{created['plan_id']}/confirm") for _ in range(3)])
            assert all(reply.status_code == 202 for reply in replies)
            complete = await wait_state(client, created['run_id'], {'completed'})
            assert graph.calls == 3 and gateway.syntheses == 1
            assert complete['evidence']['preview_reuse']['retrieved_step_ids'] == []
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['failed', 'partial', ConnectionError('private error')])
def test_one_failed_or_truncated_primary_blocks_entire_plan_without_synthesis(tmp_path, outcome):
    async def scenario():
        graph = PreviewGraph(outcomes={'s2': [outcome]})
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=multi_plan())) as (client, runtime, gateway, _, literature):
            created = await create(client)
            failed = await wait_state(client, created['run_id'], {'failed'})
            assert failed['preview']['confirmation_eligible'] is False
            assert failed['preview']['evidence']['nodes']
            assert failed['preview']['query_readiness']['blocked_step_ids'] == ['s2']
            assert failed['error']['recovery']['retryable'] is (outcome != 'partial')
            if outcome == 'partial':
                assert failed['error']['recovery']['category'] == 'retrieval_limit'
                assert 'more specific query' in failed['error']['recovery']['message']
            assert not ready_events(runtime, created['run_id'])
            assert (await client.post(f"/v2/plans/{created['plan_id']}/confirm")).status_code == 409
            assert gateway.syntheses == literature.calls == 0
    asyncio.run(scenario())


def test_failed_dependency_blocks_child_without_gpu_but_not_independent_checks(tmp_path):
    async def scenario():
        graph = PreviewGraph(outcomes={'s1': ['failed']})
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=multi_plan(dependent=True))) as (client, runtime, *_):
            created = await create(client)
            failed = await wait_state(client, created['run_id'], {'failed'})
            assert graph.step_calls == {'s1': 1, 's3': 1}
            outcomes = {item['step_id']: item for item in failed['preview']['evidence']['steps']}
            assert outcomes['s2']['blocked_by'] == ['s1']
            assert outcomes['s3']['status'] == 'complete'
            assert not ready_events(runtime, created['run_id'])
    asyncio.run(scenario())


def test_optional_context_failure_does_not_block_primary(tmp_path):
    async def scenario():
        graph = PreviewGraph(add_context=True, outcomes={'context': ['failed']})
        async with service(tmp_path, graph=graph) as (client, runtime, *_):
            created = await create(client)
            ready = await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            assert ready['preview']['status'] == 'partial'
            assert ready['preview']['confirmation_eligible'] is True
            assert ready['preview']['query_readiness']['required_step_ids'] == ['s1']
            assert ready['preview']['query_readiness']['blocked_step_ids'] == []
    asyncio.run(scenario())


def test_context_dependency_of_primary_is_required(tmp_path):
    async def scenario():
        plan = multi_plan(dependent=True)
        plan['steps'][0]['purpose'] = 'context'
        graph = PreviewGraph(outcomes={'s1': ['failed']})
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=plan)) as (client, runtime, *_):
            created = await create(client)
            failed = await wait_state(client, created['run_id'], {'failed'})
            assert failed['preview']['query_readiness']['required_step_ids'] == ['s1', 's2', 's3']
            assert graph.step_calls == {'s1': 1, 's3': 1}
    asyncio.run(scenario())


def test_preview_deadline_records_every_unfinished_primary_and_retains_success(tmp_path):
    async def scenario():
        graph = PreviewGraph(block_step='s2')
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=multi_plan(dependent=True)), preview_timeout=.05, grouped_preview_timeout=.05) as (client, runtime, gateway, *_):
            created = await create(client)
            failed = await wait_state(client, created['run_id'], {'failed'})
            assert len(failed['preview']['evidence']['steps']) == 3
            assert failed['preview']['query_readiness']['blocked_step_ids'] == ['s2']
            assert failed['preview']['error']['category'] == 'timeout'
            assert failed['preview']['evidence']['nodes']
            assert not ready_events(runtime, created['run_id'])
            assert gateway.syntheses == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['empty', 'zero_count', 'bounded_top_n'])
def test_valid_zero_matches_zero_count_and_untruncated_bounded_query_are_ready(tmp_path, kind):
    class Results(PreviewGraph):
        async def execute(self, step, previous, emit):
            result = await super().execute(step, previous, emit)
            if kind == 'empty':
                result.update(status='empty', nodes=[])
            elif kind == 'zero_count':
                result.update(nodes=[], rows=[{'donor_count': 0}])
            else:
                result.update(status='partial', truncated=False)
            return result
    async def scenario():
        graph = Results()
        plan = deepcopy(PLAN)
        if kind == 'bounded_top_n':
            plan['steps'][0]['complete'] = False
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=plan)) as (client, runtime, gateway, *_):
            created = await create(client)
            ready = await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            assert ready['preview']['confirmation_eligible'] is True
            assert not ready.get('error')
            assert graph.calls == 1
            await client.post(f"/v2/plans/{created['plan_id']}/confirm")
            done = await wait_state(client, created['run_id'], {'completed', 'partial'})
            assert graph.calls == 1
            if kind == 'zero_count':
                assert done['evidence']['steps'][0]['rows'] == [{'donor_count': 0}]
    asyncio.run(scenario())


def test_empty_dependency_is_verified_without_unrestricted_child_query(tmp_path):
    class Results(PreviewGraph):
        async def execute(self, step, previous, emit):
            result = await super().execute(step, previous, emit)
            result.update(status='empty', nodes=[])
            if step['id'] == 's2':
                result.update(queries=[], validation=[{'valid': True, 'reasons': ['empty_dependency:s1']}])
            return result
    async def scenario():
        plan = multi_plan(dependent=True)
        plan['steps'] = plan['steps'][:2]
        async with service(tmp_path, graph=Results(), gateway=Gateway(plan=plan)) as (client, runtime, *_):
            created = await create(client)
            ready = await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            assert ready['preview']['query_readiness']['derived_empty_step_ids'] == ['s2']
            assert ready['preview']['query_readiness']['no_match_step_ids'] == ['s1', 's2']
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['identity', 'ttl', 'query', 'validation', 'contract', 'plan'])
def test_confirmation_rechecks_exact_preview_and_never_falls_back_to_live_retrieval(tmp_path, change):
    async def scenario():
        graph = PreviewGraph()
        async with service(tmp_path, graph=graph) as (client, runtime, gateway, _, literature):
            created = await create(client)
            await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            run = runtime.store.get(created['run_id'])
            if change == 'identity':
                graph.identity['identity_manifest_sha256'] = 'changed'
            elif change == 'ttl':
                run['preview_cache']['step_completed_epochs']['s1'] = time.time() - 301
                runtime.store.update(run['run_id'], preview_cache=run['preview_cache'])
            elif change == 'plan':
                run['plan']['steps'][0]['question'] = 'Another gene'
                runtime.store.update(run['run_id'], plan=run['plan'])
            else:
                if change == 'query':
                    run['preview']['evidence']['steps'][0]['queries'] = []
                elif change == 'validation':
                    run['preview']['evidence']['steps'][0]['validation'] = [{'valid': False}]
                else:
                    run['preview']['query_readiness']['version'] = 'old'
                runtime.store.update(run['run_id'], preview=run['preview'])
            response = await client.post(f"/v2/plans/{created['plan_id']}/confirm")
            assert response.status_code == 409
            failed = await wait_state(client, created['run_id'], {'failed'})
            assert failed['error']['recovery']['category'] == 'preview_revalidation_required'
            assert graph.calls == 1 and gateway.syntheses == literature.calls == 0
    asyncio.run(scenario())


def test_cancel_during_query_check_never_exposes_confirmable_plan(tmp_path):
    async def scenario():
        graph = PreviewGraph(block_step='s1')
        async with service(tmp_path, graph=graph) as (client, runtime, gateway, *_):
            created = await create(client)
            await asyncio.wait_for(graph.blocked.wait(), 1)
            await client.post(f"/v2/runs/{created['run_id']}/cancel")
            cancelled = await wait_state(client, created['run_id'], {'cancelled'})
            assert not cancelled['plan'].get('review_ready')
            assert not ready_events(runtime, created['run_id'])
            assert gateway.syntheses == 0
    asyncio.run(scenario())


def test_reconnect_does_not_start_new_queries_and_keeps_checked_plan(tmp_path):
    async def scenario():
        graph = PreviewGraph()
        async with service(tmp_path, graph=graph) as (client, runtime, gateway, *_):
            created = await create(client)
            ready = await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            again = (await client.get(created['plan_url'])).json()
            assert again['preview']['query_readiness'] == ready['preview']['query_readiness']
            assert again['plan']['review_ready']
            assert graph.calls == gateway.plans == 1
    asyncio.run(scenario())


def test_claimed_success_without_validation_or_query_cannot_be_ready():
    preview = {'preparation_complete': True, 'evidence': {'steps': [{'step_id': 's1', 'status': 'complete', 'nodes': [{'id': 'a'}]}]}}
    assert not confirmation_eligible(PLAN, preview)
    assert query_readiness(PLAN, preview)['blocked_step_ids'] == ['s1']


@pytest.mark.parametrize('mutate', ['unspecified_truncation', 'unexplained_partial', 'no_cypher', 'missing_execution', 'incomplete_cursor'])
def test_unknown_completeness_and_unexecuted_query_cannot_authorize_confirmation(mutate):
    result = {'step_id': 's1', 'status': 'complete', 'nodes': [{'id': 'x'}], 'edges': [], 'rows': [],
              'truncated': False, 'queries': [{'cypher': 'MATCH (g:Gene) RETURN g'}], 'validation': [{'valid': True}],
              'retrieval_execution': {'completed': True, 'cursor_exhausted': True}}
    if mutate == 'unspecified_truncation':
        result.pop('truncated')
    elif mutate == 'unexplained_partial':
        result['status'] = 'partial'
    elif mutate == 'missing_execution':
        result.pop('retrieval_execution')
    elif mutate == 'incomplete_cursor':
        result['retrieval_execution']['cursor_exhausted'] = False
    else:
        result['queries'] = [{}]
    preview = {'preparation_complete': True, 'evidence': {'steps': [result]}}
    assert not confirmation_eligible(PLAN, preview)


def test_queue_wait_does_not_rerun_already_confirmed_evidence_when_ttl_elapses(tmp_path):
    async def scenario():
        graph = PreviewGraph()
        async with service(tmp_path, graph=graph, max_concurrent=1) as (client, runtime, gateway, *_):
            created = await create(client)
            await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            await runtime.semaphore.acquire()
            try:
                assert (await client.post(f"/v2/plans/{created['plan_id']}/confirm")).status_code == 202
                run = runtime.store.get(created['run_id'])
                assert run['status'] == 'queued'
                run['preview_cache']['step_completed_epochs']['s1'] = time.time() - 301
                runtime.store.update(run['run_id'], preview_cache=run['preview_cache'])
            finally:
                runtime.semaphore.release()
            done = await wait_state(client, created['run_id'], {'completed'})
            assert graph.calls == gateway.syntheses == 1
            assert done['evidence']['preview_reuse']['reused_step_ids'] == ['s1']
    asyncio.run(scenario())


def test_changed_graph_while_queued_blocks_synthesis_and_new_queries(tmp_path):
    async def scenario():
        graph = PreviewGraph()
        async with service(tmp_path, graph=graph, max_concurrent=1) as (client, runtime, gateway, _, literature):
            created = await create(client)
            await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            await runtime.semaphore.acquire()
            try:
                assert (await client.post(f"/v2/plans/{created['plan_id']}/confirm")).status_code == 202
                graph.identity['identity_manifest_sha256'] = 'new-release'
            finally:
                runtime.semaphore.release()
            run = await wait_state(client, created['run_id'], {'partial', 'failed'})
            assert run['error']['recovery']['category'] == 'preview_revalidation_required'
            assert graph.calls == 1 and gateway.syntheses == literature.calls == 0
    asyncio.run(scenario())


def test_independent_preflight_checks_have_at_most_two_active_executions(tmp_path):
    class ConcurrentGraph(PreviewGraph):
        active = 0
        peak = 0
        async def execute(self, step, previous, emit):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(.015)
                return await super().execute(step, previous, emit)
            finally:
                self.active -= 1
    async def scenario():
        graph = ConcurrentGraph()
        plan = multi_plan()
        plan['steps'].extend([{**deepcopy(plan['steps'][0]), 'id': 's'+str(n)} for n in range(4, 7)])
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=plan)) as (client, runtime, gateway, *_):
            created = await create(client)
            ready = await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            assert graph.calls == 6
            assert graph.peak == 2
            assert len(ready['preview']['query_readiness']['verified_step_ids']) == 6
            assert gateway.plans == 1 and gateway.syntheses == 0
    asyncio.run(scenario())


def test_optional_preview_timeout_keeps_checked_primary_eligible(tmp_path):
    async def scenario():
        graph = PreviewGraph(add_context=True, block_step='context')
        async with service(tmp_path, graph=graph, preview_timeout=.05) as (client, runtime, gateway, *_):
            created = await create(client)
            ready = await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            assert ready['preview']['confirmation_eligible'] is True
            assert ready['preview']['query_readiness']['blocked_step_ids'] == []
            assert ready['preview']['evidence']['steps'][1]['purpose'] == 'context'
            assert ready['preview']['evidence']['steps'][1]['status'] == 'failed'
            assert ready['plan']['review_ready'] is True
            assert gateway.syntheses == 0
    asyncio.run(scenario())


def test_optional_timeout_keeps_evidence_citations_in_plan_order(tmp_path):
    class ReorderedGraph(PreviewGraph):
        async def execute(self, step, previous, emit):
            if step['id'] == 's1':
                await asyncio.sleep(.01)
            return await super().execute(step, previous, emit)
    async def scenario():
        graph = ReorderedGraph(block_step='s3')
        plan = multi_plan()
        plan['steps'][2]['purpose'] = 'context'
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=plan), preview_timeout=1) as (client, runtime, gateway, *_):
            created = await create(client)
            # This scenario exercises an in-flight optional timeout. Wait for
            # s3 to enter execute so scheduler/load variance cannot turn it into
            # a timeout before the optional check starts.
            await asyncio.wait_for(graph.blocked.wait(), 2)
            assert graph.step_calls == {'s1': 1, 's2': 1, 's3': 1}
            ready = await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            outcomes = ready['preview']['evidence']['steps']
            assert [(item['step_id'], item['evidence_id']) for item in outcomes] == [('s1', 'G1'), ('s2', 'G2'), ('s3', 'G3')]
            assert outcomes[2]['status'] == 'failed'
            assert gateway.syntheses == 0
            graph.release.set()
            await client.post(f"/v2/plans/{created['plan_id']}/confirm")
            final = await wait_state(client, created['run_id'], {'completed'})
            assert [(item['step_id'], item['evidence_id']) for item in final['evidence']['steps']] == [('s1', 'G1'), ('s2', 'G2'), ('s3', 'G3')]
            assert graph.step_calls == {'s1': 1, 's2': 1, 's3': 2}
    asyncio.run(scenario())


def test_combined_evidence_overrun_blocks_ready_even_when_each_query_succeeded(tmp_path):
    class SizedGraph(PreviewGraph):
        async def execute(self, step, previous, emit):
            result = await super().execute(step, previous, emit)
            result['materialized_bytes'] = 8
            return result
    async def scenario():
        graph = SizedGraph()
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=multi_plan()), max_bytes=10) as (client, runtime, gateway, *_):
            created = await create(client)
            failed = await wait_state(client, created['run_id'], {'failed'})
            assert all(step['status'] == 'complete' for step in failed['preview']['evidence']['steps'])
            assert failed['preview']['query_resource_limit_exceeded'] is True
            assert failed['preview']['confirmation_eligible'] is False
            assert failed['error']['recovery']['category'] == 'retrieval_limit'
            assert not ready_events(runtime, created['run_id'])
            assert gateway.syntheses == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('count, expected', [(1, 'failed'), (3, 'awaiting_confirmation')])
def test_single_and_grouped_queries_share_resolution_and_retrieval_deadline(tmp_path, count, expected):
    class DelayedGraph(PreviewGraph):
        async def prepare_plan(self, plan, emit):
            await asyncio.sleep(.035)
            return await super().prepare_plan(plan, emit)
        async def execute(self, step, previous, emit):
            await asyncio.sleep(.06)
            return await super().execute(step, previous, emit)
    async def scenario():
        plan = multi_plan()
        plan['steps'] = plan['steps'][:count]
        graph = DelayedGraph()
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=plan), preview_timeout=.075, grouped_preview_timeout=.30) as (client, runtime, gateway, *_):
            created = await create(client)
            run = await wait_state(client, created['run_id'], {expected})
            assert run['preview']['confirmation_eligible'] is (count == 3)
            assert gateway.syntheses == 0
            if count == 1:
                assert run['preview']['error']['category'] == 'timeout'
                assert not ready_events(runtime, created['run_id'])
            else:
                assert graph.calls == 3
    asyncio.run(scenario())


def test_grouped_outer_deadline_covers_preparation_and_every_query(tmp_path):
    class SlowGraph(PreviewGraph):
        async def prepare_plan(self, plan, emit):
            await asyncio.sleep(.03)
            return await super().prepare_plan(plan, emit)
        async def execute(self, step, previous, emit):
            await asyncio.sleep(.2)
            return await super().execute(step, previous, emit)
    async def scenario():
        async with service(tmp_path, graph=SlowGraph(), gateway=Gateway(plan=multi_plan()), preview_timeout=.06, grouped_preview_timeout=.09) as (client, runtime, gateway, *_):
            start = time.monotonic()
            created = await create(client)
            run = await wait_state(client, created['run_id'], {'failed'})
            assert time.monotonic() - start < .20
            assert run['preview']['error']['category'] == 'timeout'
            assert not ready_events(runtime, created['run_id'])
            assert gateway.syntheses == 0
    asyncio.run(scenario())


def test_grouped_queries_do_not_extend_schema_preparation_deadline(tmp_path):
    class SlowPreparation(PreviewGraph):
        async def prepare_plan(self, plan, emit):
            await asyncio.sleep(.2)
            return await super().prepare_plan(plan, emit)
    async def scenario():
        graph = SlowPreparation()
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=multi_plan()), preview_timeout=.04, grouped_preview_timeout=.3) as (client, runtime, gateway, *_):
            created = await create(client)
            run = await wait_state(client, created['run_id'], {'failed'})
            assert run['preview']['error']['category'] == 'timeout'
            assert graph.calls == 0
            assert len(run['preview']['evidence']['steps']) == 3
            assert gateway.syntheses == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', [None, 'unexplained_partial', 'truncated_parent', 'wrong_dependency'])
def test_explicit_top_n_dependency_allows_all_records_for_that_selected_population(tmp_path, fault):
    class BoundedGraph(PreviewGraph):
        async def execute(self, step, previous, emit):
            result = await super().execute(step, previous, emit)
            result.update(status='partial', requested_scope={'complete': step['complete']})
            if step['id'] == 's1':
                if fault == 'truncated_parent':
                    result['truncated'] = True
                    result['retrieval_execution']['cursor_exhausted'] = False
            elif fault != 'unexplained_partial':
                result['bounded_dependency_step_ids'] = ['wrong'] if fault == 'wrong_dependency' else ['s1']
            return result
    async def scenario():
        plan = multi_plan(dependent=True)
        plan['steps'] = plan['steps'][:2]
        plan['steps'][0]['complete'] = False
        plan['steps'][0]['question'] = 'Find the top 5 genes.'
        plan['steps'][1]['question'] = 'Find all pathways for those selected genes.'
        async with service(tmp_path, graph=BoundedGraph(), gateway=Gateway(plan=plan)) as (client, runtime, gateway, graph, *_):
            created = await create(client)
            run = await wait_state(client, created['run_id'], {'failed' if fault else 'awaiting_confirmation'})
            assert run['preview']['confirmation_eligible'] is (fault is None)
            if fault:
                assert not ready_events(runtime, created['run_id'])
                assert gateway.syntheses == 0
            else:
                assert run['preview']['query_readiness']['verified_step_ids'] == ['s1', 's2']
                assert run['preview']['evidence']['completeness'] == 'partial'
                assert (await client.post(f"/v2/plans/{created['plan_id']}/confirm")).status_code == 202
                final = await wait_state(client, created['run_id'], {'partial'})
                assert graph.calls == 2
                assert final['evidence']['preview_reuse']['retrieved_step_ids'] == []
    asyncio.run(scenario())


@pytest.mark.parametrize('count', [0, 3])
def test_scalar_count_parent_does_not_prove_dependent_entity_lookup_empty(tmp_path, count):
    class CountGraph(PreviewGraph):
        async def execute(self, step, previous, emit):
            result = await super().execute(step, previous, emit)
            result['nodes'] = []
            if step['id'] == 's1':
                result['rows'] = [{'count': count}]
            else:
                result.update(status='empty', queries=[], validation=[{'valid': True, 'reasons': ['empty_dependency:s1']}])
            return result
    async def scenario():
        plan = multi_plan(dependent=True)
        plan['steps'] = plan['steps'][:2]
        async with service(tmp_path, graph=CountGraph(), gateway=Gateway(plan=plan)) as (client, runtime, gateway, *_):
            created = await create(client)
            run = await wait_state(client, created['run_id'], {'failed'})
            assert run['preview']['query_readiness']['verified_step_ids'] == ['s1']
            assert run['preview']['query_readiness']['blocked_step_ids'] == ['s2']
            assert not ready_events(runtime, created['run_id'])
            assert gateway.syntheses == 0
    asyncio.run(scenario())
