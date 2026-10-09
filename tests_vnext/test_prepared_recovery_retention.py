import asyncio
from copy import deepcopy
from pankagent_vnext.planning_session import run
from pankagent_vnext.query_recovery import stage_recovery
from test_claude_led_planning import gateway, SCHEMA


def test_stage_diagnostic_preserves_human_reason_without_python_rejection(tmp_path):
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
        assert result['steps']
        assert not result.get('clarification')
        assert len(calls) == 2
        stage_advice = next(d for d in result['python_diagnostics'] if d.get('detail'))
        assert stage_advice['blocking'] is False
        assert stage_advice['detail'] == recovery
        assert 'stage 4' in stage_advice['detail']['message']
        assert stage_advice['detail']['suggestions'][0]['label'] == 'Use recorded stage 3'
    asyncio.run(scenario())
