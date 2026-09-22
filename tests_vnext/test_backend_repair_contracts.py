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
    assert '49.6068, lower than non-diabetic samples (52.9265)' in answer
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


def test_nonaggregate_public_run_removes_unrequested_classifications_and_summary_aliases():
    donor = {'id': 'HPAP-041', 'labels': ['donor'], 'properties': {
        'diabetes_type': 'PRIVATE_TYPE',
        'derived_diabetes_status': 'PRIVATE_STATUS',
        't1d_stage': 'PRIVATE_STAGE',
        'data_source': 'PRIVATE_SOURCE'}}
    evidence = {'nodes': [donor], 'edges': [], 'rows': [],
                'donor_summary': {'rows': [{'donor_id': 'HPAP-041',
                                            'recorded_stage': 'PRIVATE_STAGE'}]},
                'aggregate_cohort_facts': {
                    'recorded_diabetes_type_counts': {'PRIVATE_TYPE': 1},
                    'recorded_derived_diabetes_status_counts': {'PRIVATE_STATUS': 1},
                    'recorded_stage_counts': {'PRIVATE_STAGE': 1},
                    'recorded_source_counts': {'PRIVATE_SOURCE': 1}}}
    run = {'question': 'List donor IDs.',
           'plan': {'steps': [{'question': 'List donor IDs.', 'constraints': []}]},
           'preview': {'evidence': evidence}}
    visible = public_run(run)
    encoded = json.dumps(visible)
    assert 'HPAP-041' in encoded
    for sentinel in ('PRIVATE_TYPE', 'PRIVATE_STATUS', 'PRIVATE_STAGE', 'PRIVATE_SOURCE'):
        assert sentinel not in encoded


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


def test_provider_schema_uses_supported_subset_and_local_length_guard():
    from pankagent_vnext.answer_blocks import TOOL, text
    assert 'maxItems' not in TOOL['input_schema']['properties']['fact_ids']
    # Full provenance/identifiers must never be silently clipped for prose.
    assert len(text('x' * 500)) == 500
    facts = catalogue(detection_evidence())
    with pytest.raises(ValueError): render({'fact_ids':[facts[0]['id']]*25},facts)


def test_evidence_type_grammar_does_not_add_a_gene_alias():
    from tests_vnext.test_preplanning_grounding import FakeGraph, make_index
    graph = FakeGraph()
    graph.rows['Gene'].append({'id':'fixture-type-id','name':'TYPE','labels':['Gene']})
    index = make_index(graph)
    matches = index.match('Show the cell-type expression or enrichment evidence for CFTR. For each evidence type, explain the comparison.')
    assert not any(m['state']=='resolved' and any(c['id']=='fixture-type-id' for c in m['candidates']) for m in matches)
    assert any(m['state']=='resolved' and any(c['id']=='fixture-type-id' for c in m['candidates']) for m in index.match('Show gene TYPE'))


def test_gwas_dependencies_preserve_exact_variants_and_disease():
    from pankagent_vnext.query_templates import compile_variant_dependencies
    from tests_vnext.test_query_templates import _authorized, step, RELEASE
    from pankagent_vnext.graph import validate_cypher
    c={'entity_type':'disease','property':'id','operator':'=','value':'MONDO_0005147'}
    s=step('PART_OF_GWAS_SIGNAL',[c],depends_on=['coloc'],resolved_entities=[{
        'constraint_index':0,'requested':c,'state':'resolved','graph_version':RELEASE,
        'entity_type':'disease','labels':['disease'],'id':c['value']}])
    bindings={'dep_0':{'graph_version':RELEASE,'id_labels':{'rs123':['variants']}}}
    s = _authorized(s)
    query=compile_variant_dependencies(s,bindings)
    assert query and 'a.id IN $dep_0' in query['cypher']
    assert query['parameters']['template_0']==c['value']
    assert query['parameter_bindings']['dep_0'] == {
        'dependency_index':0, 'owner':'variants', 'property':'id', 'operator':'IN',
        'proof_source':'dependency_evidence',
        'proof_kind':'current_graph_dependency_entities',
        'graph_release':RELEASE, 'required_label':'variants'}
    assert validate_cypher(query['cypher'],s,{**query['parameters'],'dep_0':['rs123']},dependency_bindings=bindings)==[]
    bindings['dep_0']['id_labels']={c['value']:['disease']}
    assert compile_variant_dependencies(s,bindings) is None


