import pytest
import unittest
from pankagent_vnext.semantic_registry import resolve, RELEASE
from pankagent_vnext.categorical_bindings import bind_verified_categories
from pankagent_vnext.graph import tokenize, validate_cypher
from pankagent_vnext.tissue_aliases import matched_tissues
from pankagent_vnext.query_recovery import stage_recovery, plan_recovery

STAGE = 'Stage 2: two or more autoantibodies, dysglycemia (e.g., HbA1c ‚â• 5.7%)'
ISLET = {'id':'UBERON_0000006','name':'pancreatic islet (islet of Langerhans)'}
VOCAB = {'stages':[STAGE],'sources':['HPAP'],'modalities':['scRNA-seq','snMultiomics'],
         'tissues':[ISLET], 'assay_donor_sources':{'snMultiomics':['HPAP']}, 'inventory_complete':True}

def step(q='Find T1D stage 2 donors scRNAseq islet sample'):
 return resolve({'id':'s1','question':q,'relation_types':['HAS_SAMPLE'],'constraints':[
 {'entity_type':'disease','property':'id','operator':'=','value':'MONDO_0005147'}]},VOCAB,RELEASE)

@pytest.mark.parametrize('term',['islet','pancreatic islet','islet of Langerhans'])
def test_verified_tissue_alias_survives_omitted_planner_filter(term):
 s=step('Find T1D stage 2 donors scRNAseq '+term+' samples')
 assert any(c.get('entity_type')=='anatomical_structure' and c['value']==ISLET['id'] for c in s['constraints'])
 assert not any(c.get('entity_type')=='disease' for c in s['constraints'])

def test_explicit_diagnosed_filter_remains_requested():
 s=step('Find donors diagnosed with T1D with stage 2 metadata and islet scRNAseq samples')
 assert any(c.get('entity_type')=='disease' for c in s['constraints'])

def test_successful_re_resolution_does_not_retain_stale_recovery():
 s=step()
 s['recovery']={'category':'stage_inventory_unavailable','message':'old failed inventory'}
 resolved=resolve(s,VOCAB,RELEASE)
 assert not resolved.get('semantic_issues') and not resolved.get('recovery')
 assert s['recovery']['message']=='old failed inventory'

def test_sample_only_question_does_not_acquire_a_donor_requirement():
 from pankagent_vnext.semantic_registry import donor_intent
 s={'question':'List spleen samples','constraints':[]}
 assert not donor_intent(s)
 result=resolve(s,VOCAB,RELEASE)
 assert result['semantic_registry']['donor_required'] is False
 assert not any(c.get('entity_type')=='donor' for c in result['constraints'])

def test_molecular_cohort_description_does_not_force_donor_metadata_paths():
 from pankagent_vnext.semantic_registry import donor_intent
 s={'question':'Is ZC3H11A differentially expressed in beta cells between non-diabetic and type 1 diabetes donors?',
    'relation_types':['T1D_DEG_IN'],'constraints':[{'entity_type':'Gene','property':'name','value':'ZC3H11A'}]}
 assert not donor_intent(s)
 assert resolve(s,VOCAB,RELEASE)==s
 s['constraints'].append({'entity_type':'donor','property':'t1d_stage','value':'Stage 2'})
 assert donor_intent(s)

def test_registry_mismatch_is_an_operator_issue_not_biological_clarification():
 s=resolve({'question':'Find stage 2 donors','constraints':[]},VOCAB,'other-release')
 assert s['recovery']['category']=='graph_release_mismatch'
 assert not s['recovery']['retryable'] and not s['recovery']['suggestions']

def test_missing_stage_number_is_not_relabelled_as_invented_stage_zero():
 s=resolve({'question':'Count islet samples from HPAP donors with type 1 diabetes stage',
    'constraints':[{'entity_type':'donor','property':'t1d_stage','operator':'=','value':'Stage 0: unspecified'}]},VOCAB,RELEASE)
 assert s['semantic_issues']
 assert 'without specifying which stage' in s['recovery']['message']
 assert 'stage 0' not in s['recovery']['message'].lower()
 assert s['recovery']['suggestions'][0]['label']=='Use recorded stage 2'

def test_exact_anatomy_name_in_wrong_id_field_is_repaired_but_ids_are_never_fuzzy():
 from pankagent_vnext.anatomy_resolution import resolve_anatomy
 records=[{'id':'UBERON_0001264','name':'pancreas','labels':['anatomical_structure']}]
 result=resolve_anatomy('Pancreas',records,RELEASE,'id')
 assert result['state']=='resolved' and result['id']=='UBERON_0001264'
 assert result['match_kind']=='verified_name_in_id_field'
 for value in ('pancrea','UBERON_0001265','CL:0000170'):
  assert resolve_anatomy(value,records,RELEASE,'id')['state']=='not_found'

