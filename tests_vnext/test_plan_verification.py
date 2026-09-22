from unittest.mock import AsyncMock
import asyncio
import json
from types import SimpleNamespace
from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.plan_verification import review_input, review_verified_local_coloc
from pankagent_vnext.pattern_planning import VERSION as PATTERN_VERSION, DIGEST as PATTERN_DIGEST


def test_review_contains_scope_and_status_without_donor_rows_or_queries():
    plan = {'steps': [{'id': 's1', 'question': 'Count samples', 'constraints': [], 'relation_types': ['HAS_SAMPLE']}]}
    preview = {'evidence': {'steps': [{'step_id': 's1', 'status': 'empty', 'nodes': [], 'edges': [],
                'queries': ['SECRET QUERY'], 'rows': [{'secret': 'protected donor value'}]}]}, 'query_readiness': {'ready': True}}
    data = review_input('Count samples', plan, preview)
    assert data['checks'][0]['execution_status'] == 'empty'
    assert 'SECRET' not in json.dumps(data) and 'protected' not in json.dumps(data)


def test_one_short_review_cannot_approve_with_issues():
    async def check():
        gateway = object.__new__(ClaudeGateway)
        gateway.settings = SimpleNamespace(model='claude-sonnet-5')
        reservations, calls, settlements = [], [], []
        gateway._reserve = AsyncMock(side_effect=lambda *args: reservations.append(args) or 'reservation')
        gateway.budget = SimpleNamespace(asettle=AsyncMock(side_effect=lambda *args: settlements.append(args)))
        async def create(*args, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(stop_reason='tool_use', usage=SimpleNamespace(model_dump=lambda: {}), content=[
                SimpleNamespace(type='tool_use', name='verify_plan', input={'approved': True,
                    'issues': [{'step_id': 's1', 'reason': 'Missing the requested tissue.'}]})])
        gateway._create = create
        result = await gateway.review_grounded_plan('request', {'steps': []}, {})
        assert not result['approved'] and len(calls) == 1 and len(settlements) == 1
        assert reservations[0][0] == 'plan_verification' and calls[0]['max_tokens'] == 400
        assert 'record_plan' not in json.dumps(calls)
    asyncio.run(check())


def _local_coloc_plan_and_preview():
    constraints = [
        {'entity_type': 'Gene', 'property': 'id', 'operator': '=',
         'value': 'ENSG00000225190'},
        {'entity_type': 'disease', 'property': 'id', 'operator': '=',
         'value': 'MONDO_0005147'},
    ]
    step = {
        'id': 'coloc', 'question': 'Show recorded colocalization evidence.',
        'relation_types': ['SIGNAL_COLOC_WITH'], 'depends_on': [],
        'constraints': constraints, 'complete': True,
        'evidence_combination': 'independent',
        'query_compilation': {'route': 'verified_local_template',
                              'version': PATTERN_VERSION,
                              'digest': PATTERN_DIGEST},
    }
    edge = {'start_id': 'ENSG00000225190', 'end_id': 'MONDO_0005147',
            'type': 'SIGNAL_COLOC_WITH', 'properties': {}}
    record = {'record_link': 'urn:pankgraph:signal-coloc-with:sha256:one'}
    outcome = {
        'step_id': 'coloc', 'status': 'complete', 'truncated': False,
        'query_route': 'template',
        'generator_attempts': [{'route': 'template', 'status': 'completed'}],
        'validation': [{'valid': True, 'route': 'template'}],
        'retrieval_execution': {'completed': True, 'cursor_exhausted': True},
        'requested_scope': {'relation_types': ['SIGNAL_COLOC_WITH'],
                            'constraints': constraints, 'complete': True},
        'edges': [edge], 'colocalization_records': [record],
        'colocalization_record_links': [record['record_link']],
        'colocalization_signal_counts': {
            'record_count': 1,
            'unresolved_gwas_signal_reference_count': 0,
            'unresolved_qtl_signal_reference_count': 0,
            'typed_endpoints_verified_record_count': 1,
        },
        'colocalization_record_completeness': {'state': 'complete'},
    }
    plan = {'steps': [step], 'planning_route': {
        'kind': 'verified_signal_pattern', 'claude_calls': 0}}
    preview = {'query_readiness': {'ready': True},
               'evidence': {'steps': [outcome]}}
    return plan, preview


def test_closed_coloc_plan_is_reviewed_locally_without_reinterpreting_signal_roles():
    plan, preview = _local_coloc_plan_and_preview()
    result = review_verified_local_coloc(plan, preview)
    assert result['approved'] is True
    assert result['model_calls'] == 0
    assert result['route'] == 'verified_local_template'
    assert result['issues'] == []


def test_marked_local_coloc_review_fails_closed_without_model_fallback():
    plan, preview = _local_coloc_plan_and_preview()
    preview['evidence']['steps'][0]['generator_attempts'].append(
        {'route': 'gpu_initial', 'status': 'completed'})
    result = review_verified_local_coloc(plan, preview)
    assert result is not None and result['approved'] is False
    assert 'generated query' in result['issues'][0]['reason']


def test_nonlocal_pattern_keeps_existing_model_review_route():
    plan, preview = _local_coloc_plan_and_preview()
    plan['steps'][0].pop('query_compilation')
    assert review_verified_local_coloc(plan, preview) is None


def test_pattern_plan_waits_for_review_and_confirmation_reuses_queries(tmp_path):
    from copy import deepcopy
    from tests_vnext.test_runtime import Gateway, Graph, PLAN, service, wait_state
    class ReviewingGateway(Gateway):
        def __init__(self):
            plan = deepcopy(PLAN)
            plan['planning_route'] = {'kind': 'verified_signal_pattern', 'claude_calls': 0}
            super().__init__(plan=plan)
            self.entered, self.release = asyncio.Event(), asyncio.Event()
            self.reviews = 0
        async def review_grounded_plan(self, question, plan, preview):
            self.reviews += 1
            assert preview['preparation_complete'] and preview['query_readiness']['ready']
            self.entered.set()
            await self.release.wait()
            return {'approved': True, 'issues': []}
    async def check():
        gateway, graph = ReviewingGateway(), Graph()
        async with service(tmp_path, gateway=gateway, graph=graph) as (client, runtime, *_):
            created = (await client.post('/v2/plans', json={'question': 'INS expression'})).json()
            await asyncio.wait_for(gateway.entered.wait(), 1)
            events = runtime.store.events_after(created['run_id'], 0)
            assert not any(e['type'] == 'plan_ready' for e in events)
            assert (await client.post(f"/v2/plans/{created['plan_id']}/confirm")).status_code == 409
            gateway.release.set()
            await wait_state(client, created['run_id'], {'awaiting_confirmation'})
            assert gateway.reviews == 1
            assert (await client.post(f"/v2/plans/{created['plan_id']}/confirm")).status_code == 202
            await wait_state(client, created['run_id'], {'completed'})
            assert gateway.reviews == 1
    asyncio.run(check())


def test_rejected_review_never_exposes_approved_plan(tmp_path):
    from copy import deepcopy
    from tests_vnext.test_runtime import Gateway, PLAN, service, wait_state
    class RejectingGateway(Gateway):
        def __init__(self):
            plan = deepcopy(PLAN)
            plan['planning_route'] = {'kind': 'verified_schema_pattern', 'claude_calls': 0}
            super().__init__(plan=plan)
        async def review_grounded_plan(self, question, plan, preview):
            return {'approved': False, 'issues': [{'step_id': 's1', 'reason': 'Wrong tissue.'}]}
    async def check():
        async with service(tmp_path, gateway=RejectingGateway()) as (client, runtime, *_):
            created = (await client.post('/v2/plans', json={'question': 'INS expression'})).json()
            run = await wait_state(client, created['run_id'], {'failed'})
            assert run['error']['category'] == 'plan_scope_verification_failed'
            assert not any(e['type'] == 'plan_ready' for e in runtime.store.events_after(created['run_id'], 0))
    asyncio.run(check())