def test_credible_set_grammar_does_not_add_set_gene():
    from tests_vnext.test_preplanning_grounding import FakeGraph, make_index
    graph = FakeGraph()
    graph.rows['Gene'].append({'id':'fixture-set-id','name':'SET','labels':['Gene']})
    index = make_index(graph)
    matches = index.match('What T1D GWAS evidence is recorded for rs689? Report the credible set and lead-variant context.')
    assert not any(m['state']=='resolved' and any(c['id']=='fixture-set-id' for c in m['candidates']) for m in matches)
    assert any(m['state']=='resolved' and any(c['id']=='fixture-set-id' for c in m['candidates']) for m in index.match('Show gene SET'))


def test_aggregate_projection_accepts_numeric_display_counts():
    r=project({'display':{'nodes':3,'edges':2},'steps':[{'nodes':[], 'edges':[], 'rows':[{'time_minutes':3,'mean_response':.04,'unit':'ng/min'}]}]})
    assert r['display']=={'nodes':3,'edges':2}
    assert r['steps'][0]['rows'][0]['mean_response']==.04


def test_functional_facts_survive_aggregate_projection_without_graph_records():
    step={'status':'complete','nodes':[],'edges':[],'rows':[], 'functional_metadata':{
        'unique_donors':5,'contributing_donors':3,'contributing_donors_by_timepoint':[3]*50,
        'trace_points':[{'time_minutes':3*i,'mean_response':.04,'contributing_donors':3} for i in range(1,51)],
        'y_label':'ng/100 IEQs/min','filters':{'center':'HPAP','disease':'T1D'},'stimuli':[[0,150,'recorded stimulus']]}}
    answer=fallback(catalogue([project(step)]))
    assert 'Selected donor inventory: 5; donors contributing finite values: 3' in answer
    assert '50 timepoints' in answer and 'no records are available' not in answer
    assert 'recorded stimulus' in answer and 'HPAP' in answer


def test_necessary_gene_gwas_input_is_independent_and_acyclic():
    from pankagent_vnext.dependency_scope import compile_inputs
    gene={'entity_type':'Gene','property':'id','operator':'=','value':'verified-gene'}
    disease={'entity_type':'disease','property':'id','operator':'=','value':'verified-disease'}
    p={'steps':[
        {'id':'gwas','relation_types':['PART_OF_GWAS_SIGNAL'],'depends_on':[],'constraints':[gene,disease]},
        {'id':'qtl','relation_types':['PART_OF_QTL_SIGNAL'],'depends_on':[],'constraints':[gene]},
        {'id':'coloc','relation_types':['SIGNAL_COLOC_WITH'],'depends_on':['gwas','qtl'],'constraints':[gene,disease]}]}
    result=compile_inputs(p);byid={s['id']:s for s in result['steps']}
    assert byid['gwas']['depends_on']==['coloc'] and byid['coloc']['depends_on']==[]
    assert byid['gwas']['constraints']==[disease]
    assert [s['id'] for s in result['steps']].index('coloc') < [s['id'] for s in result['steps']].index('gwas')
    assert p['steps'][0]['depends_on']==[]


def test_ssgsea_request_is_honest_zero_cost_unsupported():
    from pankagent_vnext.planning_fastpath import unsupported_analysis_plan
    p=unsupported_analysis_plan('Find T1D effector genes and run ssGSEA on them')
    assert not p['steps'] and 'No ssGSEA analysis was run' in p['clarification']
    assert p['planning_route']['claude_calls']==0
    assert unsupported_analysis_plan('What does ssGSEA mean?') is None