def test_gene_annotation_categories_get_independent_checks_with_dependency_mapping():
 from pankagent_vnext.graph_contract import independent_measurement_steps
 from copy import deepcopy
 s={'id':'s1','question':'Show pathways and GO annotations for ZC3H11A',
    'relation_types':['FUNCTION_ANNOTATION','ASSOCIATED_WITH_GO'],'depends_on':[],
    'constraints':[{'entity_type':'Gene','property':'name','operator':'=','value':'ZC3H11A'}]}
 p={'steps':[s,{'id':'s2','question':'Inspect linked evidence','relation_types':[],
     'depends_on':['s1'],'constraints':[]}]}
 original=deepcopy(p);result=independent_measurement_steps(p)
 assert p==original
 assert [x['relation_types'] for x in result['steps'][:2]]==[['FUNCTION_ANNOTATION'],['ASSOCIATED_WITH_GO']]
 assert result['steps'][2]['depends_on']==['s1_annotation_1','s1_annotation_2']
 assert all(x['constraints']==s['constraints'] for x in result['steps'][:2])
 s['evidence_combination']='cooccurrence'
 assert independent_measurement_steps(p)==p
 s['evidence_combination']='independent'
 s['constraints'].append({'entity_type':'GO_term','property':'name','value':'specific term'})
 assert independent_measurement_steps(p)==p

@pytest.mark.parametrize('literal',[STAGE,'Stage 2: two or more autoantibodies, HbA1c >= 5.7%','Stage 2: dysglycemia'])
def test_verified_stage_is_bound_without_regenerating_source_bytes(literal):
 s=step();q="MATCH (d:donor)-[:HAS_SAMPLE]->(s:Sample_node)<-[:HAS_SAMPLE]-(a:anatomical_structure) WHERE d.t1d_stage = '"+literal+"' AND a.id = 'UBERON_0000006' AND s.data_modality IN ['scRNA-seq','snMultiomics'] RETURN d,s,a"
 rewritten,parameters,notes=bind_verified_categories(q,s,{})
 assert '$canonical_donor_stage' in rewritten
 assert parameters['canonical_donor_stage']==STAGE
 assert notes[0]['requested_stage']=='2'
 assert not validate_cypher(rewritten,s,parameters)

@pytest.mark.parametrize('query',["MATCH (d:disease) WHERE d.t1d_stage='Stage 2: bad' RETURN d", "MATCH (d:donor) WHERE d.t1d_stage='Stage 3: bad' RETURN d", "MATCH (d:donor) WHERE d.t1d_stage CONTAINS 'Stage 2: bad' RETURN d", "MATCH (d:donor) RETURN 'd.t1d_stage = \\\'Stage 2: bad\\\'' AS text"])
def test_binding_never_repairs_wrong_owner_stage_operator_or_text(query):
 rewritten,params,notes=bind_verified_categories(query,step(),{})
 assert rewritten==query and params=={} and notes==[]

@pytest.mark.parametrize('query',["MATCH (d:donor) WHERE d.t1d_stage='Stage 2: unterminated", "MATCH (d:donor) /* unclosed"])
def test_malformed_candidate_remains_available_to_validation_and_bounded_retry(query):
 rewritten,params,notes=bind_verified_categories(query,step(),{})
 assert rewritten==query and params=={} and notes==[]
 assert validate_cypher(rewritten,step(),params)

def test_canonical_parameter_never_overwrites_dependency_or_existing_parameter():
 q="MATCH (d:donor) WHERE d.t1d_stage='Stage 2: damaged' RETURN d"
 original={'dep_0':['donor-a'],'canonical_donor_stage':'different preexisting value'}
 rewritten,params,notes=bind_verified_categories(q,step(),original)
 assert rewritten==q and params==original and notes==[]
 assert original['canonical_donor_stage']=='different preexisting value'

def test_unicode_quoted_literals_and_comments_have_exact_spans():
 q="// comment\nMATCH (`d`:donor) WHERE d.t1d_stage='Stage 2: \\u1234' RETURN 'a''b'"
 for t in tokenize(q):
  assert q[t.start:t.end]
 assert [q[t.start:t.end] for t in tokenize(q) if t.kind=='STRING']==["'Stage 2: \\u1234'", "'a''b'"]

def test_stage_absence_requires_complete_current_inventory_not_guess():
 r=stage_recovery('2',{'stages':['Stage 1: normal','Stage 3: diagnosed'],'inventory_complete':True},RELEASE)
 assert r['category']=='recorded_stage_unavailable'
 assert 'No donors are recorded as stage 2' in r['message']
 assert len(r['suggestions'])==2 and not r['retryable']
 assert stage_recovery('99',VOCAB,RELEASE)['category']=='stage_needs_clarification'
 assert not step().get('recovery')

