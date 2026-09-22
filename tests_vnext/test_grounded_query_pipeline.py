import asyncio
from copy import deepcopy
from unittest.mock import patch
from test_graph import FakeAdapter
from pankagent_vnext.plan_recovery import recover_empty_plan

STEP = {'id':'s1','question':'Show ADCY3 colocalization evidence.', 'graph_version':'PanKgraph_08_04',
        'relation_types':['SIGNAL_COLOC_WITH'],'depends_on':[], 'complete':True,
        'constraints':[{'entity_type':'Gene','property':'name','operator':'=','value':'ADCY3'}]}
GOOD = "MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease) WHERE g.name='ADCY3' RETURN g,r,d"


async def emit(*args):
    pass


def adapter(batches):
    value=FakeAdapter(batches)
    value.settings.graph_version='PanKgraph_08_04'
    value.settings.grounded_query_policy=True
    return value


def gpu_only():
    """These checks exercise generation/repair rather than template routing."""
    return patch('pankagent_vnext.query_templates.compile_query', return_value=None)


def test_unique_direction_is_corrected_without_resampling():
    async def check():
        graph=adapter([[GOOD.replace(')-[',')<-[').replace(']->(',']-(')]])
        with gpu_only():
            result=await graph.execute(deepcopy(STEP),{},emit)
        assert result['status']=='complete'
        assert len(graph.generated)==1
        assert graph.retrieved[0][0]==GOOD
        assert result['validation'][0]['deterministic_repairs']
    asyncio.run(check())


def test_gpu_retry_receives_error_and_rejected_query_then_claude_once():
    async def check():
        bad=GOOD+' LIMIT 1'
        graph=adapter([[bad],[bad]])
        calls=[]
        async def repair(step, question, failures, candidate):
            calls.append((failures,candidate))
            return [GOOD]
        graph.query_repair=repair
        with gpu_only():
            result=await graph.execute(deepcopy(STEP),{},emit)
        assert result['status']=='complete'
        assert [n for q,n in graph.generated]==[1,1]
        assert 'Previous candidate to correct' in graph.generated[1][0]
        assert bad in graph.generated[1][0]
        assert len(calls)==1 and calls[0][1]==bad
        assert [a['route'] for a in result['generator_attempts']]==['gpu_initial','gpu_repair','claude_repair']
        assert len(graph.retrieved)==1
    asyncio.run(check())


def test_claude_repair_does_not_bypass_constraint_validation():
    async def check():
        bad=GOOD.replace("g.name='ADCY3'","g.name='GSDMB'")
        graph=adapter([[bad],[bad]])
        async def repair(*args):return [bad]
        graph.query_repair=repair
        with gpu_only():
            result=await graph.execute(deepcopy(STEP),{},emit)
        assert result['status']=='failed'
        assert not graph.retrieved
    asyncio.run(check())


def test_query_cache_revalidates_and_reads_again_without_inference():
    async def check():
        graph=adapter([[GOOD]])
        with gpu_only():
            first=await graph.execute(deepcopy(STEP),{},emit)
            second=await graph.execute(deepcopy(STEP),{},emit)
        assert first['status']==second['status']=='complete'
        assert second['query_route']=='cache'
        assert len(graph.generated)==1
        assert len(graph.retrieved)==len(graph.explained)==2
    asyncio.run(check())


def test_empty_planner_with_misleading_clarification_is_system_failure():
    async def check():
        class Never:
            async def plan(self,*args):raise AssertionError('No third repair call')
        failed={'steps':[],'proposal_issue':'empty_executable_plan',
                'clarification':'The proposed checks could not be linked safely.'}
        result=await recover_empty_plan(Never(),failed,'ADCY3 coloc?',[],10)
        assert result['recovery']['category']=='planning_failure'
        assert result['recovery']['retryable'] is True
    asyncio.run(check())


def test_explicit_auto_proceed_only_after_checked_preview(tmp_path):
    from tests_vnext.test_runtime import service, wait_state
    async def check():
        async with service(tmp_path) as (client,runtime,gateway,*_):
            response=await client.post('/v2/plans',json={'question':'Which cell types express INS?','auto_proceed_seconds':5})
            run_id=response.json()['run_id']
            await wait_state(client,run_id,{'awaiting_confirmation'})
            assert gateway.syntheses==0
            await asyncio.sleep(5.2)
            result=runtime.store.get(run_id)
            assert result['status'] in {'completed','partial'}
            assert gateway.syntheses==1
            events=runtime.store.events_after(run_id,0)
            assert any(e['type']=='auto_proceed_scheduled' for e in events)
    asyncio.run(check())


def test_cancelled_opt_in_timer_cannot_start_inference(tmp_path):
    from tests_vnext.test_runtime import service, wait_state
    async def check():
        async with service(tmp_path) as (client,runtime,gateway,*_):
            response=await client.post('/v2/plans',json={'question':'Which cell types express INS?','auto_proceed_seconds':5})
            run_id=response.json()['run_id']
            await wait_state(client,run_id,{'awaiting_confirmation'})
            await client.post(f'/v2/runs/{run_id}/cancel')
            await asyncio.sleep(5.2)
            assert runtime.store.get(run_id)['status']=='cancelled'
            assert gateway.syntheses==0
    asyncio.run(check())