def test_keep_original_sources_rejects_new_category_before_execution():
    from pankagent_vnext.followup_scope import preserve_original_sources
    prior={'run_id':'prior','plan':{'steps':[{'relation_types':['GENE_DETECTED_IN']}]},
           'evidence':{'steps':[{'edges':[{'type':'GENE_DETECTED_IN','properties':{
               'data_source':'recorded-source','data_version':'v1'}}]}]}}
    p={'steps':[{'id':'detection','relation_types':['GENE_DETECTED_IN'],'constraints':[]},
                {'id':'invented-deg','relation_types':['T1D_DEG_IN'],'constraints':[]}]}
    r=preserve_original_sources(p,'Now restrict to beta cells. Keep the original sources.',prior)
    assert [s['id'] for s in r['steps']]==['detection']
    assert {c['property']:c['value'] for c in r['steps'][0]['constraints']}=={'data_source':'recorded-source','data_version':'v1'}
    assert r['followup_source_scope']['rejected_unrequested_step_ids']==['invented-deg']
    assert len(p['steps'])==2
    p['steps'][0]['constraints']=[{'property':'data_source','value':'new-source'}]
    with pytest.raises(ValueError,match='changed_original_source_scope'):
        preserve_original_sources(p,'Keep the original sources',prior)


def test_signal_comparison_is_record_operation_not_new_join():
    from pankagent_vnext.signal_comparison import compile_comparisons, RELEASE
    def parent(identifier, relation, identities, depends=()):
        cs=[{'entity_type':kind,'property':'id','operator':'=','value':value} for kind,value in identities]
        return {'id':identifier,'relation_types':[relation],'constraints':cs,'depends_on':list(depends),
                'complete':True,'graph_version':RELEASE,'evidence_combination':'independent',
                'resolved_entities':[{'constraint_index':i,'entity_type':c['entity_type'],
                    'id':c['value'],'name':{'gene':'ADCY3','disease':'T1D'}.get(c['value']),
                    'requested':c,'state':'resolved','graph_version':RELEASE} for i,c in enumerate(cs)]}
    parents=[parent('c','SIGNAL_COLOC_WITH',[('Gene','gene'),('disease','disease')]),
             parent('q','PART_OF_QTL_SIGNAL',[('Gene','gene')]),
             parent('g','PART_OF_GWAS_SIGNAL',[('disease','disease')],['q'])]
    compare={'id':'compare','relation_types':['SIGNAL_COLOC_WITH','PART_OF_QTL_SIGNAL','PART_OF_GWAS_SIGNAL'],
             'depends_on':['c','q','g'],'constraints':[],'complete':True,
             'question':'Do the retrieved coloc gwas_signal_id/qtl_signal_id and lead variants match the identifiers found in the separate QTL and GWAS signal records for ADCY3 and T1D?'}
    result=compile_comparisons({'steps':parents+[compare]},RELEASE)
    assert len(result['steps'])==3
    assert result['record_comparison_operations'][0]['no_new_retrieval']
    compare['question']+=' Only include European cohorts.'
    assert len(compile_comparisons({'steps':parents+[compare]},RELEASE)['steps'])==4


def test_exact_signal_memberships_retain_cross_evidence_citations_and_tissue():
    from pankagent_vnext.signal_comparison import RELEASE
    def step(eid,edges):return {'evidence_id':eid,'status':'complete','graph_version':RELEASE,'nodes':[],'edges':edges,'rows':[]}
    coloc={'type':'SIGNAL_COLOC_WITH','start_id':'gene','end_id':'disease','properties':{
        'gwas_signal_id':'LOC__credibleSet1__selected','qtl_signal_id':'qtl1','coloc_dataset':'t1d_eQTL-inspire_coloc'}}
    gwas={'type':'PART_OF_GWAS_SIGNAL','start_id':'rs1','end_id':'disease','properties':{'credible_set_id':'LOC__credibleSet1'}}
    qtl={'type':'PART_OF_QTL_SIGNAL','start_id':'rs2','end_id':'gene','properties':{
        'credible_set':'qtl1','data_source':'INSPIRE; SusieR','tissue_id':'UBERON_0000006'}}
    steps=[step('G1',[coloc]),step('G2',[gwas]),step('G3',[qtl])]
    facts=catalogue(steps);fact=next(f for f in facts if f['kind']=='signal_membership')
    assert fact['supporting_evidence_ids']==['G2','G3']
    assert 'rs1' in fact['text'] and 'rs2' in fact['text']
    assert '[G1] [G2] [G3]' in render({'fact_ids':[]},facts)
    qtl['properties']['tissue_id']='wrong-tissue'
    fact=next(f for f in catalogue(steps) if f['kind']=='signal_membership')
    assert fact['supporting_evidence_ids']==['G2'] and 'rs2' not in fact['text']


