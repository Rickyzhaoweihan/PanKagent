import asyncio
from copy import deepcopy
from pankagent_vnext.verified_facts import build, number
from pankagent_vnext.semantic_registry import _unresolved_tissue_role, _negated_at
from pankagent_vnext.agent_schemas import module


def evidence(edges, **kw):
    return {'s1': {'evidence_id':'G1','status':'complete','truncated':False,
        'retrieval_execution':{'completed':True,'cursor_exhausted':True},'edges':edges,**kw}}


def edge(kind, identifier, **props):
    return {'type':kind,'start_id':identifier,'end_id':'population','properties':props}


def test_ranking_uses_full_records_decimal_zeros_and_identity_ties():
    rows=[edge('PART_OF_GWAS_SIGNAL','z',p_value='1e-400'),edge('PART_OF_GWAS_SIGNAL','b',p_value=0),
          edge('PART_OF_GWAS_SIGNAL','a',p_value=0),edge('PART_OF_GWAS_SIGNAL','z',p_value='0.2')]
    result=build('Show the 3 distinct variants with the smallest recorded P values',evidence(rows))[0]
    assert [r['id'] for r in result['rankings'][0]['rows']]==['a','b','z']
    assert result['rankings'][0]['rows'][2]['value']=='1E-400'
    assert not build('Show the 3 variants with smallest P values',evidence(rows,status='partial'))[0]['rankings']


def test_facts_do_not_mutate_records_and_keep_mixed_directions():
    data=evidence([edge('T1D_DEG_IN','a',log2_fold_change=2,p_value=0),edge('T1D_DEG_IN','b',log2_fold_change=-1,p_value='.01')])
    before=deepcopy(data);facts=build('Compare measurements',data)[0]
    signed=next(x for x in facts['numeric'] if x['field']=='log2_fold_change')
    assert (signed['positive'],signed['negative'],signed['zero'])==(1,1,0)
    assert data==before
    assert number('NaN') is None and number(True) is None


def test_grammatical_sample_modifier_does_not_invent_tissue():
    for text in ['donors whether or not they have samples','Count RNA-capable samples from StudyA donors', 'Count exact scRNA-seq samples']:
        assert not _unresolved_tissue_role(text,{'sources':['StudyA'],'modalities':['scRNA-seq']},[])
    assert _unresolved_tissue_role('Count unknownsite samples',{},[])
    assert _unresolved_tissue_role('samples from unknownsite',{},[])


def test_negative_adjective_does_not_negate_later_source():
    q='non-diabetic control donors in StudyA'
    assert not _negated_at(q,q.index('StudyA'),len(q))
    q='donors not from StudyA'
    assert _negated_at(q,q.index('StudyA'),len(q))


def test_routing_is_schema_owned():
    assert module('query_patterns')['gpu_participation_required'] is False


def test_cancelled_grounding_waiter_does_not_cancel_shared_refresh():
    from pankagent_vnext.preplanning_grounding import Grounder
    async def scenario():
        instance=object.__new__(Grounder);instance.refresh_task=None
        calls=[]
        async def warm(force=False):
            calls.append(1);await asyncio.sleep(.03);return 'new verified index'
        instance._warm=warm
        try:await asyncio.wait_for(instance.warm(),.001)
        except asyncio.TimeoutError:pass
        assert await instance.warm()=='new verified index'
        assert calls==[1]
        await instance.close()
    asyncio.run(scenario())


def test_result_acceptance_keeps_empty_and_optional_annotation():
    from pankagent_vnext.result_assessment import assess
    empty=evidence([],status='empty')['s1'];empty['nodes']=[]
    assert assess({},empty)['empty']
    complete=evidence([edge('annotation','a')])['s1'];complete['nodes']=[{'id':'a'},{'id':'population'},{'id':'extra'}]
    assert assess({},complete)['complete']
    complete['nodes']=[{'id':'a'}]
    assert assess({},complete)['reasons']==['result_missing_edge_endpoint']


def test_property_tool_facts_keep_provenance_without_claiming_exhaustiveness():
    from pankagent_vnext.schema_tools import merge_property_facts
    from pankagent_vnext.agent_schemas import active_pack
    pack=active_pack();original={'status':'ready','schema':{'categories':{}}}
    observation={'status':'complete','reference':'relationships.FUNCTION_ANNOTATION.properties.data_source',
        'schema_sha256':pack.digest,'graph_release':pack.identity()['graph_release'],
        'search_text':'KEGG','values_complete':True,'values':[{'value':'KEGG','frequency':20}]}
    merged=merge_property_facts(original,observation)
    assert merged['schema']['categories']['FUNCTION_ANNOTATION.data_source']==['KEGG']
    assert not merged['property_observations']['FUNCTION_ANNOTATION.data_source']['complete_inventory']
    assert original=={'status':'ready','schema':{'categories':{}}}
    assert merge_property_facts(original,{**observation,'graph_release':'other'})==original


