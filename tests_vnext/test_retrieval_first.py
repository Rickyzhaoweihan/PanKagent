from copy import deepcopy
import json
from pathlib import Path
import pytest
from pankagent_vnext.clinical_retrieval import draft
from pankagent_vnext.semantic_registry import resolve, STAGES
from pankagent_vnext.schema_tools import question_guidance
from pankagent_vnext.ranking_contract import attach_to_plan, validation_errors, guidance
from pankagent_vnext.graph import tokenize
from tests_vnext.test_preplanning_grounding import make_index, contextual_graph, candidate_ids

V={'inventory_sha256':'fixture-inventory','inventory_complete':True,'sources':['StudyA'],'donor_sources':['StudyA'],
   'sample_sources':[], 'modalities':['scRNA-seq','snMultiomics'],
   'stages':list(STAGES.values()),'modality_links_verified':True,
   'tissues':[{'id':'tissue1','name':'spleen'}],
   'donor_categories_complete':True,'donor_categorical_values':{'diabetes_type':['Control Without Diabetes','Type 1 Diabetes']},
   'donor_diseases':[{'id':'d1','name':'type 1 diabetes'}]}
G={'status':'ready','identity':{'graph_release':'PanKgraph_08_04'},'sample_terminology':V}

@pytest.mark.parametrize('q,expected',[
 ('In StudyA, how many donors have a recorded T1D stage of 3?', {'t1d_stage':STAGES['3'],'data_source':'StudyA'}),
 ('Count StudyA non-diabetic control donors who are recorded as T1D stage 1.',{'t1d_stage':STAGES['1'],'diabetes_type':'Control Without Diabetes'}),
 ('How many scRNA-seq samples, excluding multiome assays, come from spleen in StudyA non-diabetic controls?',{'diabetes_type':'Control Without Diabetes','id':'tissue1'}),
 ('How many StudyA stage 3 donors do NOT have a recorded diabetes type of T1D?',{'diabetes_type':'Type 1 Diabetes'}),
])
def test_closed_population_keeps_predicates_and_does_not_add_diagnosis(q,expected):
    p=draft(q,G);assert p
    s=resolve(p['steps'][0],V,'PanKgraph_08_04');assert not s['semantic_issues']
    assert not any(c.get('entity_type')=='disease' for c in s['constraints'])
    fields={c['property']:c['value'] for c in s['constraints']};assert all(fields[k]==v for k,v in expected.items())
    if 'NOT' in q: assert next(c for c in s['constraints'] if c['property']=='diabetes_type')['operator']=='!='
    if 'samples' not in q:assert not p['steps'][0]['relation_types']
    assert all(x['complete'] for x in p['steps'])


def test_comparison_has_separate_populations_without_changing_source_or_assay():
    q='Compare exact scRNA-seq sample availability between StudyA ND and T1D donors. Count distinct samples separately for each cohort.'
    p=draft(q,G);assert len(p['steps'])==2
    for step in p['steps']:
        s=resolve(step,V,'PanKgraph_08_04');assert not s['semantic_issues']
        assert {'property':'data_source','entity_type':'donor','operator':'=','value':'StudyA'} in s['constraints']
        assert s['sample_requirements']['modality_groups']==[['scRNA-seq']]
    a,b=[resolve(s,V,'PanKgraph_08_04') for s in p['steps']]
    assert any(c['property']=='diabetes_type' for c in a['constraints'])
    assert any(c['entity_type']=='disease' and c['value']=='d1' for c in b['constraints'])

@pytest.mark.parametrize('q',['Count unknownsource stage 1 donors','Count StudyA donors with unknowntrait','Count StudyA donors older than 50','Compare unknown assay samples between StudyA ND and T1D donors'])
def test_unrecognized_modifiers_are_not_silently_dropped(q):
    assert draft(q,G) is None


def test_domains_and_negation_are_not_node_requests():
    graph=contextual_graph();graph.rows['Gene'].append({'id':'gene-not','name':'NOT','labels':['Gene']})
    index=make_index(graph)
    assert 'gene-not' not in candidate_ids(index.match('How many donors do NOT have a recorded diabetes type of T1D?'))
    assert 'gene-not' in candidate_ids(index.match('Find gene NOT'))
    mentions=index.match('What GO biological-process, molecular-function and cellular-component terms annotate CFTR?')
    assert 'GO:0008150' not in candidate_ids(mentions)


def test_property_guidance_does_not_authorize_filter():
    g=question_guidance('Return GWAS P values and PIP')
    assert g['requested_property_owners']
    assert all(not x['filter_authorized'] for x in g['requested_property_owners'])


def test_ranking_is_formatting_work_and_cypher_keeps_complete_population():
    p=attach_to_plan({'original_question':'Show top 5 genes by highest mean CPM','steps':[{'id':'s','question':'Show top 5 genes by highest mean CPM','relation_types':['GENE_DETECTED_IN'],'constraints':[]}]})
    s=p['steps'][0];s['graph_version']='PanKgraph_08_04';assert s['complete'] and s['ranking_contract']['execution_stage']=='formatting'
    q='MATCH (g:Gene)-[r:GENE_DETECTED_IN]->(c:anatomical_structure) RETURN g,r,c'
    assert validation_errors(tokenize(q),s,{})==[]
    assert validation_errors(tokenize(q+' ORDER BY r.mean_donor_cpm DESC LIMIT 5'),s,{})
    assert 'No ORDER BY or LIMIT' in guidance(s)