def test_aggregate_cohort_suppresses_unrequested_classifications_but_keeps_assay_denominators():
    step={'evidence_id':'G1','status':'complete','truncated':False,'nodes':[
        {'id':'private-donor','labels':['donor'],'properties':{
            'diabetes_type':'Control Without Diabetes','derived_diabetes_status':'Prediabetes',
            't1d_stage':'Stage 1','data_source':'HPAP'}},
        {'id':'private-sample-a','labels':['Sample_node'],'properties':{'data_modality':'scRNA-seq'}},
        {'id':'private-sample-b','labels':['Sample_node'],'properties':{'data_modality':'scRNA-seq'}}],
        'edges':[{'type':'HAS_SAMPLE','start_id':'private-donor','end_id':s} for s in ['private-sample-a','private-sample-b']]}
    visible=project(step);answer=fallback(catalogue([visible]))
    assert 'Control Without Diabetes' not in answer
    assert 'Prediabetes' not in answer and 'Stage 1' not in answer
    assert 'Recorded donor sources' not in answer
    assert not any(key.startswith('recorded_') for key in visible['aggregate_cohort_facts'])
    assert '2 assay/sample records linked to 1 unique donors' in answer
    assert 'private-' not in json.dumps(visible) and 'private-' not in answer
    assert project(visible)['aggregate_cohort_facts']==visible['aggregate_cohort_facts']


def test_model_evidence_context_suppresses_unrequested_donor_classifications():
    from pankagent_vnext.evidence_context import compact_evidence
    step = {'evidence_id': 'G1', 'question': 'Count spleen samples.',
        'status': 'complete', 'truncated': False, 'nodes': [
            {'id': 'donor-private', 'labels': ['donor'], 'properties': {
                'diabetes_type': 'PRIVATE_DIABETES_SENTINEL',
                'derived_diabetes_status': 'PRIVATE_DERIVED_SENTINEL',
                't1d_stage': 'PRIVATE_STAGE_SENTINEL',
                'data_source': 'PRIVATE_SOURCE_SENTINEL'}},
            {'id': 'sample-private', 'labels': ['Sample_node'],
             'properties': {'data_modality': 'scRNA-seq'}}],
        'edges': [{'type': 'HAS_SAMPLE', 'start_id': 'donor-private',
                   'end_id': 'sample-private'}], 'rows': [],
        'requested_scope': {'original_question': 'Count spleen samples.',
                            'constraints': [], 'relation_types': ['HAS_SAMPLE']}}
    compact = compact_evidence([step])
    serialized = json.dumps(compact)
    assert 'PRIVATE_DIABETES_SENTINEL' not in serialized
    assert 'PRIVATE_DERIVED_SENTINEL' not in serialized
    assert 'PRIVATE_STAGE_SENTINEL' not in serialized
    assert 'PRIVATE_SOURCE_SENTINEL' not in serialized


