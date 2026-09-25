import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import pytest
from pankagent_vnext.competing_candidates import Selection, produce, membership
from pankagent_vnext.query_assistance import approved_patch
from pankagent_vnext.store import Store
from test_composable_planning import evidence, operation
from test_runtime import service, Gateway, PLAN, wait_state
from test_plan_preview import PreviewGraph

STEP = {'id':'a','question':'approved','depends_on':[],'constraints':[], 'complete':True}


def test_complete_replaces_partial_not_larger_wrong_result():
    s = Selection(STEP,{})
    assert s.offer(evidence('a',['x','y'],status='partial'),'gpu')
    assert s.offer(evidence('a',['x']),'template')
    assert s.current()['status']=='complete' and s.revision==2
    assert s.offer(evidence('a',['x','y']),'gpu')
    assert s.current()['error']['category']=='candidate_membership_conflict'


def test_empty_and_equivalent_are_not_replaced():
    s = Selection(STEP,{})
    assert s.offer(evidence('a',[],status='empty'),'template')
    assert not s.offer(evidence('a',[],status='empty'),'gpu')
    assert s.revision==1 and not s.conflict


def test_collect_and_native_graph_rows_have_same_membership():
    first = evidence('a',['x','y'])
    edge = {'start_id':'x','end_id':'y','type':'LINK','properties':{}}
    first['edges'] = [edge]
    ref = {'edge':['x','LINK','y'],'fingerprint':'same'}
    first['rows'] = [{'nodes':[{'node_id':'x'},{'node_id':'y'}],'edges':[ref]}]
    second = deepcopy(first)
    second['rows'] = [{'gene':{'node_id':'x'},'target':{'node_id':'y'},'relationship':ref}]
    selector = Selection(STEP,{})
    assert selector.offer(first,'template')
    assert not selector.offer(second,'gpu')
    assert not selector.conflict
    second['rows'][0]['count'] = 99
    assert selector.offer(second,'gpu') and selector.conflict


def test_structured_assistance_validates_real_tool_reply_without_network():
    from pankagent_vnext.llm import ClaudeGateway
    async def run():
        reply = SimpleNamespace(content=[SimpleNamespace(type='tool_use',name='propose_structure',
            input={'action':'no_change','step_json':'','reason':'already correct'})],
            usage=SimpleNamespace(model_dump=lambda:{'input_tokens':1,'output_tokens':1}))
        async def reserve(*args):return 'reservation'
        async def create(*args,**kwargs):return reply
        async def settle(*args):pass
        gateway = SimpleNamespace(settings=SimpleNamespace(model='claude-sonnet-5'),
            _reserve=reserve,_create=create,_options=lambda:{},budget=SimpleNamespace(asettle=settle))
        result = await ClaudeGateway.assist_query_structure(gateway,{'kind':'conflict'})
        assert result['action']=='no_change'
        reply.content[0].input = {'action':'invent_records'}
        assert (await ClaudeGateway.assist_query_structure(gateway,{}))['reason']=='invalid_assistance_response'
    asyncio.run(run())


def test_partial_replacement_requires_new_requested_evidence_and_no_loss():
    selection = Selection({**STEP, 'relation_types':['FIRST','SECOND']}, {})
    first = evidence('a',['x'],status='partial')
    first['edges'] = [{'type':'FIRST','start_id':'x','end_id':'x'}]
    assert selection.offer(first,'template')
    larger = deepcopy(first)
    larger['nodes'].append({'id':'y','labels':['donor']})
    assert not selection.offer(larger,'gpu')
    missing = deepcopy(first)
    missing['edges'] = [{'type':'SECOND','start_id':'x','end_id':'x'}]
    assert not selection.offer(missing,'gpu')
    improved = deepcopy(first)
    improved['edges'].extend(missing['edges'])
    assert selection.offer(improved,'gpu')
    assert selection.records[-1]['decision'] == 'replaced_missing_evidence'


def test_invalid_queries_never_selected():
    s = Selection(STEP,{})
    r=evidence('a',['x']);r['validation']=[{'valid':False}]
    assert not s.offer(r,'gpu') and s.current() is None


def test_role_patch_keeps_input_scope():
    op=operation('intersection');p=deepcopy(op)
    parents={'a':evidence('a',['x']),'b':evidence('b',['x'])}
    parents['a']['edges'] = [{'start_id':'x','end_id':'z','type':'HAS_SAMPLE','properties':{}}]
    p['operation']['inputs'][0]['role']='source'
    assert approved_patch(op,p,parents)==p
    p['operation']['operator']='union'
    with pytest.raises(ValueError,match='scope_changed'):approved_patch(op,p,parents)


