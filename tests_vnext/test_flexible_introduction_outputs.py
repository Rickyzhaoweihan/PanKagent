from copy import deepcopy
import asyncio
import pytest
from pankagent_vnext.graph_contract import generation_request
from pankagent_vnext.schema_drafting import compile_identity_draft
from pankagent_vnext.verified_facts import build
from pankagent_vnext.release_schema import REGISTRY


def test_sample_only_generation_does_not_require_donors_or_tissues():
    step={'question':'Which sample types are recorded?', 'relation_types':[], 'constraints':[],
          'semantic_registry':{'donor_required':False}, 'sample_requirements':{}, 'complete':True}
    text=generation_request(step,step['question'])
    assert 'donor -HAS_SAMPLE-> Sample_node' not in text
    assert 'Required connected schema paths: Sample_node;' in text
    assert 'distinct recorded values' in text


def test_cohort_generation_keeps_required_donor_and_tissue_paths():
    step={'question':'Find samples from StudyA donors from tissue X', 'relation_types':['HAS_SAMPLE'],
          'constraints':[{'entity_type':'donor','property':'data_source','operator':'=','value':'StudyA'},
                         {'entity_type':'anatomical_structure','property':'id','operator':'=','value':'X'}],
          'semantic_registry':{'donor_required':True},'sample_requirements':{},'complete':True}
    text=generation_request(step,step['question'])
    assert 'donor -HAS_SAMPLE-> Sample_node' in text
    assert 'anatomical_structure -HAS_SAMPLE-> the SAME Sample_node' in text
    assert 'StudyA' in text and 'X' in text


def grounding():
    return {'status':'ready','identity':{'graph_release':REGISTRY['release']},'mentions':[
        {'requested':'ExampleGene','state':'resolved','candidates':[{'id':'g123','name':'ExampleGene','entity_type':'Gene'}]}]}


@pytest.mark.parametrize('question', ['Tell me about gene ExampleGene',
    "Give me a short introduction to ExampleGene using PanKgraph's data.",
    'Give me an overview of ExampleGene and its relevant evidence in PanKgraph.'])
def test_plain_intro_identity_draft_retains_focal_entity(question):
    plan=compile_identity_draft(question,grounding())
    assert plan and len(plan['steps']) == 1
    assert plan['steps'][0]['constraints'][0]['value']=='g123'


@pytest.mark.parametrize('question', ['Tell me about ExampleGene in stage 3 donors',
    'Tell me about ExampleGene excluding immune cells', 'Tell me about ExampleGene and OtherGene',
    'Tell me about ExampleGene and its QTL evidence'])
def test_identity_recovery_cannot_drop_requested_conditions(question):
    assert compile_identity_draft(question,grounding()) is None


def test_empty_intro_proposal_recovers_through_normal_preparation():
    from test_planning_compiler_gateway import gateway_for
    async def check():
        q='Tell me about gene ExampleGene'
        gateway,_=gateway_for(lambda _: {'interpreted_question':q,'steps':[],'clarification':None})
        p=await gateway.plan(q,[],grounding=grounding())
        assert p['steps'] and not p.get('proposal_issue')
        assert p['introduction_recovery']['kind']=='verified_identity_draft'
        assert p['steps'][0]['constraints'][0]['value']=='g123'
    asyncio.run(check())


def test_scalar_facts_deduplicate_without_inventing_completeness():
    result={'status':'complete','nodes':[],'edges':[],
            'rows':[{'modality':'A'},{'modality':'A'},{'modality':'B'}, {'modality':None}, {'count':24}],
            'retrieval_execution':{'completed':True,'cursor_exhausted':True},'truncated':False}
    f=build('What types?',{'s':result})[0]['scalar_values']
    assert f==[{'column':'modality','values':['A','B'],'distinct_count':2,'missing_values':1,
                'complete':True,'count_unit':'distinct recorded scalar values'}]
    result['truncated']=True
    assert build('What types?',{'s':result})[0]['scalar_values'][0]['complete'] is False
    result['status']='failed'
    assert build('What types?',{'s':result})==[]


def test_mixed_scalar_column_cannot_claim_complete_string_membership():
    from pankagent_vnext.verified_facts import scalar_values
    assert scalar_values([{'category':'A'},{'category':3}],True)==[]


@pytest.mark.parametrize('question', [
    'Which studies and data sources are represented in PanKgraph?',
    'List recorded dataset sources.', 'Which donor source is recorded?',
    'Which studies are represented?'])
def test_source_category_projection_follows_schema_request_vocabulary(question):
    from pankagent_vnext.answer_facts import requested_classification_fields
    from pankagent_vnext.graph import validate_cypher
    step={'question':question, 'relation_types':[], 'constraints':[]}
    assert requested_classification_fields(step)=={'data_source'}
    for label in ['donor', 'Sample_node']:
        issues=validate_cypher(f'MATCH (n:{label}) RETURN DISTINCT n.data_source AS source',step)
        assert not any('classification_projection' in issue for issue in issues), issues


@pytest.mark.parametrize('question', ['Which samples are available?',
    'What is the donor data source URL?', 'Show the distribution of donors by stage.'])
def test_unrequested_source_projection_is_not_authorized_by_generic_output(question):
    from pankagent_vnext.answer_facts import requested_classification_fields
    from pankagent_vnext.graph import validate_cypher
    step={'question':question, 'relation_types':[], 'constraints':[]}
    assert 'data_source' not in requested_classification_fields(step)
    assert 'unrequested_donor_classification_projection:data_source' in validate_cypher(
        'MATCH (d:donor) RETURN DISTINCT d.data_source AS source',step)


def test_source_request_does_not_authorize_unrequested_clinical_projection():
    from pankagent_vnext.graph import validate_cypher
    step={'question':'Which studies and data sources are represented?',
          'relation_types':[], 'constraints':[]}
    issues=validate_cypher('MATCH (d:donor) RETURN DISTINCT d.diabetes_type AS category',step)
    assert 'unrequested_donor_classification_projection:diabetes_type' in issues
