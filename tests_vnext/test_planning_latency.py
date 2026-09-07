import asyncio
import copy
from pankagent_vnext.planning_fastpath import expand_compact_plan,literature_only_revision
from tests_vnext.test_runtime import Gateway,Graph,PLAN,service,new_plan,wait_state


def test_compact_output_keeps_exact_scope():
    q='Compare CFTR detection in T2D ductal subclasses, excluding MUC5B.'
    p=expand_compact_plan({'interpreted_question':q,'steps':[{'id':'s','question':'','constraints':[{'property':'condition','value':'T2D'}]}]})
    assert p['steps'][0]['question']==q and p['steps'][0]['title']==q
    assert p['steps'][0]['constraints']==[{'property':'condition','value':'T2D'}]
    assert p['literature'] is True
    assert literature_only_revision('disable literature')
    assert not literature_only_revision('enable literature and change to T2D')


def test_literature_only_revision_uses_no_planner_call(tmp_path):
    async def check():
        gateway=Gateway()
        async with service(tmp_path,gateway=gateway) as (client,runtime,*_):
            initial=await new_plan(client,'INS expression')
            parent=runtime.store.get(initial['run_id'])
            count=gateway.plans
            response=await client.post(f'/v2/plans/{initial["plan_id"]}/revise',json={'question':'disable literature','revision_instruction':'disable literature','revision_mode':'instruction'})
            child=await wait_state(client,response.json()['run_id'],{'awaiting_confirmation'})
            assert gateway.plans==count
            assert child['plan']['literature'] is True
            assert child['plan']['steps']==parent['plan']['steps']
    asyncio.run(check())


def test_parallel_preview_keeps_order_dependencies_and_early_plan(tmp_path):
    class ParallelGraph(Graph):
        def __init__(self):super().__init__();self.started=set();self.two=asyncio.Event();self.reads=[]
        async def execute(self,step,previous,emit):
            self.started.add(step['id'])
            if {'s1','s2'}<=self.started:self.two.set()
            if step['id'] in ('s1','s2'):await asyncio.wait_for(self.two.wait(),1)
            if step['id']=='s3':assert 's1' in previous
            await emit('progress',{'stage':'querying_graph','step_id':step['id']})
            self.reads.append(step['id'])
            return await super().execute(step,previous,emit)
    async def check():
        plan=copy.deepcopy(PLAN);plan['steps']=[{**plan['steps'][0],'id':f's{i}','depends_on':['s1'] if i==3 else []} for i in (1,2,3)]
        graph=ParallelGraph()
        async with service(tmp_path,gateway=Gateway(plan=plan),graph=graph) as (client,runtime,*_):
            created=await new_plan(client,'INS expression')
            run=runtime.store.get(created['run_id'])
            assert graph.reads==['s1','s2','s3']
            assert [s['step_id'] for s in run['preview']['evidence']['steps']]==graph.reads
            assert run['plan']['review_ready']
            events=runtime.store.events_after(run['run_id'], 0)
            kinds=[e['type'] for e in events]
            assert kinds.index('plan_validated')<kinds.index('preview_step')<kinds.index('plan_ready')
    asyncio.run(check())


def test_early_plan_is_replayable_but_confirmation_waits_and_cancel_stops_preview(tmp_path):
    class WaitingGraph(Graph):
        def __init__(self):
            super().__init__(); self.started=asyncio.Event(); self.stopped=asyncio.Event()
        async def execute(self,step,previous,emit):
            self.started.set()
            try: await asyncio.Event().wait()
            finally: self.stopped.set()
    async def check():
        graph=WaitingGraph()
        async with service(tmp_path,graph=graph) as (client,runtime,*_):
            created=(await client.post('/v2/plans',json={'question':'INS expression'})).json()
            await asyncio.wait_for(graph.started.wait(),1)
            run=(await client.get(created['plan_url'])).json()
            assert run['status']=='planning' and run['plan']['review_ready']
            assert (await client.post(f'/v2/plans/{run["plan_id"]}/confirm')).status_code==409
            events=runtime.store.events_after(run['run_id'],0)
            assert any(e['type']=='plan_validated' for e in events)
            await client.post(f'/v2/runs/{run["run_id"]}/cancel')
            await asyncio.wait_for(graph.stopped.wait(),1)
            assert not any(e['type']=='preview_step' for e in runtime.store.events_after(run['run_id'],0))
    asyncio.run(check())
