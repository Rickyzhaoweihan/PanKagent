import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pankagent_vnext.task_preparation import bind_unique_requested_identities, independent_subset
from pankagent_vnext.entity_lookup import retain_grounding_proofs, valid_selection
from pankagent_vnext.planning_session import run
from test_claude_led_planning import graph, gateway, SCHEMA, PLAN


def grounding(name='INS'):
    return {'status':'ready','identity':{'graph_release':'PanKgraph_08_04'},'mentions':[
        {'requested':name,'state':'resolved','candidates':[{'id':'g1','name':name,
         'entity_type':'Gene','labels':['Gene'],'match_kind':'recorded_name'}]}]}


def test_initial_proof_reused_without_lookup_and_cannot_change_id():
    async def check():
        db=graph([]); g=retain_grounding_proofs(db,grounding('TM4SF6'))
        proof=g['mentions'][0]['candidates'][0]['selection_proof']
        assert valid_selection(db,proof,'Find TM4SF6')
        assert not valid_selection(db,{**proof,'id':'invented'},'Find TM4SF6')
        model,calls=gateway([('record_plan',{**PLAN,'entity_choices':[{'mention':'TM4SF6',
            'entity_type':'Gene','id':'g1','reason':'verified unique identity'}]})])
        resolver=AsyncMock()
        result=await run(model,'Find TM4SF6','{}','test',SCHEMA,500,lambda p,c:p,
                         resolver=resolver,initial_proofs=[proof])
        assert len(calls)==1 and not result.get('clarification')
        resolver.assert_not_called()
    asyncio.run(check())


def test_binding_missing_anchor_never_reverses_negative_or_overwrites_predicate():
    plan={'steps':[{'id':'q1','relation_types':['GENE_EXPRESSED_IN'],'constraints':[]}]}
    # Use the actual schema relation supported by the release.
    from pankagent_vnext.release_schema import REGISTRY
    relation=next(k for k,v in REGISTRY['relations'].items() if any('Gene' in p['source'] for p in v['paths']))
    plan['steps'][0]['relation_types']=[relation]
    fixed=bind_unique_requested_identities('Find INS',grounding(),plan)
    assert fixed['steps'][0]['constraints'][0]['value']=='g1'
    assert not bind_unique_requested_identities('Exclude INS',grounding(),plan)['steps'][0]['constraints']
    specified=deepcopy(plan);specified['steps'][0]['constraints']=[{'entity_type':'Gene','property':'id','value':'other'}]
    assert bind_unique_requested_identities('Find INS',grounding(),specified)==specified


def test_failed_task_closure_handles_non_topological_order_and_preserves_sibling():
    plan={'steps':[{'id':'child2','depends_on':['child1']},{'id':'child1','depends_on':['bad']},
                   {'id':'good','question':'independent evidence'},{'id':'bad','question':'failed check'}],
          'answer_step_ids':['good','child2']}
    subset=independent_subset(plan,'missing_requested_scope:Gene:INS:bad')
    assert [s['id'] for s in subset['steps']]==['good']
    assert subset['answer_step_ids']==['good']
    assert {c['step_id'] for c in subset['unmet_checks']}=={'bad','child1','child2'}
    assert independent_subset(plan,'unknown_global_failure') is None


def test_specific_binding_diagnostic_keeps_reason_without_entity_values():
    from pankagent_vnext.diagnostics import diagnostic
    d=diagnostic('missing_requested_scope:Gene:PRIVATE_VALUE:q1','planning')
    assert d['code']=='E04.REQUESTED_ENTITY_BINDING_MISSING'
    assert 'PRIVATE_VALUE' not in str(d)


def test_persistent_execution_repair_claims_share_run_limit(tmp_path):
    from pankagent_vnext.store import Store
    from pankagent_vnext.agent_schemas import module
    store=Store(tmp_path)
    r=store.create('test')
    store.update(r['run_id'],plan={'steps':[],'planning_route':{'claude_calls':5}})
    limits=module('validation_repair')['limits']
    assert store.claim_execution_repair(r['run_id'],'q1',limits)
    assert store.claim_execution_repair(r['run_id'],'q2',limits)
    assert not store.claim_execution_repair(r['run_id'],'q3',limits)
    store.close()
    store=Store(tmp_path)
    assert not store.claim_execution_repair(r['run_id'],'q1',limits)
    store.close()