def test_literal_or_dependency_patch_rejected():
    s={**STEP,'constraints':[{'entity_type':'donor','property':'t1d_stage','value':'Stage 1','operator':'='}]}
    p=deepcopy(s);p['constraints'][0]['value']='Stage 3'
    with pytest.raises(ValueError,match='predicate_changed'):approved_patch(s,p,{})
    p=deepcopy(s);p['depends_on']=['other']
    with pytest.raises(ValueError,match='scope_changed'):approved_patch(s,p,{})


class RaceGraph(PreviewGraph):
    def __init__(self, mode='equivalent'):
        super().__init__()
        self.mode=mode
        self.shared={'reads':0,'cancelled':0}
        self.late=asyncio.Event()
    async def _retrieve(self, query, params, limits):
        self.shared['reads']+=1
        await asyncio.sleep(.005)
        return evidence('s1',['INS'])
    async def execute(self, step, previous, emit):
        if getattr(self,'_candidate_route','')=='gpu':
            try:await self.late.wait()
            except asyncio.CancelledError:
                self.shared['cancelled']+=1
                raise
        result=await self._retrieve('same',{}, {})
        result['step_id']=step['id'];result['graph_version']='test-release'
        if self.mode=='conflict' and getattr(self,'_candidate_route','')=='gpu':
            result['nodes'][0]['id']='DIFFERENT'
        if hasattr(self,'_candidate_result'):await self._candidate_result(result)
        return result


def test_shared_identical_read_only_once():
    async def run():
        g=RaceGraph();g.late.set();results=[]
        async def emit(*a):pass
        async def offer(r,o):results.append((r,o))
        async def assist(*a):return None
        await produce(g,STEP,{},emit,offer,assist,asyncio.Semaphore(2))
        assert g.shared['reads']==1
        assert {o for _,o in results}=={'local','gpu'}
    asyncio.run(run())


@pytest.mark.parametrize('mode',['equivalent','conflict'])
def test_runtime_first_preview_then_late_candidate(tmp_path,mode):
    async def run():
        g=RaceGraph(mode)
        async with service(tmp_path, graph=g, gateway=Gateway(plan=PLAN),competing_candidates=True) as (client,r,*_):
            response=await client.post('/v2/plans',json={'question':'Which cell types express INS?'})
            created=response.json()
            ready=await wait_state(client,created['run_id'],{'awaiting_confirmation'})
            first=ready['plan_id']
            assert g.shared['reads']==1
            g.late.set()
            await asyncio.wait_for(r.candidate_previews[created['run_id']].task,2)
            current=r.store.get(created['run_id'])
            if mode=='conflict':
                assert current['plan_id']!=first
                assert not current['preview']['confirmation_eligible']
                assert (await client.post(f'/v2/plans/{first}/confirm')).status_code==409
            else:
                assert current['plan_id']==first
                assert (await client.post(f'/v2/plans/{first}/confirm')).status_code==202
    asyncio.run(run())


def test_confirmation_cancels_pending_candidate_and_freezes_evidence(tmp_path):
    async def run():
        g=RaceGraph('conflict')
        async with service(tmp_path,graph=g,gateway=Gateway(plan=PLAN),competing_candidates=True) as (client,r,*_):
            created=(await client.post('/v2/plans',json={'question':'INS?'})).json()
            ready=await wait_state(client,created['run_id'],{'awaiting_confirmation'})
            before=deepcopy(ready['preview'])
            assert (await client.post('/v2/plans/'+ready['plan_id']+'/confirm')).status_code==202
            g.late.set()
            assert r.candidate_previews[created['run_id']].task.done()
            assert r.store.get(created['run_id'])['preview']['evidence']['nodes']==before['evidence']['nodes']
            assert g.shared['cancelled']==1
    asyncio.run(run())


def test_store_compare_and_swap_and_frozen_publish(tmp_path):
    store=Store(tmp_path);run=store.create('q');plan={'steps':[]}
    store.update(run['run_id'],status='awaiting_confirmation',plan=plan)
    assert store.publish_candidate_preview(run['run_id'],plan,{}, {})
    current=store.get(run['run_id'])
    assert store.retired_plan(run['plan_id'])
    assert not store.confirm(run['run_id'],run['plan_id'])
    assert store.confirm(run['run_id'],current['plan_id'])
    assert not store.publish_candidate_preview(run['run_id'],plan,{'changed':True},{})
    assert store.get(run['run_id'])['preview']=={}
    store.close()


def test_default_off():
    from pankagent_vnext.config import Settings
    assert not Settings().competing_candidates