def test_full_record_cohort_facts_are_mandatory_and_nd_mismatch_fails_closed():
    from pankagent_vnext.answer_facts import build_answer_facts
    from pankagent_vnext.release_schema import REGISTRY
    step={'evidence_id':'G1','question':'How many HPAP spleen samples are ND/healthy (not type 1 diabetes)?',
        'graph_version':REGISTRY['release'],'status':'complete','truncated':False,
        'requested_scope':{'constraints':[],'relation_types':['HAS_SAMPLE']},
        'nodes':[
            {'id':'private-donor','labels':['donor'],'properties':{
                'diabetes_type':'Diabetes (Type I)','derived_diabetes_status':'Diabetes',
                't1d_stage':'Stage 3','data_source':'HPAP'}},
            {'id':'private-sample','labels':['Sample_node'],'properties':{'data_modality':'snMultiomics'}}],
        'edges':[{'type':'HAS_SAMPLE','start_id':'private-donor','end_id':'private-sample'}],
        'rows':[]}
    step['answer_facts']=build_answer_facts(step)
    facts=catalogue([step])
    assert all(fact['mandatory'] for fact in facts)
    answer=render({'fact_ids':[]},facts)
    assert 'Cohort integrity check failed' in answer
    assert 'Diabetes (Type I): 1 donors' in answer
    assert 'Stage 3: 1 donors' not in answer
    assert 'Recorded derived diabetes classifications' not in answer
    assert 'cannot be labeled as the requested cohort' in answer
    assert 'private-donor' not in json.dumps(facts) and 'private-sample' not in json.dumps(facts)


def test_tool_markup_clarification_is_planning_error_after_one_repair():
    from pankagent_vnext.llm import plan_structure_issue
    from pankagent_vnext.plan_recovery import recover_empty_plan
    malformed={'interpreted_question':'Question','steps':[], 'clarification':'null</ ant ml:para meter>\n'}
    assert plan_structure_issue(malformed)=='malformed_plan'
    class Gateway:
        calls=0
        async def plan(self,*args,**kwargs):self.calls+=1;return malformed
    gateway=Gateway()
    result=asyncio.run(recover_empty_plan(gateway,malformed,'Keep original sources',[],2))
    assert gateway.calls==1 and result['recovery']['category']=='planning_failure'
    assert '<' not in json.dumps(result) and result['interpreted_question']=='Keep original sources'


def test_session_summary_is_one_sentence_without_changing_decimal_facts():
    import re
    from pankagent_vnext.session_summary import answer
    e=detection_evidence()['s1'];e['edges'][0]['properties']['recorded_value']=12.34567
    summary=answer({'run_id':'earlier','status':'completed','evidence':{'steps':[e]}})
    assert '12.34567' in summary
    assert len(re.findall(r'[.!?](?=\s|$)',summary))==1


def test_genetic_interaction_check_does_not_report_unqueried_physical_zero():
    e=detection_evidence()['s1'];e['nodes'][1]['labels']=['Gene']
    e['edges'][0]['type']='GENETIC_INTERACTION'
    answer=fallback(catalogue([e]))
    assert 'Genetic interaction' in answer
    assert 'Physical interactions' not in answer


def test_explicit_hirn_request_survives_partial_graph_answer():
    from pankagent_vnext.literature_policy import apply_literature_policy
    p=apply_literature_policy({'steps':[{'id':'s1'}]},'What PanKgraph and HIRN evidence connects IFIH1 to T1D?')
    assert p['literature_intent']['reason']=='explicit_request'
    assert literature_gate(p,{'steps':[{'status':'failed'}]},'')[0]
    assert not apply_literature_policy({},'Use graph only; no HIRN')['literature']