def test_splitting_independent_categories_remaps_answer_selection():
    from pankagent_vnext.independent_checks import split
    from pankagent_vnext.composable_planning import normalize
    p={'steps':[{'id':'s1','question':'INS expression and markers',
         'relation_types':['GENE_DETECTED_IN','MARKER_GENE_OF'],
         'constraints':[{'entity_type':'Gene','property':'name','operator':'=','value':'INS'}]}],
       'answer_step_ids':['s1']}
    revised=split(p,'Show INS expression and marker evidence')
    assert revised['answer_step_ids']==['s1_check_1','s1_check_2']
    assert normalize(revised)['answer_step_ids']==revised['answer_step_ids']


def test_case_and_cell_plural_normalization_never_authorizes_absent_mention():
    from pankagent_vnext.entity_lookup import mention_in_request
    assert mention_in_request('Ductal cell','Is CFTR enriched in ductal cells?')
    assert mention_in_request('beta cell','Is INS beta-cell restricted?')
    assert not mention_in_request('alpha cell','Is INS beta-cell restricted?')


def test_nd_after_donor_phrase_and_explanatory_inventory_value():
    from pankagent_vnext.semantic_registry import control_cohort_polarity, _control_category
    assert control_cohort_polarity('How many donors in HPAP for ND?')['positive']
    assert control_cohort_polarity('How many donors in HPAP not ND?')['negative']
    vocabulary={'donor_categories_complete':True,'donor_categorical_values':{'diabetes_type':[
        'Control Without Diabetes', 'Controlled Wording\nDiabetes (Type I)\nControl Without Diabetes\nSource https://example.org']}}
    assert _control_category(vocabulary)=='Control Without Diabetes'


def test_t1d_donor_modifier_and_explicit_assay_are_not_reinterpreted():
    from pankagent_vnext.semantic_registry import _unresolved_donor_modifier, _assay_intent_values
    assert not _unresolved_donor_modifier('samples from HPAP T1D donors',{})
    available=['scRNA-seq','snMultiomics']
    assert _assay_intent_values('scRNA-seq samples',available)[0]=={'scRNA-seq'}
    assert _assay_intent_values('scRNA-seq or snMultiomics samples including RNA components',available)[0]==set(available)


def test_all_sample_query_does_not_become_donor_only_without_assay():
    from pankagent_vnext.semantic_registry import sample_lookup_requested, generation_guidance
    from pankagent_vnext.graph_contract import generation_request
    s={'question':'Retrieve samples from verified donors','relation_types':['HAS_SAMPLE'],
       'semantic_request':{'source':'user_request','question':'How many samples from HPAP ND donors?'},
       'semantic_registry':{'donor_required':True},'sample_requirements':{},'constraints':[]}
    assert sample_lookup_requested(s)
    prompt=generation_request(s,s['question'])
    assert 'Donor-only lookup' not in prompt
    assert 'no required tissue join' in prompt
    assert 'DONOR-ONLY lookup' not in generation_guidance(s)
    assert not sample_lookup_requested({**s,'relation_types':['HAS_DONOR'],'deferred_sample_scope':True})


def test_chain_transfers_unused_reservation_without_starving_parallel_siblings():
    from types import SimpleNamespace
    from pankagent_vnext.annotation_selection import allocate_independent_budgets, inherited_budget
    plan={'steps':[{'id':'a'},{'id':'b','depends_on':['a']},{'id':'c','depends_on':['a']},{'id':'x'}]}
    allocate_independent_budgets(plan,SimpleNamespace(max_nodes=100,max_edges=100,max_bytes=1000,max_rows=100))
    a,b,c,x=plan['steps']
    parent={'status':'complete','resource_budget':{'allocated':a['retrieval_budget'],
            'consumed':{'max_nodes':5,'max_edges':5,'max_bytes':50,'max_rows':5}}}
    b_budget=inherited_budget(b,{'a':parent});c_budget=inherited_budget(c,{'a':parent})
    assert b_budget['max_nodes']==c_budget['max_nodes']==35
    assert b_budget['max_nodes']+c_budget['max_nodes']+5+x['retrieval_budget']['max_nodes']==100
    assert inherited_budget(x,{'a':parent})==x['retrieval_budget']
    assert inherited_budget(b,{'a':{**parent,'status':'partial'}})==b['retrieval_budget']