def test_focus_identity_not_copied_to_independent_partner_annotation():
    from pankagent_vnext.task_preparation import bind_unique_requested_identities
    grounding={'status':'ready','mentions':[{'requested':'FocusGene','state':'resolved','candidates':[
        {'id':'g1','name':'FocusGene','entity_type':'Gene','labels':['Gene']}]}]}
    plan={'steps':[{'id':'x','question':'Retrieve annotations of the partners',
        'relation_types':['FUNCTION_ANNOTATION'],'constraints':[]}]}
    assert bind_unique_requested_identities('Find FocusGene partners and their annotations',grounding,plan)==plan


def test_result_execution_must_be_verified_even_when_empty():
    from pankagent_vnext.result_assessment import assess
    assert not assess({}, {'status':'empty','nodes':[],'edges':[]})['valid']


def test_dependency_signal_roles_never_share_leads_implicitly():
    from pankagent_vnext.referenced_dependencies import recipe_for, referenced_ids
    parent={'edges':[{'type':'SIGNAL_COLOC_WITH','properties':{'gwas_lead_vars':'rs11','qtl_lead_vars':['rs12','rs13']}}]}
    assert referenced_ids(recipe_for({'relation_types':['PART_OF_GWAS_SIGNAL']}),parent)==(['rs11'],None)
    assert referenced_ids(recipe_for({'relation_types':['PART_OF_QTL_SIGNAL']}),parent)==(['rs12','rs13'],None)
    parent['edges'][0]['properties']['qtl_lead_vars']='unknown signal prefix'
    assert referenced_ids(recipe_for({'relation_types':['PART_OF_QTL_SIGNAL']}),parent)[1]=='dependency_reference_format_unverified'


def overlap_step():
    from test_query_templates import step, _authorized
    from pankagent_vnext.release_schema import REGISTRY
    constraints=[{'entity_type':owner,'property':'id','operator':'=','value':identifier}
                 for owner,identifier in [('Gene','gene_a'),('anatomical_structure','cell_a')]]
    value=_authorized(step('OCR_PEAK_IN',constraints,resolved_entities=[{'constraint_index':i,
        'requested':deepcopy(c),'state':'resolved','graph_version':REGISTRY['release'],
        'entity_type':c['entity_type'],'labels':[c['entity_type']],'id':c['value'],'name':c['value']}
        for i,c in enumerate(constraints)]))
    # Re-sign the request proof after adding the explicit structural request.
    import hashlib
    value['question']='Return OCR peaks that overlap the gene body in the same assembly'
    value['semantic_request']['question']=value['question']
    for binding in value['request_filter_bindings']:
        binding['request_sha256']=hashlib.sha256(value['question'].encode()).hexdigest()
    return value


def test_gene_body_overlap_is_typed_bounded_and_not_regulation():
    from pankagent_vnext.query_templates import compile_query
    from pankagent_vnext.graph import validate_cypher
    s=overlap_step();result=compile_query(s)
    assert result['template_id']=='interval_overlap_records'
    assert 'p.`genome_assembly` = g.`genome_assembly`' in result['cypher']
    assert result['parameters']=={'template_0':'gene_a','template_1':'cell_a'}
    assert validate_cypher(result['cypher'],s,result['parameters'])==[]
    assert validate_cypher(result['cypher'].replace(' <= ',' >= '),s,result['parameters'])==['interval_overlap_requires_verified_template']
    assert compile_query({**s,'constraints':s['constraints'][:1]}) is None


def test_duplicate_admissible_candidates_never_execute_twice():
    from test_graph import FakeAdapter, VALID, step
    async def scenario():
        adapter=FakeAdapter([[VALID, VALID], [VALID]])
        # A result-side failure permits bounded alternatives, but identical reads
        # cannot repair missing execution evidence and must not be repeated.
        adapter.answer['retrieval_execution']={'completed':False,'cursor_exhausted':False}
        result=await adapter.execute(step(),{},lambda *args: asyncio.sleep(0))
        assert result['status']=='failed'
        assert len(adapter.retrieved)==1
        assert any('duplicate_query_parameters' in v['reasons'] for v in result['validation'])
    asyncio.run(scenario())


def test_forced_grounding_refresh_cannot_be_satisfied_by_cached_inflight_read():
    from pankagent_vnext.preplanning_grounding import Grounder
    async def scenario():
        instance=object.__new__(Grounder);instance.refresh_task=None;started=asyncio.Event();release=asyncio.Event();calls=[]
        async def warm(force=False):
            calls.append(force);started.set()
            if not force:await release.wait()
            return 'fresh' if force else 'cached'
        instance._warm=warm
        ordinary=asyncio.create_task(instance.warm());await started.wait()
        forced=asyncio.create_task(instance.warm(force=True));await asyncio.sleep(0);release.set()
        assert await ordinary=='cached'
        assert await forced=='fresh' and calls==[False,True]
        await instance.close()
    asyncio.run(scenario())