def test_independent_bundles_preserve_gene_disease_and_required_variant_input():
    from pankagent_vnext.independent_checks import split
    from pankagent_vnext.dependency_scope import compile_inputs
    gene={'entity_type':'Gene','property':'id','value':'verified-gene','operator':'='}
    disease={'entity_type':'disease','property':'id','value':'verified-disease','operator':'='}
    p={'steps':[{'id':'signals','relation_types':['PART_OF_GWAS_SIGNAL','PART_OF_QTL_SIGNAL','SIGNAL_COLOC_WITH'],
        'constraints':[gene,disease],'depends_on':[],'evidence_combination':'independent','complete':True}]}
    r=compile_inputs(split(p,'Connect gene to T1D using separate evidence'))
    byrel={s['relation_types'][0]:s for s in r['steps']}
    assert len(r['steps'])==3
    assert byrel['PART_OF_GWAS_SIGNAL']['depends_on']==[byrel['SIGNAL_COLOC_WITH']['id']]
    assert byrel['PART_OF_GWAS_SIGNAL']['constraints']==[disease]
    assert byrel['PART_OF_QTL_SIGNAL']['constraints']==[gene]
    assert byrel['SIGNAL_COLOC_WITH']['constraints']==[gene,disease]
    assert len(split(p,'Find the intersection of these signals')['steps'])==1
    p['steps'][0]['constraints'].append({'property':'unknown','value':'must preserve'})
    assert len(split(p,'Connect gene to T1D')['steps'])==1


def test_independent_assay_bundle_preserves_tissue_and_source_without_rna_atac_join():
    from pankagent_vnext.independent_checks import split
    gene={'entity_type':'Gene','property':'id','operator':'=','value':'ENSG_FIXTURE'}
    tissue={'entity_type':'anatomical_structure','property':'name','operator':'=','value':'beta cell'}
    source={'relationship_type':'GENE_DETECTED_IN','property':'data_source','operator':'=','value':'audited_source'}
    plan={'steps':[{'id':'s1','relation_types':['GENE_DETECTED_IN','GENE_ACTIVITY_SCORE_IN'],
        'constraints':[gene,tissue,source],'depends_on':[],'evidence_combination':'independent'}]}
    steps=split(plan,'Summarize RNA detection and ATAC activity')['steps']
    assert len(steps)==2
    assert steps[0]['constraints']==[gene,tissue,source]
    assert steps[1]['constraints']==[gene,tissue]
    assert steps[0]['relation_types']==['GENE_DETECTED_IN']
    assert steps[1]['relation_types']==['GENE_ACTIVITY_SCORE_IN']
    assert len(split(plan,'Find the intersection of RNA and ATAC evidence')['steps'])==1
    plan['steps'][0]['constraints'].append({'entity_type':'anatomical_structure','property':'unknown_scope','value':'x'})
    assert len(split(plan,'Summarize RNA and ATAC')['steps'])==1


def test_independent_effector_and_coloc_do_not_require_a_join():
    from pankagent_vnext.independent_checks import split
    gene={'entity_type':'Gene','property':'id','operator':'=','value':'ENSG_FIXTURE'}
    disease={'entity_type':'disease','property':'id','operator':'=','value':'EFO_FIXTURE'}
    plan={'steps':[{'id':'s1','relation_types':['EFFECTOR_GENE_OF','SIGNAL_COLOC_WITH'],
        'constraints':[gene,disease],'depends_on':[],'evidence_combination':'independent'}]}
    result=split(plan,'Summarize independent gene evidence')
    assert len(result['steps'])==2
    assert all(s['constraints']==[gene,disease] for s in result['steps'])
    plan['steps'][0]['evidence_combination']='intersection'
    assert len(split(plan,'Summarize gene evidence')['steps'])==1


def test_historical_tell_me_profile_uses_registered_scope_without_losing_modifiers():
    from pankagent_vnext.investigations import generic_profile_gene, expand_registered_profile, GENERIC_GENE_CATEGORIES
    from pankagent_vnext.independent_checks import split
    for gene in ('INS','PTPN22'):
        question='Tell me about gene '+gene
        assert generic_profile_gene(question)==gene
        plan=split(expand_registered_profile(question,gene),question)
        assert len(plan['steps'])==12
        assert {r for s in plan['steps'] for r in s['relation_types']}==(GENERIC_GENE_CATEGORIES-{'FGSEA_ENRICHED_IN'})|{'GENE_ACTIVITY_SCORE_IN'}
    assert generic_profile_gene('Tell me about gene INS in beta cells') is None
    assert generic_profile_gene('Tell me about gene INS and PTPN22') is None


