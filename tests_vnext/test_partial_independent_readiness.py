"""Explicit partial opt-in preserves checked evidence and failed scope separately."""
from copy import deepcopy

import pytest

from pankagent_vnext.evidence_status import (
    PARTIAL_INDEPENDENT_POLICY, QUERY_READINESS_VERSION, checked_query_result,
    confirmation_eligible, query_readiness,
)


def plan(*, policy=True):
    value = {'steps': [
        {'id': 'coloc', 'purpose': 'primary', 'complete': True, 'depends_on': []},
        {'id': 'gwas', 'purpose': 'primary', 'complete': True, 'depends_on': []},
        {'id': 'qtl', 'purpose': 'primary', 'complete': True, 'depends_on': []}],
        'clarification': None}
    if policy:
        value['retrieval_policy'] = PARTIAL_INDEPENDENT_POLICY
    return value


def result(step_id, status='complete', *, truncated=False, scalar=None):
    return {'step_id': step_id, 'status': status, 'truncated': truncated,
            'nodes': [{'id': step_id}] if status == 'complete' and scalar is None else [],
            'edges': [], 'rows': [{'count': scalar}] if scalar is not None else [],
            'queries': [{'cypher': 'MATCH (n:Gene) RETURN n', 'parameters': {}}],
            'validation': [{'valid': status in {'complete', 'empty', 'partial'}}],
            'retrieval_execution': {'completed': status in {'complete', 'empty', 'partial'},
                                    'cursor_exhausted': not truncated}}


def preview(*results):
    return {'preparation_complete': True, 'evidence': {'steps': list(results)}}


def test_one_verified_primary_and_two_failures_allows_explicit_partial_review():
    p = plan()
    evidence = preview(result('coloc'), result('gwas', 'failed'), result('qtl', 'failed'))
    original = deepcopy((p, evidence))
    ready = query_readiness(p, evidence)
    assert ready['version'] == QUERY_READINESS_VERSION
    assert ready['ready'] and ready['partial_ready'] and not ready['full_coverage']
    assert ready['verified_step_ids'] == ['coloc']
    assert ready['blocked_step_ids'] == ['gwas', 'qtl']
    assert ready['nonempty_primary_step_ids'] == ['coloc']
    assert ready['all_required_finished']
    assert (p, evidence) == original  # Readiness cannot rewrite constraints/results.
    assert confirmation_eligible(p, evidence)


def test_legacy_behavior_stays_closed_for_same_partial_results():
    evidence = preview(result('coloc'), result('gwas', 'failed'), result('qtl', 'failed'))
    for policy in (None, 'partial_independent_v0', True):
        p = plan(policy=False)
        if policy is not None:
            p['retrieval_policy'] = policy
        ready = query_readiness(p, evidence)
        assert not ready['ready'] and not ready['partial_ready']
        assert ready['blocked_step_ids'] == ['gwas', 'qtl']


@pytest.mark.parametrize('policy', [True, False])
def test_complete_checks_and_legitimate_empty_results_keep_full_coverage(policy):
    ready = query_readiness(plan(policy=policy), preview(result('coloc'), result('gwas', 'empty'), result('qtl')))
    assert ready['ready'] and ready['full_coverage'] and not ready['partial_ready']
    assert ready['no_match_step_ids'] == ['gwas']


@pytest.mark.parametrize('unfinished', [None, 'queued', 'running', 'planning', 'cancelled', 'interrupted', 'unknown'])
def test_missing_or_nonterminal_required_outcome_cannot_open_partial_gate(unfinished):
    outcomes = [result('coloc'), result('gwas', 'failed')]
    if unfinished is not None:
        outcomes.append(result('qtl', unfinished))
    ready = query_readiness(plan(), preview(*outcomes))
    assert not ready['ready'] and not ready['all_required_finished']


@pytest.mark.parametrize('guard', ['clarification', 'query_resource_limit_exceeded', 'preparation_incomplete'])
def test_existing_scope_resource_and_preparation_guards_always_apply(guard):
    p = plan()
    evidence = preview(result('coloc'), result('gwas', 'failed'), result('qtl', 'failed'))
    if guard == 'clarification':
        p['clarification'] = 'Which gene was intended?'
    elif guard == 'preparation_incomplete':
        evidence['preparation_complete'] = False
    else:
        evidence[guard] = True
    ready = query_readiness(p, evidence)
    assert not ready['ready'] and not ready['partial_ready'] and not ready['full_coverage']


def test_empty_primary_with_failed_primary_and_nonempty_context_does_not_qualify():
    p = plan()
    p['steps'][2]['purpose'] = 'context'
    ready = query_readiness(p, preview(result('coloc', 'empty'), result('gwas', 'failed'), result('qtl')))
    assert ready['verified_step_ids'] == ['coloc']
    assert not ready['ready'] and not ready['nonempty_primary_step_ids']