def test_real_query_pipeline_evaluates_both_routes_and_deduplicates():
    from test_grounded_query_pipeline import adapter, STEP as real_step, GOOD
    from unittest.mock import patch
    async def run():
        graph=adapter([[GOOD]])
        graph.answer['retrieval_execution']={'completed':True,'cursor_exhausted':True}
        results=[]
        async def emit(*a):pass
        async def offer(r,o):results.append((r,o))
        async def assist(*a):return None
        with patch('pankagent_vnext.query_templates.compile_query',return_value={'cypher':GOOD,'parameters':{}}):
            await produce(graph,real_step,{},emit,offer,assist,asyncio.Semaphore(2))
        accepted=[(r,o) for r,o in results if r['status']=='complete']
        assert {o for _,o in accepted}=={'local','gpu'}
        assert len(graph.retrieved)==1
        assert len(graph.explained)==2
        assert all(r['evidence_coverage'] for r,_ in accepted)
    asyncio.run(run())


def test_real_pipeline_continues_after_bad_gpu_candidate():
    from test_grounded_query_pipeline import adapter, STEP as real_step, GOOD, gpu_only
    async def run():
        graph=adapter([[GOOD+' LIMIT 1',GOOD]])
        graph.answer['retrieval_execution']={'completed':True,'cursor_exhausted':True}
        results=[]
        async def emit(*a):pass
        async def offer(r,o):results.append((r,o))
        async def assist(*a):return None
        with gpu_only():await produce(graph,real_step,{},emit,offer,assist,asyncio.Semaphore(2))
        assert any(r['status']=='complete' and o=='gpu' for r,o in results)
        assert len(graph.retrieved)==1
        assert all('LIMIT' not in q for q,_ in graph.retrieved)
    asyncio.run(run())


def test_parent_replacement_rebuilds_child_but_not_independent(tmp_path):
    from collections import Counter
    from test_plan_preview import multi_plan
    class Revisions(RaceGraph):
        def __init__(self):
            super().__init__();self.seen=[]
        async def execute(self,step,previous,emit):
            origin=self._candidate_route
            self.seen.append((step['id'],origin,deepcopy(previous)))
            if origin=='gpu':await self.late.wait()
            status='partial' if step['id']=='s1' and origin=='local' else 'complete'
            r=evidence(step['id'],['INS'],status=status);r['graph_version']='test-release'
            await self._candidate_result(r)
            return r
    async def run():
        g=Revisions()
        async with service(tmp_path,graph=g,gateway=Gateway(plan=multi_plan(dependent=True)),competing_candidates=True) as (client,r,*_):
            c=(await client.post('/v2/plans',json={'question':'INS?'})).json()
            first=await wait_state(client,c['run_id'],{'awaiting_confirmation'})
            assert first['preview']['query_readiness']['partial_ready']
            assert first['preview']['query_readiness']['blocked_step_ids']==['s1','s2']
            g.late.set()
            await asyncio.wait_for(r.candidate_previews[c['run_id']].task,2)
            current=r.store.get(c['run_id'])
            assert current['plan_id']!=first['plan_id']
            assert current['preview']['query_readiness']['full_coverage']
            assert Counter((key,origin) for key,origin,_ in g.seen)[('s3','local')]==1
            assert any(key=='s2' and prev['s1']['status']=='complete' for key,_,prev in g.seen)
            assert (await client.post('/v2/plans/'+first['plan_id']+'/confirm')).status_code==409
            assert (await client.post('/v2/plans/'+current['plan_id']+'/confirm')).status_code==202
    asyncio.run(run())


def test_budget_shared_by_all_assistance_kinds(tmp_path):
    store=Store(tmp_path);r=store.create('q');store.update(r['run_id'],plan={'planning_route':{'claude_calls':5}})
    limits={'execution_repairs':2,'total_claude_calls':7}
    assert store.claim_execution_repair(r['run_id'],'template',limits)
    assert store.claim_execution_repair(r['run_id'],'combination',limits)
    assert not store.claim_execution_repair(r['run_id'],'query_repair',limits)
    store.close()


def test_deadline_returns_failed_snapshot_without_hanging(tmp_path):
    class Waiting(RaceGraph):
        async def execute(self,*args):await self.late.wait()
    async def run():
        async with service(tmp_path,graph=Waiting(),gateway=Gateway(plan=PLAN),competing_candidates=True,grouped_preview_timeout=.04) as (client,r,*_):
            c=(await client.post('/v2/plans',json={'question':'INS?'})).json()
            current=await wait_state(client,c['run_id'],{'failed'})
            assert not current['preview']['confirmation_eligible']
    asyncio.run(run())