def test_nd_and_t1d_request_preserves_separate_positive_cohorts():
    from tests_vnext.test_runtime_clinical_resolution import VOCAB, RELEASE
    from pankagent_vnext.semantic_registry import resolve
    question='How many ND and T1D donors and samples are available in HPAP?'
    results=[]
    for cohort in ('ND','T1D'):
        result=resolve({'id':cohort,'question':f'Find HPAP {cohort} donors',
            'relation_types':['HAS_DONOR'] if cohort=='T1D' else [],'constraints':[],
            'semantic_request':{'source':'user_request','question':question}},VOCAB,RELEASE)
        assert not result['semantic_issues']
        results.append(result['constraints'])
    assert any(c.get('property')=='diabetes_type' and c['value']=='Control Without Diabetes' for c in results[0])
    assert not any(c.get('entity_type')=='disease' for c in results[0])
    assert any(c.get('entity_type')=='disease' and c['value']=='CURRENT_T1D' for c in results[1])
    assert not any(c.get('property')=='diabetes_type' for c in results[1])


def test_large_public_identity_projection_preserves_all_identifiers():
    from pankagent_vnext.output_scope import project
    nodes=[{'id':f'SAMPLE-{i:05d}','labels':['Sample_node'],'properties':{}} for i in range(5500)]
    nodes += [{'id':'SAMPLE-00000-extra','labels':['Sample_node'],'properties':{}}]
    value={'nodes':nodes,'note':'SAMPLE-00000-extra, SAMPLE-05499; xSAMPLE-00000z and nothing else.'}
    result=project(value)
    assert result['nodes']==nodes
    assert result['note']==value['note']


def test_aggregate_presentation_preserves_public_records_and_input():
    from pankagent_vnext.output_scope import project
    fixture={'nodes':[{'id':'DONOR-A','labels':['donor'],'properties':{'name':'Donor Alpha'}},
                      {'id':'SAMPLE-1','labels':['Sample_node'],'properties':{'name':'Sample one'}},
                      {'id':'TISSUE-X','labels':['anatomical_structure'],'properties':{'name':'tissue'}}],
             'note':'DONOR-A has SAMPLE-1; not xDONOR-Ax. Sample one is recorded.',
             'rows':[{'donor_id':'DONOR-A','sample_id':'SAMPLE-1'}],
             'answer_facts':{'unique_donors':1,'unique_samples':1},'queries':[]}
    original=deepcopy(fixture)
    result=project(fixture)
    assert fixture==original
    for field in ('nodes','rows','note','answer_facts','queries'):
        assert result[field]==fixture[field]


def test_two_request_aliases_for_same_verified_id_do_not_force_lookup():
    async def check():
        db=graph([])
        g=grounding('TM4SF6');g['mentions'].append({**deepcopy(g['mentions'][0]),'requested':'TSPAN6'})
        g=retain_grounding_proofs(db,g)
        proofs=[m['candidates'][0]['selection_proof'] for m in g['mentions']]
        model,calls=gateway([('record_plan',{**PLAN,'entity_choices':[{'mention':'canonical label',
            'entity_type':'Gene','id':'g1','reason':'same verified identity'}]})])
        resolver=AsyncMock()
        result=await run(model,'Find TM4SF6 (TSPAN6)','{}','test',SCHEMA,500,lambda p,c:p,
            resolver=resolver,initial_proofs=proofs)
        assert len(calls)==1 and not result.get('clarification')
        assert result['entity_selection_proofs'][0]['mention'] in {'TM4SF6','TSPAN6'}
        resolver.assert_not_called()
    asyncio.run(check())