def test_empty_graph_collection_wrapper_does_not_qualify_as_partial_evidence():
    empty_wrapper = result('coloc')
    empty_wrapper.update(nodes=[], edges=[], rows=[{'nodes': [], 'edges': []}])
    ready = query_readiness(plan(), preview(empty_wrapper, result('gwas', 'failed'), result('qtl', 'failed')))
    assert not ready['ready'] and not ready['nonempty_primary_step_ids']


@pytest.mark.parametrize('scalar', [0, 10])
def test_verified_scalar_answer_survives_without_inventing_graph_nodes(scalar):
    ready = query_readiness(plan(), preview(result('coloc', scalar=scalar), result('gwas', 'failed'), result('qtl', 'failed')))
    assert ready['partial_ready'] and ready['nonempty_primary_step_ids'] == ['coloc']
    assert ready['blocked_step_ids'] == ['gwas', 'qtl']


def test_truncated_evidence_is_never_a_verified_partial_admission_basis():
    ready = query_readiness(plan(), preview(result('coloc', truncated=True), result('gwas', 'failed'), result('qtl', 'failed')))
    assert not ready['ready'] and not ready['verified_step_ids']
    ready = query_readiness(plan(), preview(result('coloc'), result('gwas', 'partial', truncated=True), result('qtl', 'failed')))
    assert ready['partial_ready'] and ready['verified_step_ids'] == ['coloc']
    assert ready['blocked_step_ids'] == ['gwas', 'qtl']


@pytest.mark.parametrize('missing_proof', ['validation', 'execution', 'cursor', 'query', 'error'])
def test_a_successful_envelope_without_checked_execution_never_qualifies(missing_proof):
    unproven = result('coloc')
    if missing_proof == 'validation':
        unproven['validation'] = [{'valid': False}]
    elif missing_proof == 'execution':
        unproven['retrieval_execution']['completed'] = False
    elif missing_proof == 'cursor':
        unproven['retrieval_execution']['cursor_exhausted'] = False
    elif missing_proof == 'query':
        unproven['queries'] = []
    else:
        unproven['error'] = {'category': 'query_failed'}
    ready = query_readiness(plan(), preview(unproven, result('gwas', 'failed'), result('qtl', 'failed')))
    assert not ready['ready'] and not ready['verified_step_ids']


def test_failed_input_blocks_dependent_result_even_if_it_claims_success():
    p = plan()
    p['steps'][1]['depends_on'] = ['coloc']
    ready = query_readiness(p, preview(result('coloc', 'failed'), result('gwas'), result('qtl')))
    assert not ready['partial_ready']  # A false-success child cannot be retained as a failed check.
    assert ready['verified_step_ids'] == ['qtl']
    assert ready['blocked_step_ids'] == ['coloc', 'gwas']


def test_context_input_cannot_become_the_only_positive_primary():
    p = plan()
    p['steps'][0]['purpose'] = 'context'
    p['steps'][1]['depends_on'] = ['coloc']
    ready = query_readiness(p, preview(result('coloc'), result('gwas', 'failed'), result('qtl', 'empty')))
    assert ready['verified_step_ids'] == ['coloc', 'qtl']
    assert not ready['ready'] and not ready['nonempty_primary_step_ids']


def test_blocked_terminal_dependency_and_independent_evidence_remain_explicit():
    p = plan()
    p['steps'][1]['depends_on'] = ['coloc']
    blocked = result('gwas', 'blocked') | {'blocked_by': ['coloc']}
    ready = query_readiness(p, preview(result('coloc', 'failed'), blocked, result('qtl')))
    assert ready['partial_ready'] and ready['all_required_finished']
    assert ready['blocked_step_ids'] == ['coloc', 'gwas']


def test_no_steps_never_becomes_ready_via_policy_alone():
    ready = query_readiness({'steps': [], 'retrieval_policy': PARTIAL_INDEPENDENT_POLICY}, preview())
    assert not ready['ready'] and not ready['partial_ready'] and not ready['full_coverage']


def test_unavailable_terminal_result_can_be_disclosed_with_independent_success():
    ready = query_readiness(plan(), preview(result('coloc'), result('gwas', 'unavailable'), result('qtl', 'failed')))
    assert ready['partial_ready'] and ready['all_required_finished']
    assert ready['blocked_step_ids'] == ['gwas', 'qtl']