def test_invalid_template_proposal_cannot_change_literal_scope(tmp_path):
    from pankagent_vnext.query_assistance import advise
    import json
    class Bad(Gateway):
        async def assist_query_structure(self,payload):
            p=deepcopy(payload['approved_step']);p['constraints']=[]
            return {'action':'patch','step_json':json.dumps(p),'reason':'more rows'}
    async def run():
        async with service(tmp_path,gateway=Bad()) as (_,r,*_):
            created=r.store.create('Stage 1 donors')
            s={**STEP,'constraints':[{'entity_type':'donor','property':'t1d_stage','operator':'=','value':'Stage 1'}]}
            r.store.update(created['run_id'],plan={'steps':[s]})
            with pytest.raises(ValueError,match='predicate_changed'):
                await advise(r,created['run_id'],'template',s,{}, {})
    asyncio.run(run())


def test_cross_release_candidate_rejected():
    s=Selection({**STEP,'graph_version':'expected'}, {})
    assert not s.offer(evidence('a',['x']),'gpu')


def test_missing_required_path_witness_rejected():
    s=Selection({**STEP,'path_spec':{'nodes':[{'role':'a'}]}},{})
    assert not s.offer(evidence('a',['x']),'gpu')


def test_old_parent_callback_cannot_publish(tmp_path):
    # Selection input is frozen even if the caller mutates its parent dictionary.
    parents={'p':evidence('p',['old'])}
    s=Selection({**STEP,'depends_on':['p']},parents)
    parents['p']['nodes'][0]['id']='new'
    assert s.parents['p']['nodes'][0]['id']=='old'


def test_broken_producer_does_not_cancel_other_route():
    class Broken(RaceGraph):
        async def execute(self, step, previous, emit):
            if self._candidate_route == 'local':
                raise RuntimeError('template failure')
            await asyncio.sleep(.01)
            return evidence(step['id'], ['good'])
    async def run():
        results = []
        async def emit(*args): pass
        async def offer(result, origin): results.append(result)
        async def assist(*args): return None
        await produce(Broken(), STEP, {}, emit, offer, assist, asyncio.Semaphore(2))
        assert results[0]['nodes'][0]['id'] == 'good'
    asyncio.run(run())


def test_successful_combination_assistance_keeps_mirrored_operation_consistent(tmp_path):
    from pankagent_vnext.query_assistance import advise
    from pankagent_vnext.composable_planning import combine
    import json
    step = operation('intersection')
    proposal = deepcopy(step)
    proposal['operation']['inputs'][0]['role'] = 'source'
    class Good(Gateway):
        async def assist_query_structure(self, payload):
            return {'action':'patch', 'step_json':json.dumps(proposal), 'reason':'verified role'}
    async def run():
        async with service(tmp_path, gateway=Good()) as (_, runtime, *_):
            run = runtime.store.create('Combine verified donors')
            runtime.store.update(run['run_id'], plan={'steps':[
                {**STEP, 'id':'a'}, {**STEP, 'id':'b'}, step],
                'combine_operations':[step['operation']]})
            parents = {'a':evidence('a',['x']), 'b':evidence('b',['x'])}
            parents['a']['edges'] = [{'start_id':'x', 'end_id':'z', 'type':'HAS_SAMPLE', 'properties':{}}]
            corrected = await advise(runtime, run['run_id'], 'combination', step, {}, parents)
            assert corrected['operation'] == proposal['operation']
            assert combine(corrected, parents)['nodes'][0]['id'] == 'x'
    asyncio.run(run())


def test_confirmation_freezes_context_evidence_even_after_ttl(tmp_path):
    async def run():
        graph = RaceGraph()
        graph.late.set()
        plan = deepcopy(PLAN)
        plan['steps'].append({**deepcopy(plan['steps'][0]), 'id':'context', 'purpose':'context'})
        async with service(tmp_path, graph=graph, gateway=Gateway(plan=plan), competing_candidates=True) as (client, runtime, *_):
            created = (await client.post('/v2/plans',json={'question':'INS?'})).json()
            ready = await wait_state(client,created['run_id'],{'awaiting_confirmation'})
            run = runtime.store.get(created['run_id'])
            cache = run['preview_cache']
            cache['step_completed_epochs']['context'] = 1
            runtime.store.update(created['run_id'],preview_cache=cache)
            reads = graph.shared['reads']
            assert (await client.post('/v2/plans/'+ready['plan_id']+'/confirm')).status_code == 202
            await wait_state(client,created['run_id'],{'completed','partial'})
            assert graph.shared['reads'] == reads
    asyncio.run(run())