def test_registered_profile_carries_t1d_scope_only_to_compatible_checks():
    from pankagent_vnext.investigations import generic_profile_gene, expand_registered_profile
    question='Tell me about the gene PTPN22 in T1D'
    assert generic_profile_gene(question)=='PTPN22'
    assert generic_profile_gene('Tell me about PTPN22 in T1D')=='PTPN22'
    assert generic_profile_gene('Tell me about stress in T1D') is None
    plan=expand_registered_profile(question,'PTPN22')
    assert len(plan['steps'])==12
    for step in plan['steps']:
        disease=[c for c in step['constraints'] if c['entity_type']=='disease']
        assert bool(disease)==(step['relation_types'][0] in {'EFFECTOR_GENE_OF','SIGNAL_COLOC_WITH'})
        if disease: assert disease[0]['value']=='type 1 diabetes'
    assert generic_profile_gene(question+' in beta cells') is None


def test_spatial_variant_scope_never_substitutes_empty_qtl_search():
    from pankagent_vnext.planning_fastpath import genomic_neighborhood_plan
    for question in ('now tell me about the INS gene and what SNPs are near it',
                     'which of those SNPs is inside the gene body?'):
        plan=genomic_neighborhood_plan(question)
        assert plan['steps']==[] and plan['interpreted_question']==question
        assert plan['planning_route']['claude_calls']==0
        assert 'No neighborhood or gene-body search was executed' in plan['clarification']
    assert genomic_neighborhood_plan('Which QTL variants are associated with INS?') is None


def test_bounded_executed_empty_annotation_is_not_a_retrieval_failure():
    from pankagent_vnext.evidence_status import outcome_message, executed_without_records
    step={'status':'partial','title':'GO name contains insulin secretion','evidence_id':'G7',
          'nodes':[],'edges':[],'rows':[],'queries':[{'cypher':'verified_fixture'}],
          'retrieval_execution':{'completed':True,'cursor_exhausted':True},'truncated':False}
    assert executed_without_records(step)
    answer=outcome_message([step])
    assert 'executed query returned no matching records within its recorded filters' in answer
    assert 'not an exhaustive absence claim or a retrieval failure' in answer
    assert '[G7]' in answer
    step['retrieval_execution']['completed']=False
    assert not executed_without_records(step)
    assert 'couldn’t retrieve' in outcome_message([step])


def test_annotation_overview_does_not_claim_immune_specificity_ranking():
    from pankagent_vnext.answer_blocks import catalogue, fallback
    evidence=[{'evidence_id':'G1','status':'partial','nodes':[{'id':'g','labels':['Gene']}],
        'requested_scope':{'retrieval_selection':{'mode':'annotation_overview','ordering':'stable_identifiers'}}}]
    answer=fallback(catalogue(evidence))
    assert 'not a ranking by biological importance or immune specificity' in answer


def test_overview_keeps_atac_while_comprehensive_profile_keeps_registered_fgsea():
    from pankagent_vnext.investigations import expand_registered_profile
    overview=expand_registered_profile('Tell me about PTPN22 in T1D','PTPN22')
    comprehensive=expand_registered_profile('comprehensive gene profile for PTPN22','PTPN22')
    types=lambda p:{r for s in p['steps'] for r in s['relation_types']}
    assert 'GENE_ACTIVITY_SCORE_IN' in types(overview) and 'FGSEA_ENRICHED_IN' not in types(overview)
    assert 'FGSEA_ENRICHED_IN' in types(comprehensive)
    assert all(not s['depends_on'] for s in overview['steps'])


def test_neighborhood_guard_preserves_known_rs689_gwas_identity():
    from pankagent_vnext.planning_fastpath import genomic_neighborhood_plan
    question=('What T1D GWAS or fine-mapping evidence is recorded for rs689 near INS? '
              'Report the recorded disease, source, credible set and lead-variant context.')
    assert genomic_neighborhood_plan(question) is None
    assert genomic_neighborhood_plan('Which GWAS SNPs are near rs689?') is not None


