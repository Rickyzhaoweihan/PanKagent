from copy import deepcopy
import pytest
from pankagent_vnext.semantic_decision import record, phrase_roles
from pankagent_vnext.semantic_registry import resolve, attach_request_authorizations, RELEASE, STAGES
from pankagent_vnext.query_templates import runtime_binding_errors
from test_cohort_scope_planning_lifecycle import VOCABULARY

@pytest.mark.parametrize('question', [
 'How many spleen scmultioimcs sample avalible from HPAP T1D stage 3 samples?',
 'Please count the splenic multiome specimens from that HPAP stage-three cohort.',
 'Look up samples from the tissue and assay I mean.',
])
def test_planner_choice_is_not_reparsed_from_wording(question):
    vocab=deepcopy(VOCABULARY);vocab['modalities']=['snMultiomics']
    step={'id':'s1','question':question,'semantic_request':{'source':'user_request','question':question},
          'constraints':[{'entity_type':'donor','property':'t1d_stage','operator':'=','value':STAGES['3']},
                         {'entity_type':'donor','property':'data_source','operator':'=','value':'HPAP'},
                         {'entity_type':'Sample_node','property':'data_modality','operator':'=','value':'snMultiomics'}],
          'relation_types':['HAS_SAMPLE']}
    step['model_scope_decision']=record(step,question)
    result=resolve(step,vocab,RELEASE);result['graph_version']=RELEASE
    result=attach_request_authorizations(result)
    assert result['constraints']==step['constraints']
    assert not result['semantic_issues']
    assert not runtime_binding_errors(result)
    assert result['sample_requirements']['modality_groups']==[['snMultiomics']]
    assert result['semantic_registry']['scope_authority']=='planner'


def test_model_phrase_roles_do_not_require_python_example_keywords():
    q='Samples: scRNA-seq is just an illustration.'
    assert phrase_roles(q,[{'text':'scRNA-seq','role':'example'}])[0]['role']=='example'


def test_unknown_recorded_value_still_requires_resolution():
    q='Find samples';step={'id':'s','question':q,'relation_types':['HAS_SAMPLE'],
        'constraints':[{'entity_type':'Sample_node','property':'data_modality','operator':'=','value':'not-recorded'}],
        'semantic_request':{'source':'user_request','question':q}}
    step['model_scope_decision']=record(step,q)
    result=resolve(step,deepcopy(VOCABULARY),RELEASE)
    assert result['constraints']==step['constraints']
    assert 'recorded category inventory' in result['semantic_issues'][0]

@pytest.mark.parametrize('operator', ['=', '!=', 'IN', 'NOT IN'])
def test_planner_owns_source_owner_and_operator(operator):
    q='Look up specimens from this study, with the restriction we discussed.'
    value='["Study A"]' if operator in {'IN','NOT IN'} else 'Study A'
    predicate={'entity_type':'Sample_node','property':'data_source','operator':operator,'value':value}
    step={'id':'s','question':q,'relation_types':['HAS_SAMPLE'],'constraints':[predicate],
          'semantic_request':{'source':'user_request','question':q}}
    step['model_scope_decision']=record(step,q)
    vocab={**deepcopy(VOCABULARY),'sample_sources':['Study A'],'donor_sources':['HPAP']}
    result=resolve(step,vocab,RELEASE);result['graph_version']=RELEASE
    result=attach_request_authorizations(result)
    assert result['constraints']==[predicate]
    assert not result['semantic_registry']['donor_required']
    assert not runtime_binding_errors(result)


def test_binding_compiler_uses_schema_owners_not_pankgraph_names(monkeypatch):
    from pankagent_vnext import planner_bindings
    config={'fields':[{'owner':'Specimen','property':'method','vocabulary_path':['methods']}],
            'cohort_owner':'Participant','assay_field':['Specimen','method']}
    monkeypatch.setattr(planner_bindings,'schema_module',lambda _: {'planner_bindings':config})
    predicate={'entity_type':'Specimen','property':'method','operator':'=','value':'ATAC'}
    result=planner_bindings.prepare({'constraints':[predicate]}, {'methods':['ATAC'],'inventory_sha256':'other-kg'}, 'other-release')
    assert not result['semantic_issues']
    assert result['constraints']==[predicate]
    assert result['sample_requirements']['modality_groups']==[['ATAC']]
