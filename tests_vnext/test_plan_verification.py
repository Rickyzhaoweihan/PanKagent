import asyncio
import json
from types import SimpleNamespace
from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.plan_verification import review_input


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
        gateway._reserve = lambda *args: reservations.append(args) or 'reservation'
        gateway.budget = SimpleNamespace(settle=lambda *args: settlements.append(args))
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
