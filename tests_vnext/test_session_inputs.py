from copy import deepcopy
import pytest
from pankagent_vnext.agent_schemas import active_pack
from pankagent_vnext.session_inputs import population, attach, materialize
from pankagent_vnext.composable_planning import normalize


def prior():
    return {'run_id':'previous','session_id':'session','status':'partial',
      'plan':{'run_context':{'agent_schema':active_pack().identity()},'answer_step_ids':['donors']},
      'evidence':{'steps':[{'step_id':'donors','status':'complete','graph_version':'PanKgraph_08_04',
         'truncated':False,'nodes':[{'id':'D1','labels':['donor'],'properties':{}},
                                   {'id':'D2','labels':['donor'],'properties':{}}],
         'edges':[],'rows':[],'validation':[{'valid':True}],
         'retrieval_execution':{'completed':True,'cursor_exhausted':True}}]}}


def test_reference_uses_full_backend_population_and_typed_chain():
    source=prior();reference=population('Samples for these donors?',source,'PanKgraph_08_04')
    assert reference['count']==2 and 'D1' not in str(reference)
    plan=attach({'steps':[{'id':'samples','question':'Find scRNAseq samples','relation_types':['HAS_SAMPLE'],
                         'constraints':[],'depends_on':[]}]},reference)
    plan=normalize(plan)
    assert plan['execution_mode']=='chain' and plan['answer_step_ids']==['samples']
    child=plan['steps'][1]
    assert child['input_bindings']==[{'step_id':'session_population','entity_type':'donor','source_role':'','target_role':'source'}]
    result=materialize(plan['steps'][0],source,{'session_id':'session'})
    assert {n['id'] for n in result['nodes']}=={'D1','D2'}
    with pytest.raises(ValueError,match='unavailable'):
        materialize(plan['steps'][0],source,{'session_id':'other'})
    source['evidence']['steps'][0]['nodes'].pop()
    with pytest.raises(ValueError,match='stale'):
        materialize(plan['steps'][0],source,{'session_id':'session'})


def test_partial_or_ambiguous_population_is_never_an_unrestricted_parent():
    source=prior();source['evidence']['steps'][0]['truncated']=True
    assert population('these donors',source,'PanKgraph_08_04') is None
    source=prior();other=deepcopy(source['evidence']['steps'][0]);other['step_id']='other';other['nodes'].pop()
    source['plan']['answer_step_ids'].append('other');source['evidence']['steps'].append(other)
    assert population('these donors',source,'PanKgraph_08_04') is None
    source=prior();source['plan']['run_context']['agent_schema']['sha256']='old'
    assert population('these donors',source,'PanKgraph_08_04') is None


def test_model_cannot_supply_a_session_reference():
    plan=attach({'steps':[{'id':'x','question':'Find something','session_input':{'source_run_id':'other'}}]},None)
    assert 'session_input' not in plan['steps'][0]


def test_model_can_name_verified_prior_task_as_dependency():
    ref=population('these donors',prior(),'PanKgraph_08_04')
    plan=normalize(attach({'steps':[{'id':'samples','question':'Samples',
        'relation_types':['HAS_SAMPLE'],'constraints':[], 'depends_on':['donors'],
        'input_bindings':[{'step_id':'donors','entity_type':'donor','source_role':'','target_role':'source'}]}]},ref))
    assert plan['steps'][1]['depends_on']==['session_population']
    assert plan['steps'][1]['input_bindings'][0]['step_id']=='session_population'
    bad=deepcopy(plan);bad['steps']=bad['steps'][1:];bad['steps'][0]['depends_on']=['unverified']
    with pytest.raises(ValueError,match='requires_connected_queries'):
        attach(bad,ref)


def test_stream_storage_avoids_graph_reads_and_preserves_public_records(tmp_path, monkeypatch):
    from pankagent_vnext.store import Store
    from pankagent_vnext.app import public_payload
    store=Store(tmp_path)
    run=store.create('How many donors?')
    key=run['run_id']
    store.update(key,status='running',evidence={'nodes':[
        {'id':'PRIVATE-DONOR','labels':['donor'],'properties':{'name':'Private Name'}}]})
    context=store.event_context(key)
    assert 'PRIVATE-DONOR' in str(public_payload({'text':'PRIVATE-DONOR'},context=context))
    assert 'Private Name' in str(public_payload({'text':'Private Name'},context=context))
    original=store.get
    monkeypatch.setattr(store,'get',lambda *a: (_ for _ in ()).throw(AssertionError('graph read during streaming')))
    store.persist_answer(key,'A supported answer [G1].')
    assert store.run_status(key)=='running'
    store.event_context(key)
    event=store.event_if_active(key,'graph_answer',{'text':'A supported answer [G1].','delta':True})
    assert event['payload']['text']=='A supported answer [G1].'
    monkeypatch.setattr(store,'get',original)
    store.update(key,status='cancelled')
    store.persist_answer(key,'Late text')
    assert store.get(key)['graph_answer']=='A supported answer [G1].'
    assert store.event_if_active(key,'graph_answer',{}) is None
    store.close()
