import asyncio
from copy import deepcopy
from pankagent_vnext.planning_session import run
from pankagent_vnext.query_recovery import stage_recovery
from test_claude_led_planning import gateway, SCHEMA


def test_exhausted_planning_preserves_human_stage_reason_and_stage_three_revision(tmp_path):
    question = 'How many PLN snMultiomics samples from HPAP donors with T1D stage 4?'
    recovery = stage_recovery('4', {'stages': ['Stage 1: recorded', 'Stage 2: recorded', 'Stage 3: recorded'], 'inventory_complete': True}, 'fixture')
    async def scenario():
        proposal = {'interpreted_question': question, 'steps': [{'id': 's1', 'question': question}], 'clarification': None}
        g, calls = gateway([('record_plan', proposal)])
        async def prepare(plan):
            plan = deepcopy(plan)
            plan['steps'][0]['semantic_issues'] = ['Requested stage cannot be resolved.']
            plan['recovery'] = deepcopy(recovery)
            plan['clarification'] = recovery['message']
            return plan
        result = await run(g, question, '{}', 'test', SCHEMA, 500, lambda p,c:p, preparer=prepare)
        assert not result['steps']
        assert result['recovery'] == recovery
        assert 'stage 4' in result['clarification']
        assert '1, 2, 3' in result['clarification']
        assert result['recovery']['suggestions'][0]['label'] == 'Use recorded stage 3'
        assert result['diagnostic_history']
        from test_runtime import Gateway, service, new_plan
        async with service(tmp_path, gateway=Gateway(plan=result)) as (client, *_):
            created = await new_plan(client, question, expected_status='failed')
            visible = (await client.get(f'/v2/runs/{created["run_id"]}')).json()
            assert visible['plan']['recovery'] == recovery
            assert visible['error']['recovery']['message'] == recovery['message']
            assert visible['error']['recovery']['suggestions'][0]['label'] == 'Use recorded stage 3'
    asyncio.run(scenario())