@pytest.mark.parametrize('vocabulary',[{}, {'stages':None}, {'stages':'Stage 1: normal'},
                                     {'stages':['Stage 1: normal']}, {'stages':[None],'inventory_complete':True}])
def test_missing_or_malformed_stage_inventory_never_claims_absence(vocabulary):
 r=stage_recovery('2',vocabulary,RELEASE)
 assert r['category']!='recorded_stage_unavailable'
 assert 'No donors' not in r['message']
 assert r['evidence'].get('source')!='complete distinct donor-stage inventory'
 assert r['retryable'] and not r['suggestions']

def test_verified_empty_stage_inventory_is_distinct_from_unavailable_inventory():
 r=stage_recovery('2',{'stages':[],'inventory_complete':True},RELEASE)
 assert r['category']=='recorded_stage_unavailable'
 assert r['evidence']['inventory_complete'] and not r['evidence']['recorded_stages']

def test_suggestions_use_actual_ambiguous_entity_candidates():
 p={'steps':[{'resolved_entities':[{'state':'ambiguous','requested':{'value':'lymph'},'candidates':[{'id':'UBERON_1','name':'lymph node'}]}]}]}
 r=plan_recovery(p,RELEASE)
 assert r['suggestions'][0]['instruction']=='Resolve lymph as lymph node (UBERON_1); keep every other filter.'


class CategoricalExecutionTests(unittest.IsolatedAsyncioTestCase):
 async def test_exact_category_parameters_reach_explain_retrieval_and_evidence(self):
  from tests_vnext.test_graph import FakeAdapter
  q="MATCH (d:donor)-[:HAS_SAMPLE]->(s:Sample_node)<-[:HAS_SAMPLE]-(a:anatomical_structure) WHERE d.t1d_stage='Stage 2: corrected typography' AND a.id='UBERON_0000006' AND s.data_modality IN ['scRNA-seq','snMultiomics'] RETURN d,s,a"
  adapter=FakeAdapter([[q]])
  adapter.settings.graph_version=RELEASE
  async def emit(*args): pass
  result=await adapter.execute(step(),{},emit)
  self.assertEqual(result['status'],'complete')
  self.assertEqual(len(adapter.retrieved),1)
  generated,params=adapter.retrieved[0]
  self.assertIn('$canonical_donor_stage',generated)
  self.assertEqual(params['canonical_donor_stage'],STAGE)
  self.assertEqual(adapter.explained,adapter.retrieved)
  self.assertEqual(result['queries'][0]['parameters'],params)
  self.assertEqual(result['validation'][0]['original_candidate_cypher'],q)
  self.assertEqual(result['generator_attempts'][0]['selected'],True)

 async def test_malformed_first_candidate_reaches_escalation_without_duplicate_read(self):
  from tests_vnext.test_graph import FakeAdapter
  q="MATCH (d:donor)-[:HAS_SAMPLE]->(s:Sample_node)<-[:HAS_SAMPLE]-(a:anatomical_structure) WHERE d.t1d_stage='Stage 2: fixed' AND a.id='UBERON_0000006' AND s.data_modality IN ['scRNA-seq','snMultiomics'] RETURN d,s,a"
  adapter=FakeAdapter([["MATCH (d:donor) WHERE d.t1d_stage='Stage 2: unclosed"],[q]])
  adapter.settings.graph_version=RELEASE
  async def emit(*args): pass
  result=await adapter.execute(step(),{},emit)
  self.assertEqual(result['status'],'complete')
  self.assertEqual([n for _,n in adapter.generated],[1,8])
  self.assertEqual(len(adapter.retrieved),1)
  self.assertIn('unterminated_string',result['validation'][0]['reasons'])


def test_failed_preview_is_technical_and_empty_is_not_failure():
 from pankagent_vnext.query_recovery import retrieval_recovery
 failed={'status':'failed','evidence':{'graph_version':RELEASE,'steps':[{'step_id':'s1','status':'failed','validation':[{'reasons':['missing_same_donor_sample_tissue_modality_path']}]}]}}
 result=retrieval_recovery(failed)
 assert result['category']=='query_validation' and result['retryable']
 assert result['suggestions']==[]
 assert retrieval_recovery({**failed,'status':'empty'}) is None
 assert retrieval_recovery({**failed,'status':'partial','preparation_complete':False}) is None
 assert retrieval_recovery({**failed,'status':'partial','preparation_complete':True})['category']=='query_validation'
 failed['error']={'category':'timeout'}
 assert retrieval_recovery(failed)['category']=='retrieval_unavailable'
