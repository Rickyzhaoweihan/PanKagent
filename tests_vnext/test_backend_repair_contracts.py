"""Offline regressions for audited scientific and boundary failures."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import httpx
import pytest
from pankagent_vnext.answer_blocks import catalogue, render, fallback
from pankagent_vnext.app import CitationFilter, normalize_plan, public_run
from pankagent_vnext.evidence_status import synthesis_evidence, query_readiness
from pankagent_vnext.output_scope import project, VERSION
from pankagent_vnext.planning_fastpath import literature_request_plan
from pankagent_vnext.literature_gate import literature_gate
from pankagent_vnext.literature import LiteratureAdapter
from tests_vnext.test_answer_synthesis import gateway_with_mock, detection_evidence


def test_verified_direction_roles_and_invalid_selection():
    step = detection_evidence()['s1']
    step['evidence_id'] = 'G7'
    step['nodes'][0]['properties']['name'] = 'PLEKHM1'
    step['edges'][0]['type'] = 'GENE_ACTIVITY_SCORE_IN'
    step['edges'][0]['properties'] = {'type_1_diabetes_ocr_gene_activity_score_mean':49.6068,
        'non_diabetic_ocr_gene_activity_score_mean':52.9265, 'data_source':'audited-source'}
    facts = catalogue([step])
    answer = render({'fact_ids':[f['id'] for f in facts]}, facts)
    assert '49.6068, lower than ND (52.9265)' in answer
    assert 'PLEKHM1 (Gene)' in answer and 'ATAC' in answer and '[G7]' in answer
    assert 'hormone' not in answer and 'median' not in answer.split('49.6068')[0].split('recorded')[-1]
    with pytest.raises(ValueError):render({'fact_ids':[facts[0]['id']], 'claim':'higher'},facts)
    with pytest.raises(ValueError):render({'fact_ids':['G8:invented']},facts)
    assert CitationFilter(['G7']).feed('[G7] [G1]', final=True) == '[G7] [unverified reference]'


def test_skipped_is_not_executed_empty_and_partial_records_survive():
    step = detection_evidence()['s1'];step.update(status='partial',truncated=True,evidence_id='G11')
    step['retrieval_execution']['cursor_exhausted'] = False
    retained = synthesis_evidence({'s1':step})
    assert retained['s1']['edges'] == step['edges']
    answer = fallback(catalogue(retained))
    assert 'Coverage is incomplete' in answer and 'RNA detection' in answer
    empty = deepcopy(step);empty.update(nodes=[],edges=[],rows=[],status='empty',truncated=False,queries=[])
    empty['execution_status']='skipped_empty_dependency'
    assert 'not executed' in fallback(catalogue([empty]))
    empty.pop('execution_status');empty['queries']=[{'cypher':'MATCH (...) RETURN ...'}]
    empty['retrieval_execution']['cursor_exhausted']=True
    assert 'executed query returned no matching records' in fallback(catalogue([empty]))


def test_aggregate_projection_covers_reopening_edges_rows_and_strings():
    donor='PRIVATE-DONOR-17';sample='PRIVATE-SAMPLE-9'
    step={'status':'complete','truncated':False,'nodes':[
        {'id':donor,'labels':['donor'],'properties':{'name':donor}},
        {'id':sample,'labels':['Sample_node'],'properties':{'data_modality':'RNA'}}],
        'edges':[{'start_id':donor,'end_id':sample,'type':'HAS_SAMPLE'}],
        'rows':[{'id':donor}], 'queries':[{'cypher':donor}], 'generator_attempts':[{'text':donor}]}
    run={'plan':{'output_scope':{'mode':'aggregate_only','version':VERSION}},'evidence':step,
         'graph_answer':f'Identifier {donor}', 'preview':{'evidence':step}}
    before=deepcopy(run);visible=public_run(run)
    assert donor not in json.dumps(visible) and sample not in json.dumps(visible)
    assert not visible['evidence']['nodes'] and not visible['evidence']['edges']
    assert visible['evidence']['donor_summary']['unique_donors']==1
    assert visible['evidence']['aggregate_record_counts']['samples']==1
    assert run==before
    old=deepcopy(run);old['plan']={}
    assert public_run(old)['graph_answer']==old['graph_answer']
    assert '1 unique donors and 1 assay/sample records' in fallback(catalogue([project(step)]))


def test_literature_only_confirmation_and_explicit_mixed_independence():
    question='What does the available HIRN literature say about beta-cell stress in T1D?'
    plan=normalize_plan(literature_request_plan(question))
    assert not plan['steps'] and not plan['clarification']
    assert plan['interpreted_question']==question
    assert query_readiness(plan,{'preparation_complete':True})['ready']
    assert literature_gate(plan,{},'')[0]
    mixed={'steps':[{'id':'s1'}], 'literature':True, 'literature_intent':{'reason':'explicit_request'}}
    assert literature_gate(mixed,{'steps':[{'status':'failed'}]},'')[0]
    assert literature_request_plan('What PanKgraph and HIRN evidence connects IFIH1 to T1D?') is None


@pytest.mark.parametrize('category', ['rerank_timeout','rate_limited','unknown-secret-stack'])
def test_hirn_error_allowlist_and_exact_compatible_request(category):
    async def run():
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200,headers={'content-type':'text/event-stream'},
                text='event: error\ndata: '+json.dumps({'category':category,'trace':'PRIVATE-TRACE'})+'\n\n')
        settings=SimpleNamespace(literature_url='https://fixture.invalid',literature_timeout=1,
                                 corpus_version='fixture',source_policy='mixed')
        adapter=LiteratureAdapter(settings,transport=httpx.MockTransport(handler))
        async def emit(*_):pass
        question='Exact interpreted question?'
        history=[{'role':'user','content':'Earlier question'}, {'role':'assistant','content':'Earlier answer'}]
        try:result=await adapter.search(question,history,emit)
        finally:await adapter.close()
        body=json.loads(requests[0].content)
        assert set(body)=={'question','conversation'} and body['question']==question
        assert body['conversation'][0]['question']=='Earlier question'
        assert result['error_category']==('upstream_error' if category.startswith('unknown') else category)
        assert 'PRIVATE-TRACE' not in json.dumps(result)
        assert len(result['request_diagnostics']['question_sha256'])==64
        assert requests[0].headers['X-Request-ID']==result['request_diagnostics']['request_id']
    asyncio.run(run())


@pytest.mark.parametrize('valid',[True,False])
def test_answer_yields_only_validated_canonical_text(monkeypatch,tmp_path,valid):
    async def run():
        gateway,fake,_=gateway_with_mock(monkeypatch,tmp_path,['UNVERIFIED PROSE MUST NOT DISPLAY'])
        evidence=detection_evidence();prepared=gateway.prepare_answer('Show INS detection',evidence)
        from tests_vnext.test_answer_synthesis import MockStream, USAGE
        async def final(self):
            self.owner.final_messages+=1
            return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda:deepcopy(USAGE)),stop_reason='tool_use',
                content=[SimpleNamespace(type='tool_use',name='select_answer_facts',
                   input={'fact_ids':[f['id'] for f in prepared.facts]} if valid else {'fact_ids':['G99:fake']})])
        monkeypatch.setattr(MockStream,'get_final_message',final)
        try:chunks=[v async for v in gateway.synthesize('Show INS detection',evidence,prepared=prepared)]
        finally:await gateway.close()
        assert len(chunks)==1 and 'UNVERIFIED PROSE' not in chunks[0]
        assert 'RNA detection' in chunks[0]
        assert prepared.generation['answer_validation']['valid'] is valid
        assert ('Partial answer' in chunks[0]) is (not valid)
        assert len(fake.stream_calls)==1 and fake.create_calls==[]
        assert gateway.budget.snapshot()['pending_calls']==0
    asyncio.run(run())


def test_stage_metadata_does_not_change_source_or_add_diagnosis():
    from pankagent_vnext.semantic_registry import resolve, STAGES
    question=('Which HPAP donors have recorded stage-1 T1D metadata and an islet assay? '
              'Return aggregate counts only, distinguish recorded disease stage from the diagnosis category.')
    step={'id':'s2','question':question,'relation_types':['HAS_SAMPLE'],'constraints':[
        {'entity_type':'donor','property':'data_source','operator':'=','value':'HPAP'}],
        'semantic_request':{'source':'user_request','question':question}}
    result=resolve(step,{'sources':['HPAP','Metadata'],'stages':list(STAGES.values()),'modalities':[]},'PanKgraph_08_04')
    assert any(c.get('property')=='data_source' and c['value']=='HPAP' for c in result['constraints'])
    assert not any(c.get('value')=='Metadata' or c.get('entity_type')=='disease' for c in result['constraints'])
    assert any(c.get('property')=='t1d_stage' for c in result['constraints'])
    from pankagent_vnext.semantic_registry import dataset_source_owner, diagnosis_filter_intent
    assert dataset_source_owner('donor.data_source = Metadata', 'Metadata')=='donor'
    assert diagnosis_filter_intent('stage 1 donors with diagnosed T1D')


def test_embedded_source_classifications_use_full_record_distribution():
    from pankagent_vnext.answer_facts import source_classifications
    edges=[]
    for label,count in {'Possible':13,'Moderate':25,'Strong':8,'Causal':8}.items():
        edges += [{'type':'EFFECTOR_GENE_OF','properties':{'evidence':json.dumps([{'summary':f'GENE classified as {label} (overall score 2)'}])}}]*count
    assert source_classifications(edges)=={'Causal':8,'Moderate':25,'Possible':13,'Strong':8}
    edges.append({'type':'EFFECTOR_GENE_OF','properties':{'evidence':'Causal according to a model'}})
    assert source_classifications(edges)['not recorded']==1


def test_literature_only_full_lifecycle_no_graph_or_claude(monkeypatch,tmp_path):
    from pankagent_vnext.app import create_app
    from tests_vnext.test_answer_synthesis import RuntimeGateway, RuntimeGraph, UnrequestedLiterature, await_state, sse_events
    async def run():
        gateway,fake,settings=gateway_with_mock(monkeypatch,tmp_path,[],RuntimeGateway)
        graph=RuntimeGraph()
        class Literature(UnrequestedLiterature):
            calls=[]
            async def search(self,question,history,emit):
                self.calls.append((question,history))
                return {'status':'complete','perspectives':[{'answer':'A fixture literature finding.','references':[],'status':'complete'}]}
        lit=Literature();app=create_app(settings,gateway,graph,lit)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
                question='What does HIRN literature say about beta-cell stress?'
                created=(await client.post('/v2/plans',json={'question':question})).json()
                plan=await await_state(client,created['run_id'],{'awaiting_confirmation','failed'})
                assert plan['status']=='awaiting_confirmation',plan.get('error')
                confirmations=await asyncio.gather(*[client.post(f"/v2/plans/{created['plan_id']}/confirm") for _ in range(2)])
                assert all(c.status_code==202 for c in confirmations)
                final=await await_state(client,created['run_id'],{'completed','partial','failed'})
                assert final['status']=='completed',final.get('error')
                assert graph.calls==gateway.plan_calls==gateway.prepare_calls==0
                assert len(lit.calls)==1 and lit.calls[0]==(question,[])
                assert app.state.runtime.store.history(created['session_id'])[-1]['content'].find('fixture literature finding')>=0
                events=sse_events(await client.get(created['events_url']))
                assert any(e['type']=='literature_complete' for e in events)
                assert fake.stream_calls==fake.create_calls==[]
    asyncio.run(run())


def test_independent_known_identity_checks_and_changed_dependency_scope():
    from pankagent_vnext.dependency_scope import normalize
    plan={'interpreted_question':'independent GWAS and QTL for rs689', 'steps':[
        {'id':'qtl','relation_types':['PART_OF_QTL_SIGNAL'],'constraints':[]},
        {'id':'gwas','relation_types':['PART_OF_GWAS_SIGNAL'],'depends_on':['qtl'],
         'constraints':[{'entity_type':'variants','property':'id','value':'rs689'},
                        {'entity_type':'disease','property':'id','value':'MONDO_0005147'}]}]}
    assert normalize(plan)['steps'][1]['depends_on']==[]
    plan['interpreted_question']='Find the intersection of GWAS and QTL'
    assert normalize(plan)['steps'][1]['depends_on']==['qtl']
    cohort={'steps':[{'id':'donors','relation_types':['HAS_DONOR'],'constraints':[
        {'entity_type':'donor','property':'data_source','value':'HPAP'}]},
        {'id':'samples','relation_types':['HAS_SAMPLE'],'depends_on':['donors'],'constraints':[
        {'entity_type':'donor','property':'data_source','value':'Metadata'}]}]}
    with pytest.raises(ValueError,match='changed_dependency_scope'):normalize(cohort)
    cohort['steps'][1]['constraints']=[]
    assert normalize(cohort)['steps'][1]['constraints']==cohort['steps'][0]['constraints']


def test_summary_reuses_evidence_without_changing_saved_answer():
    from pankagent_vnext.session_summary import requested, plan, answer
    prior={'run_id':'previous', 'status':'completed', 'graph_answer':'Historical answer remains untouched',
           'evidence':{'steps':[detection_evidence()['s1']]}}
    before=deepcopy(prior)
    proposed=plan('Summarize that in one sentence',prior)
    assert proposed['steps']==[] and proposed['plan_mode']=='session_summary'
    assert query_readiness(proposed,{'preparation_complete':True})['ready']
    summary=answer(prior)
    assert '12.34567' in summary and 'no new search was performed' in summary
    assert prior==before
    assert not requested('Summarize the HIRN evidence for a different gene')


def test_explicit_literature_can_be_confirmed_when_graph_is_unavailable():
    plan={'steps':[{'id':'s1','depends_on':[]}], 'literature':True,
          'literature_intent':{'reason':'explicit_request'},'retrieval_policy':'partial_independent_v1'}
    preview={'preparation_complete':True,'evidence':{'steps':[{'step_id':'s1','status':'failed'}]}}
    ready=query_readiness(plan,preview)
    assert ready['ready'] and ready['partial_ready']
    assert ready['verified_step_ids']==[] and ready['blocked_step_ids']==['s1']


def test_referential_go_followup_carries_gene_but_explicit_replacement_does_not():
    from pankagent_vnext.followup_scope import grounding_question
    prior={'plan':{'steps':[{'constraints':[{'entity_type':'Gene','property':'name','value':'CFTR'}]}]}}
    assert grounding_question('which of those GO terms are immune-related?',prior).endswith('gene: CFTR.')
    assert grounding_question('focus only on insulin secretion GO terms',prior).endswith('gene: CFTR.')
    for question in ('What about gene INS?','Now inspect PTPN22','Show rs689 GWAS'):
        assert grounding_question(question,prior)==question