@pytest.mark.parametrize('failure_status', ['failed', 'blocked', 'unavailable', 'partial'])
def test_runtime_partial_confirmation_reuses_checked_and_failed_snapshot_without_extra_calls(tmp_path, failure_status):
    import asyncio
    from test_runtime import Gateway, service, wait_state
    from test_plan_preview import PreviewGraph, multi_plan

    async def run():
        proposed = multi_plan()
        proposed['retrieval_policy'] = PARTIAL_INDEPENDENT_POLICY
        class TerminalFailureGraph(PreviewGraph):
            async def execute(self, step, previous, emit):
                value = await super().execute(step, previous, emit)
                if value['status'] == 'partial':
                    value.update(truncated=True, retrieval_execution={'completed': True, 'cursor_exhausted': False})
                if value['status'] in {'blocked', 'unavailable'}:
                    value.update(nodes=[], edges=[], rows=[], queries=[],
                                 validation=[{'valid': False, 'reasons': ['component_unavailable']}],
                                 retrieval_execution={'completed': False, 'cursor_exhausted': False})
                return value
        graph = TerminalFailureGraph(outcomes={'s2': [failure_status]})
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=proposed)) as (client, runtime, gateway, _, literature):
            response = await client.post('/v2/plans', json={'question': 'Which evidence categories are recorded for INS?'})
            assert response.status_code == 202
            created = response.json()
            ready = await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            assert ready['preview']['query_readiness']['partial_ready']
            assert ready['preview']['query_readiness']['blocked_step_ids'] == ['s2']
            snapshot = deepcopy(ready['preview']['evidence']['steps'])
            replies = await asyncio.gather(*(client.post(f"/v2/plans/{created['plan_id']}/confirm") for _ in range(3)))
            assert all(reply.status_code == 202 for reply in replies)
            final = await wait_state(client, created['run_id'], {'partial'})
            assert graph.calls == 3 and gateway.syntheses == 1 and literature.calls == 0
            assert final['graph_answer'].startswith('Partial answer: 2 of 3 requested checks completed.')
            assert final['evidence']['preview_reuse']['retrieved_step_ids'] == []
            assert final['evidence']['preview_reuse']['unreused_reasons']['s2'] == 'retained_failed_check'
            assert final['preview']['evidence']['steps'] == snapshot
            failed = next(step for step in final['evidence']['steps'] if step['step_id'] == 's2')
            assert failed['status'] == failure_status
            assert final['evidence']['completeness'] == 'partial'
            assert final['evidence']['retrieval']['failed_checks'] == (0 if failure_status == 'partial' else 1)
            # Reading/reconnecting a saved partial answer performs no new calls.
            assert (await client.get(f"/v2/runs/{created['run_id']}")).status_code == 200
            assert graph.calls == 3 and gateway.syntheses == 1
    asyncio.run(run())


def test_runtime_partial_policy_does_not_override_cancellation(tmp_path):
    import asyncio
    from test_runtime import Gateway, service, wait_state
    from test_plan_preview import PreviewGraph, multi_plan

    async def run():
        proposed = multi_plan()
        proposed['retrieval_policy'] = PARTIAL_INDEPENDENT_POLICY
        graph = PreviewGraph(block_step='s3', outcomes={'s2': ['failed']})
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=proposed)) as (client, runtime, gateway, _, literature):
            created = (await client.post('/v2/plans', json={'question': 'Which evidence categories are recorded for INS?'})).json()
            await asyncio.wait_for(graph.blocked.wait(), 1)
            assert (await client.post(f"/v2/runs/{created['run_id']}/cancel")).status_code == 200
            cancelled = await wait_state(client, created['run_id'], {'cancelled'})
            assert not cancelled['plan'].get('review_ready')
            assert (await client.post(f"/v2/plans/{created['plan_id']}/confirm")).status_code == 409
            assert gateway.syntheses == literature.calls == 0
    asyncio.run(run())


@pytest.mark.parametrize('status', ['failed', 'blocked', 'unavailable', 'cancelled', 'interrupted', 'timeout', 'unknown', None])
def test_aggregate_status_never_hides_an_unsuccessful_check(status):
    from pankagent_vnext.evidence_status import aggregate_outcome_status
    steps = [result('coloc'), result('qtl', status)]
    original = deepcopy(steps)
    summary = aggregate_outcome_status(steps)
    assert summary['completeness'] == 'partial'
    assert summary['incomplete_checks'] == 1
    assert summary['failed_checks'] == (0 if status in {'unknown', None} else 1)
    assert steps == original


def test_aggregate_status_preserves_complete_empty_and_truncation_distinctions():
    from pankagent_vnext.evidence_status import aggregate_outcome_status
    assert aggregate_outcome_status([result('s1'), result('s2', 'empty')])['completeness'] == 'complete'
    assert aggregate_outcome_status([result('s1', 'empty'), result('s2', 'empty')])['completeness'] == 'empty'
    assert aggregate_outcome_status([])['completeness'] == 'partial'
    assert aggregate_outcome_status([result('s1', truncated=True)])['completeness'] == 'partial'
    assert aggregate_outcome_status([result('s1') | {'error': {'category': 'failure'}}])['completeness'] == 'partial'


@pytest.mark.parametrize('status', ['blocked', 'unavailable'])
def test_aggregate_evidence_complete_plus_unavailable_is_partial(status):
    from pankagent_vnext.app import aggregate_evidence
    evidence = aggregate_evidence({'s1': {'status': 'complete', 'nodes': [{'id': 'g'}]},
                                   's2': {'status': status}})
    assert evidence['completeness'] == 'partial'
    assert evidence['retrieval']['completeness'] == 'partial'
    assert evidence['retrieval']['failed_checks'] == 1
