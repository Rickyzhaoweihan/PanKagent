from copy import deepcopy
from pankagent_vnext.term_clarification import recovery, source_role_text, repair_generated_scope
from pankagent_vnext.tissue_aliases import PLN_ID, PLN_NAME
from pankagent_vnext.plan_recovery import empty_failure
V = {'inventory_complete': True, 'donor_sources': ['HPAP'],
     'tissues': [{'id':PLN_ID,'name':PLN_NAME},{'id':'pancreas','name':'pancreas'}]}


def test_case_correction_is_a_question_not_silent_authorization():
    q='How many T1D stage 1 donors available in hPAP?'
    r=recovery(q,V,'release')
    assert r['message'].startswith('Do you mean HPAP?')
    assert r['suggestions'][0]['recommended_question']==q.replace('hPAP','HPAP')
    assert r['original_question']==q
    assert not empty_failure({'steps':[],'clarification':r['message'],'recovery':r})


def test_pkn_is_a_suggestion_with_recorded_proxy_and_retains_filters():
    q='Show samples in PKN from HPAP, excluding stage 3 donors.'
    r=recovery(q,V,'release')
    assert r['suggestions'][0]['recommended_question']==q.replace('PKN','pancreatic lymph node')
    assert 'proxy' in r['message']
    assert r['suggestions'][0]['matches'][0]['match_basis']=='spelling_candidate'
    assert recovery(q.replace('PKN','pancreatic lymph node'),V,'release') is None


def test_registry_presence_and_protected_identifiers():
    assert recovery('Show samples in PKN',{'inventory_complete':False},'r') is None
    for q in ['Show gene PKN', 'Find variant rs123', 'Show stage 2 donors in HPAP',
              'Show samples in PLN','Show donors from HPAP']:
        assert recovery(q,V,'r') is None
    r=recovery('Show samples in XYZ',V,'r')
    assert r and not r['suggestions'] and 'XYZ' in r['message']


def test_multiple_terms_produce_one_complete_question():
    q='Show samples in PKN from hPAP excluding stage 1.'
    r=recovery(q,V,'r')
    assert len(r['issues'])==2 and len(r['suggestions'])==1
    assert r['suggestions'][0]['recommended_question']=='Show samples in pancreatic lymph node from HPAP excluding stage 1.'


def test_source_expansion_is_not_tissue_but_explicit_tissue_is_preserved():
    q='Count donors in HPAP (Human Pancreas Analysis Program), with pancreas samples.'
    assert source_role_text(q,V)=='Count donors in HPAP, with pancreas samples.'
    assert source_role_text('HPAP (pancreas samples only)',V)=='HPAP (pancreas samples only)'
    assert source_role_text(q,{})==q


def test_repair_only_the_narrow_invented_constraint_case():
    source={'question':'Count pancreas donors', 'relation_types':['HAS_DONOR'],
        'constraints':[{'entity_type':'anatomical_structure','value':'pancreas'},
                       {'entity_type':'donor','property':'t1d_stage','value':'stage 1'}],
        'semantic_request':{'source':'user_request','question':'Count stage 1 donors in HPAP'}}
    resolved={'semantic_issues':['A generated sample-tissue filter was not authorized by the user request and was removed.']}
    original=deepcopy(source);fixed=repair_generated_scope(source,resolved)
    assert fixed['question']==source['semantic_request']['question']
    assert fixed['constraints']==source['constraints'][1:] and source==original
    assert repair_generated_scope({**source,'relation_types':['HAS_SAMPLE']},resolved) is None
    assert repair_generated_scope(source,{'semantic_issues':['unknown requested filter']}) is None


def test_ambiguous_source_offers_choices_without_applying_one():
    v={**V,'donor_sources':['HPAP','HPAT']}
    r=recovery('Count donors from HPAX excluding stage 3',v,'r')
    assert len(r['suggestions'])==2
    assert 'Which meaning' in r['message']
    assert all('excluding stage 3' in x['recommended_question'] for x in r['suggestions'])


def test_runtime_clarification_bypasses_planner_and_queries(tmp_path):
    import asyncio
    from tests_vnext.test_runtime import service, Graph, wait_state
    class GroundedGraph(Graph):
        async def ground_question(self, question):
            return {'state':'ready','sample_terminology':deepcopy(V)}
        async def execute(self,*args):
            raise AssertionError('Clarification must not execute a query')
    async def scenario():
        async with service(tmp_path,graph=GroundedGraph()) as (client,runtime,gateway,*_):
            response=await client.post('/v2/plans',json={'question':'How many stage 1 donors in hPAP?'})
            r=await wait_state(client,response.json()['run_id'],{'awaiting_confirmation','failed'})
            assert r['plan']['recovery']['category']=='term_clarification'
            assert r['plan']['recovery']['suggestions'][0]['recommended_question']=='How many stage 1 donors in HPAP?'
            assert gateway.plans==gateway.syntheses==0
    asyncio.run(scenario())


def test_output_and_viewer_modules_remain_unchanged():
    from pathlib import Path
    import subprocess
    for path in ['pankagent_vnext/llm.py','pankagent_vnext/output_scope.py',
                 'pankagent_vnext/evidence_context.py','pankagent_vnext/format_input_modes.py',
                 'pankagent_vnext/viewer_evidence.py','pankgraph_results/app.py']:
        assert Path(path).read_bytes()==subprocess.check_output(['git','show','b34f1f8:'+path])


def test_full_cohort_name_does_not_create_pancreas_filter():
    from pankagent_vnext.semantic_registry import resolve, STAGES
    q='Count stage 1 donors in HPAP (Human Pancreas Analysis Program).'
    step={'id':'s1','question':q,'constraints':[],'relation_types':[], 'complete':True,
          'semantic_request':{'source':'user_request','question':q}}
    v={**V,'stages':list(STAGES.values()),'modalities':[]}
    resolved=resolve(step,v,'PanKgraph_08_04')
    assert not any(c.get('entity_type')=='anatomical_structure' for c in resolved['constraints'])
    assert not any('sample-tissue' in issue for issue in resolved['semantic_issues'])


def test_inventory_fallback_when_large_index_unavailable(monkeypatch):
    import asyncio
    from pankagent_vnext.graph import GraphAdapter
    import pankagent_vnext.preplanning_grounding as module
    async def unavailable(*args,**kwargs):return {'state':'unavailable'}
    monkeypatch.setattr(module,'ground_question',unavailable)
    graph=object.__new__(GraphAdapter)
    async def verified():return None
    async def vocabulary():return deepcopy(V)
    graph._ensure_identity=verified;graph.semantic_vocabulary=vocabulary
    grounded=asyncio.run(graph.ground_question('Count stage 1 donors in hPAP'))
    assert grounded['term_inventory_status']=='verified_fallback'
    assert recovery('Count stage 1 donors in hPAP',grounded['term_vocabulary'],'r')['suggestions']