@pytest.mark.parametrize('question',[
    'In StudyA, how many donors have a recorded T1D stage of 3?',
    'Count StudyA non-diabetic control donors who are recorded as T1D stage 1.',
    'How many scRNA-seq samples, excluding multiome assays, come from spleen in StudyA non-diabetic controls?',
    'Compare exact scRNA-seq sample availability between StudyA ND and T1D donors. Count distinct samples separately for each cohort.',
])
def test_cohort_plan_preparation_and_templates_without_paid_calls(question):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from test_planning_compiler_gateway import gateway_for
    from pankagent_vnext.graph import GraphAdapter, validate_cypher
    from pankagent_vnext.query_templates import compile_query
    async def check():
        gateway, calls = gateway_for(lambda _: deepcopy(draft(question,G)))
        plan = await gateway.plan(question, [], grounding=G)
        assert not plan.get('proposal_issue') and len(calls) == 1
        graph = object.__new__(GraphAdapter)
        graph.settings = SimpleNamespace(graph_version='PanKgraph_08_04')
        graph._resolution_secret = b'fixture'
        graph.identity_verified = True
        graph._ensure_identity = AsyncMock()
        graph.semantic_vocabulary = AsyncMock(return_value=deepcopy(V))
        async def resolve_identity(constraint,index,step):
            return {'constraint_index':index,'requested':deepcopy(constraint),'state':'resolved',
                    'graph_version':'PanKgraph_08_04','entity_type':constraint['entity_type'],
                    'labels':[constraint['entity_type']], 'id':constraint['value'], 'name':constraint['value']}
        graph._resolve_constraint = resolve_identity
        prepared = await graph.prepare_plan(plan, AsyncMock())
        for step in prepared['steps']:
            assert not step.get('semantic_issues'), (step.get('semantic_issues'), step.get('runtime_binding_issues'))
            query = compile_query(step)
            assert query, step
            assert not validate_cypher(query['cypher'],step,query['parameters'])
            assert all(word not in query['cypher'].upper() for word in ('ORDER BY','LIMIT','COUNT('))
    asyncio.run(check())


def test_capability_request_preserves_assay_labels():
    from test_runtime_clinical_resolution import VOCAB
    v = dict(deepcopy(VOCAB), inventory_complete=True)
    g = dict(G, sample_terminology=v)
    p=draft('Count RNA-capable samples from HPAP stage 1 donors, including scRNA-seq and snMultiomics. Keep the assay labels separate.',g)
    assert p
    step=p['steps'][0]
    assert step['sample_requirements']['modality_groups']==[['scRNA-seq','snMultiomics']]
    assert not any(c['entity_type']=='disease' for c in step['constraints'])


def test_plain_identity_draft_does_not_require_an_unrequested_relationship():
    from pankagent_vnext.schema_drafting import compile_identity_draft
    g=dict(G,mentions=[{'requested':'ExampleGene','state':'resolved','candidates':[
        {'id':'g123','name':'ExampleGene','entity_type':'Gene'}]}])
    p=compile_identity_draft('Tell me about gene ExampleGene',g)
    assert p['steps'][0]['relation_types']==[]
    assert p['steps'][0]['constraints'][0]['value']=='g123'
    assert compile_identity_draft('Tell me about gene ExampleGene and its partners',g) is None


def test_formatter_ranking_still_rejects_invented_statistical_cutoffs():
    s=attach_to_plan({'steps':[{'id':'s','question':'Show top 5 genes by highest mean CPM',
        'relation_types':['GENE_DETECTED_IN'],'constraints':[]}]})['steps'][0]
    s['graph_version']='PanKgraph_08_04'
    query='MATCH (g:Gene)-[r:GENE_DETECTED_IN]->(c:anatomical_structure) WHERE r.mean_donor_cpm > 100 RETURN g,r,c'
    assert 'unrequested_ranking_cutoff:mean_donor_cpm' in validation_errors(tokenize(query),s,{})


@pytest.mark.parametrize('q',['Count StudyA donors without samples',
    'Find StudyA donors who do not have samples', 'Count StudyA donors with unknown diabetes status'])
def test_closed_draft_does_not_convert_exclusions_or_unknown_conditions_to_positive_joins(q):
    assert draft(q,G) is None


@pytest.mark.parametrize('question',['Tell me about gene ExampleGene','What is ExampleGene?'])
def test_identity_only_plan_passes_scope_guards(question):
    import asyncio
    from test_planning_compiler_gateway import gateway_for
    from pankagent_vnext.schema_drafting import compile_identity_draft
    g=dict(G,mentions=[{'requested':'ExampleGene','state':'resolved','candidates':[
        {'id':'g123','name':'ExampleGene','entity_type':'Gene','labels':['Gene']}]}])
    async def check():
        gateway,_=gateway_for(lambda _:compile_identity_draft(question,g))
        plan=await gateway.plan(question,[],grounding=g)
        assert not plan.get('proposal_issue') and not plan.get('clarification')
        assert plan['steps'][0]['constraints'][0]['value']=='g123'
        assert plan['steps'][0]['relation_types']==[]
    asyncio.run(check())
