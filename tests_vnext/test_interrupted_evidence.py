"""A retrieval deadline cannot turn unfinished requested coverage into complete."""
from copy import deepcopy
import asyncio

import pytest

from pankagent_vnext.interrupted_evidence import complete_interrupted_evidence
from pankagent_vnext.store import Store


def inputs():
    plan = {'steps': [{'id': 's1'}, {'id': 's2', 'depends_on': ['s1']},
                      {'id': 's3', 'depends_on': ['s2']}, {'id': 's4', 'depends_on': []}]}
    step = {'step_id': 's1', 'evidence_id': 'G1', 'status': 'complete', 'rows': [{'count': 0}],
            'queries': [{'cypher': 'RETURN 0 AS count'}], 'nodes': [], 'edges': []}
    evidence = {'completeness': 'complete', 'steps': [step], 'rows': [{'count': 0}],
                'queries': deepcopy(step['queries']), 'nodes': [], 'edges': [], 'truncated': False,
                'preview_reuse': {'reused_step_ids': ['s1']},
                'retrieval': {'completeness': 'complete', 'node_count': 0}}
    return plan, evidence


@pytest.mark.parametrize('category,state', [('timeout', 'timed_out'), ('connection', 'interrupted')])
def test_missing_checks_distinguish_active_blocked_and_unattempted(category, state):
    plan, evidence = inputs()
    before = deepcopy(evidence)
    result = complete_interrupted_evidence(plan, evidence, {'category': category, 'message': 'PRIVATE'}, ['s2'])
    assert result['steps'][0] == evidence['steps'][0]
    assert result['steps'][1]['execution_status'] == state
    assert result['steps'][1]['attempted'] is True
    assert result['steps'][2]['execution_status'] == 'blocked'
    assert result['steps'][2]['blocked_by'] == ['s2']
    assert result['steps'][3]['execution_status'] == 'not_attempted'
    assert result['steps'][3]['attempted'] is False
    assert all(s['status'] == 'failed' and s['rows'] == [] for s in result['steps'][1:])
    assert result['completeness'] == result['retrieval']['completeness'] == 'partial'
    assert result['retrieval']['checks'] == 4 and result['retrieval']['failed_checks'] == 3
    assert result['retrieval']['incomplete_step_ids'] == ['s2', 's3', 's4']
    assert result['preview_reuse'] == evidence['preview_reuse']
    assert result['queries'] == evidence['queries'] and result['rows'] == [{'count': 0}]
    assert result['truncated'] is False
    assert 'PRIVATE' not in str(result)
    assert evidence == before


def test_synthesis_failure_does_not_invalidate_completed_retrieval():
    plan, evidence = inputs()
    plan['steps'] = plan['steps'][:1]
    assert complete_interrupted_evidence(plan, evidence, {'category': 'timeout'}) == evidence


def test_no_prior_evidence_never_becomes_empty_result():
    result = complete_interrupted_evidence({'steps': [{'id': 's1'}]}, None, {'category': 'timeout'}, ['s1'])
    assert result['completeness'] == 'partial'
    assert result['steps'][0]['status'] == 'failed'
    assert result['steps'][0]['execution_status'] == 'timed_out'


def test_interrupted_coverage_survives_restart(tmp_path):
    plan, evidence = inputs()
    result = complete_interrupted_evidence(plan, evidence, {'category': 'timeout'}, ['s2'])
    store = Store(tmp_path)
    run = store.create('synthetic deadline test')
    store.update(run['run_id'], plan=plan, evidence=result, status='partial', stage='partial')
    store.close()
    reopened = Store(tmp_path)
    try:
        assert reopened.get(run['run_id'])['evidence'] == result
    finally:
        reopened.close()


@pytest.mark.parametrize('shutdown', [False, True])
def test_cancel_and_graceful_shutdown_preserve_graph_and_record_missing_outcome(tmp_path, shutdown):
    from test_plan_preview import PreviewGraph, multi_plan
    from test_runtime import Gateway, new_plan, service
    async def scenario():
        graph = PreviewGraph()
        plan = multi_plan(dependent=True)
        plan['steps'] = plan['steps'][:2]
        plan['steps'][1]['purpose'] = 'context'  # optional late work does not invalidate checked primary readiness
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=plan)) as (client, runtime, gateway, graph, literature):
            created = await new_plan(client)
            saved = runtime.store.get(created['run_id'])
            cache = deepcopy(saved['preview_cache'])
            cache['step_completed_epochs']['s2'] = 0
            runtime.store.update(created['run_id'], preview_cache=cache)
            graph.block_step = 's2'
            assert (await client.post(f'/v2/plans/{created["plan_id"]}/confirm')).status_code == 202
            await asyncio.wait_for(graph.blocked.wait(), 1)
            in_progress = runtime.store.get(created['run_id'])
            assert in_progress['evidence']['steps'][0]['status'] == 'complete'
            if not shutdown:
                response = await client.post(f'/v2/runs/{created["run_id"]}/cancel')
                assert response.json()['status'] == 'cancelled'
                await asyncio.gather(*list(runtime.tasks.values()), return_exceptions=True)
                snapshot = (await client.get(f'/v2/runs/{created["run_id"]}')).json()
                assert snapshot['status'] == 'cancelled'
                assert snapshot['graph_answer'] is None
                assert (await client.post(f'/v2/plans/{created["plan_id"]}/confirm')).status_code == 409
                events = runtime.store.events_after(created['run_id'], 0)
                assert len([e for e in events if e['type'] == 'terminal']) == 1
                assert not any(e['type'] == 'graph_answer' for e in events)
        reopened = Store(tmp_path)
        try:
            result = reopened.get(created['run_id'])
            assert result['status'] == ('interrupted' if shutdown else 'cancelled')
            assert result['graph_answer'] is None
            assert result['evidence']['nodes'] == in_progress['evidence']['nodes']
            assert result['evidence']['steps'][0] == in_progress['evidence']['steps'][0]
            assert result['evidence']['completeness'] == result['evidence']['retrieval']['completeness'] == 'partial'
            assert result['evidence']['steps'][1]['execution_status'] == 'interrupted'
            assert result['evidence']['steps'][1]['attempted'] is True
            assert graph.step_calls['s1'] == 1
            assert graph.cancelled == 1
            assert gateway.syntheses == literature.calls == 0
        finally:
            reopened.close()
    asyncio.run(scenario())


def test_unclean_restart_does_not_invent_whether_unrecorded_checks_started(tmp_path):
    plan, evidence = inputs()
    store = Store(tmp_path)
    run = store.create('synthetic interrupted execution')
    store.update(run['run_id'], status='running', stage='querying_graph', plan=plan, evidence=evidence)
    store.close()
    reopened = Store(tmp_path)
    try:
        assert reopened.interrupt_active() == [run['run_id']]
        result = reopened.get(run['run_id'])
        assert result['status'] == 'interrupted' and result['graph_answer'] is None
        assert result['evidence']['steps'][0] == evidence['steps'][0]
        assert result['evidence']['completeness'] == 'partial'
        assert all(s['execution_status'] == 'interrupted' and s['attempted'] is None
                   for s in result['evidence']['steps'][1:])
        assert reopened.interrupt_active() == []
        assert len([e for e in reopened.events_after(run['run_id'], 0) if e['type'] == 'terminal']) == 1
    finally:
        reopened.close()