def test_strict_coloc_tissue_scope_uses_verified_dataset_mapping():
    from pankagent_vnext.coloc_tissue_scope import compile_scope
    from pankagent_vnext.coloc_scope import RELEASE, QTL_CONTEXT
    grounding={'identity':{'graph_release':RELEASE},'schema':{'categories':{
        'PART_OF_QTL_SIGNAL.tissue_name':['Pancreas','Islet'],
        'PART_OF_QTL_SIGNAL.tissue_id':['UBERON_0001264','UBERON_0000006']}}}
    plan={'steps':[{'id':'s1','relation_types':['SIGNAL_COLOC_WITH'],'constraints':[
        {'entity_type':'Gene','property':'id','value':'ENSG_FIXTURE'}]}]}
    result,issue=compile_scope('Show coloc specifically in pancreatic or islet tissue',grounding,plan)
    assert issue is None
    assert result['steps'][0]['constraints'][-1]['value']==sorted(QTL_CONTEXT)
    result,issue=compile_scope('Show coloc in islet tissue',grounding,plan)
    assert issue is None and len(result['steps'][0]['constraints'][-1]['value'])==2
    assert all(QTL_CONTEXT[k][1]=='UBERON_0000006' for k in result['steps'][0]['constraints'][-1]['value'])
    assert compile_scope('Show coloc in islet tissue',{},plan)[1]=='unverified_coloc_tissue_mapping'
    assert compile_scope('Show coloc and describe tissue context',grounding,plan)==(plan,None)
    assert compile_scope('Show coloc in pancreas and islet tissue',grounding,plan)[1].startswith('ambiguous')


def test_atac_gene_activity_is_assay_language_not_a_second_gene():
    from tests_vnext.test_preplanning_grounding import FakeGraph, make_index
    graph=FakeGraph();graph.rows['Gene'].append({'id':'fixture-atac-id','name':'ATAC','labels':['Gene']})
    index=make_index(graph)
    matches=index.match('Compare mean ATAC gene activity for CFTR. Keep ATAC distinct from RNA.')
    assert not any(m['state']=='resolved' and any(c['id']=='fixture-atac-id' for c in m['candidates']) for m in matches)
    assert any(m['state']=='resolved' and any(c['id']=='fixture-atac-id' for c in m['candidates']) for m in index.match('Show gene ATAC'))


def test_cohort_sample_request_cannot_degrade_to_donor_inventory():
    from pankagent_vnext.cohort_plan_scope import compile_scope
    predicate={'entity_type':'donor','property':'data_source','value':'audited-source'}
    plan={'steps':[{'id':'s1','question':'Count donors','relation_types':['HAS_DONOR'],
        'constraints':[predicate],'depends_on':[]}]}
    question='Which donors have an islet assay? Return aggregate donor and assay-record counts only.'
    result,issue=compile_scope(question,{},plan)
    assert issue is None
    assert result['steps'][0]['relation_types']==['HAS_SAMPLE']
    assert result['steps'][0]['constraints']==[predicate]
    assert result['steps'][0]['question']==question
    assert compile_scope('Count donors',{},plan)==(plan,None)
    molecular={'steps':[{'id':'s1','relation_types':['GENE_DETECTED_IN'],'constraints':[]}]}
    assert compile_scope('Show the gene assay context without donor identifiers',{},molecular)==(molecular,None)
    assert compile_scope('Count donors without samples',{},plan)[1]=='missing_requested_category:HAS_SAMPLE'


def test_unqueried_sample_inventory_is_unknown_not_zero():
    step={'evidence_id':'G1','status':'complete','truncated':False,
          'nodes':[{'id':'private-donor','labels':['donor'],'properties':{}}],'edges':[]}
    visible=project(step)
    assert visible['aggregate_record_counts']['samples'] is None
    answer=fallback(catalogue([visible]))
    assert 'assay/sample record count unavailable' in answer
    assert '0 assay' not in answer and 'private-donor' not in answer
