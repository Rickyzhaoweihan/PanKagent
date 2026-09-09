"""Coverage regressions use local fixtures, never model or graph requests."""
from copy import deepcopy
import json
import pytest

from pankagent_vnext.evidence_coverage import (
    VERSION, build_evidence_coverage, coverage_for_answer, complete_empty_message,
)
from pankagent_vnext.evidence_context import compact_evidence, scientific_excerpt
from pankagent_vnext.scope_guard import ScopeTextFilter, broad_cell_search

QUERY = 'MATCH (g:Gene)-[r:GENE_ENRICHED_IN]->(c:anatomical_structure) WHERE g.id = $gene RETURN g,r,c'


def check(*, query=QUERY, condition=False, restricted=False, status='complete', truncated=False,
          count=2, verified=True):
    constraints = [{'entity_type':'Gene','property':'id','operator':'=','value':'ENSG00000001626'}]
    if condition:
        constraints.append({'relationship_type':'GENE_ENRICHED_IN','property':'condition','operator':'=','value':'ND'})
    if restricted:
        constraints.append({'entity_type':'anatomical_structure','property':'id','operator':'=','value':'CL_0002079'})
    step = {'id':'s1','question':'Find CFTR enrichment','complete':True,'depends_on':[],
            'constraints':constraints,'relation_types':['GENE_ENRICHED_IN']}
    nodes = [{'id':'g','labels':['Gene'],'properties':{'name':'CFTR'}}] if count else []
    nodes += [{'id':f'c{i}','labels':['anatomical_structure'],'properties':{'name':f'Cell {i}'}} for i in range(count)]
    edges = [{'start_id':'g','end_id':f'c{i}','type':'GENE_ENRICHED_IN',
              'properties':{'comparison':'one_vs_rest','condition':'ND','log2_fold_change':7.62}}
             for i in range(count)]
    result = {'step_id':'s1','status':status,'truncated':truncated,'graph_version':'PanKgraph_08_04',
              'nodes':nodes,'edges':edges,'rows':[], 'queries':[{'cypher':query,'parameters':{'gene':'ENSG00000001626'}}],
              'requested_scope':{'constraints':constraints,'relation_types':step['relation_types'],'complete':True}}
    result['evidence_coverage'] = build_evidence_coverage(step,result,graph_version='PanKgraph_08_04',
        query=query,parameters={'gene':'ENSG00000001626'},validation_verified=verified)
    return step,result


def test_complete_two_record_search_is_exhaustive_without_redefining_one_vs_rest():
    _, result=check()
    coverage=result['evidence_coverage']
    assert coverage['query_scope']['complete_for_requested_scope'] is True
    assert coverage['query_scope']['cell_type_scope']=='all_matching'
    assert coverage['source_comparisons'][0]['comparator']=='remaining_cell_types_in_source_analysis'
    assert coverage['source_comparisons'][0]['record_count']==2
    assert broad_cell_search({'s1':result})


def test_condition_restriction_preserves_all_matching_cell_scope_under_that_condition():
    _,result=check(condition=True,query=QUERY.replace(' RETURN',' AND r.condition = "ND" RETURN'))
    coverage=result['evidence_coverage']['query_scope']
    assert coverage['cell_type_scope']=='all_matching'
    assert coverage['constraints'][-1]['value']=='ND'


@pytest.mark.parametrize('query',[
    QUERY.replace(' RETURN', ' AND c.id = "CL_0002079" RETURN'),
    QUERY.replace('(c:anatomical_structure)', '(c:anatomical_structure {id: "CL_0002079"})'),
    QUERY.replace(' RETURN', ' AND r.cell_type = "Ductal" RETURN'),
])
def test_actual_cell_filters_do_not_gain_broad_scope_even_if_plan_omitted_them(query):
    _,result=check(query=query)
    assert result['evidence_coverage']['query_scope']['cell_type_scope']=='restricted'
    assert not broad_cell_search({'s1':result})


@pytest.mark.parametrize('field',[
    'cell_type_label','expression_cell_type','cell_type_name','cell_label',
    'target_record_id','target_subtype_id','targetSubtypeId','cell_ontology_identifier',
    'expression_cell_subtype_name','cell_type_source_column',
])
@pytest.mark.parametrize('typed',[False,True])
def test_relationship_cell_identity_family_never_certifies_all_cells(field,typed):
    query=QUERY.replace(' RETURN',f' AND r.{field} = "Ductal" RETURN')
    step,result=check(query=query)
    if typed:
        step['constraints'].append({'relationship_type':'GENE_ENRICHED_IN',
                                    'property':field,'operator':'=','value':'Ductal'})
    result['evidence_coverage']=build_evidence_coverage(step,result,
        graph_version='PanKgraph_08_04',query=query,parameters={'gene':'ENSG00000001626'},
        validation_verified=True)
    assert result['evidence_coverage']['query_scope']['cell_type_scope']=='restricted'
    assert not broad_cell_search({'s1':result})


def test_relationship_cell_label_in_property_map_is_restricted():
    query=QUERY.replace('[r:GENE_ENRICHED_IN]', '[r:GENE_ENRICHED_IN {cell_type_label:"Ductal"}]')
    _,result=check(query=query)
    assert result['evidence_coverage']['query_scope']['cell_type_scope']=='restricted'


def test_cell_statistics_and_condition_filters_preserve_all_matching_cell_scope():
    query=QUERY.replace(' RETURN',' AND r.mean_pct_cells_expressing > 10 AND r.condition = "ND" RETURN')
    step,result=check(query=query,condition=True)
    step['constraints'].append({'relationship_type':'GENE_ENRICHED_IN',
                               'property':'mean_pct_cells_expressing','operator':'>','value':10})
    coverage=build_evidence_coverage(step,result,graph_version='PanKgraph_08_04',
        query=query,parameters={'gene':'ENSG00000001626'},validation_verified=True)
    assert coverage['query_scope']['cell_type_scope']=='all_matching'


@pytest.mark.parametrize('query',[
    QUERY+' LIMIT 2',
    QUERY.replace(' RETURN',' MATCH (c)-[:HAS_CELL_TYPE]->(t:anatomical_structure) RETURN'),
    QUERY.replace(' RETURN',' OPTIONAL MATCH (c)-[:HAS_CELL_TYPE]->(t:anatomical_structure) RETURN'),
    QUERY+' UNION '+QUERY,
    QUERY.replace('RETURN g,r,c','WITH g,c,r WHERE c.id = "CL_0002079" RETURN g,r,c'),
    QUERY.replace('RETURN g,r,c','RETURN collect(c)[..2]'),
    QUERY.replace('RETURN g,r,c','RETURN CASE WHEN c.id = "CL_0002079" THEN c ELSE null END'),
])
def test_unknown_or_bounded_query_shapes_never_certify_all_cells(query):
    _,result=check(query=query)
    assert result['evidence_coverage']['query_scope']['cell_type_scope']=='unknown'
    assert not broad_cell_search({'s1':result})


def test_collection_projection_and_order_do_not_shrink_cell_scope():
    _,result=check(query=QUERY.replace('RETURN g,r,c','WITH collect(g)+collect(c) AS nodes, collect(r) AS edges RETURN nodes,edges'))
    assert result['evidence_coverage']['query_scope']['cell_type_scope']=='all_matching'


def test_dependency_and_untyped_identity_filters_remain_unknown():
    step,result=check()
    for change in ({'depends_on':['previous']}, {'constraints':[{'property':'id','value':'c'}]}):
        altered={**step,**change}
        coverage=build_evidence_coverage(altered,result,graph_version='PanKgraph_08_04',query=QUERY,validation_verified=True)
        assert coverage['query_scope']['cell_type_scope']=='unknown'


@pytest.mark.parametrize('status,truncated,verified',[
    ('failed',False,True),('partial',False,True),('complete',True,True),('complete',False,False),
])
def test_no_exhaustive_claim_for_failure_partial_truncation_or_unverified(status,truncated,verified):
    _,result=check(status=status,truncated=truncated,verified=verified)
    assert not result['evidence_coverage']['query_scope']['complete_for_requested_scope']
    assert not broad_cell_search({'s1':result})


def test_legacy_and_later_interrupted_records_are_not_retroactively_verified():
    _,result=check()
    legacy=deepcopy(result);legacy.pop('evidence_coverage')
    assert coverage_for_answer(legacy)['query_scope']['verification']=='unknown'
    assert not broad_cell_search({'s1':legacy})
    result['status']='partial'
    assert not coverage_for_answer(result)['query_scope']['complete_for_requested_scope']


def test_compaction_keeps_full_source_comparison_and_complete_search_metadata():
    _,result=check(count=130)
    before=deepcopy(result)
    compact=compact_evidence({'s1':result})
    assert compact[0]['context_sampled']
    excerpt=scientific_excerpt(compact)[0]
    coverage=excerpt['evidence_coverage']
    assert coverage['source_comparisons'][0]['record_count']==130
    assert coverage['query_scope']['complete_for_requested_scope'] is True
    assert coverage['query_scope']['cell_type_scope']=='all_matching'
    assert len(excerpt['edges']) < 130
    assert result==before


def test_restricted_query_preserves_source_analysis_comparator():
    _,result=check(restricted=True,query=QUERY.replace(' RETURN',' AND c.id = "CL_0002079" RETURN'))
    assert result['evidence_coverage']['query_scope']['cell_type_scope']=='restricted'
    assert result['evidence_coverage']['source_comparisons'][0]['comparator']=='remaining_cell_types_in_source_analysis'
    assert not broad_cell_search({'s1':result})


def test_complete_empty_has_confident_but_scoped_database_conclusion():
    _,result=check(count=0,status='empty')
    message=complete_empty_message({'s1':result})
    assert 'PanKgraph contains no matching cell-type enrichment records' in message
    assert 'requested entities and filters' in message and 'PanKgraph_08_04' in message
    assert '[G1]' in message and 'not proof' in message
    _,legacy=check(count=0,status='empty');legacy.pop('evidence_coverage')
    assert complete_empty_message({'s1':legacy}) is None


def test_zero_scalar_is_evidence_not_empty_search_error():
    step,result=check(count=0,status='complete')
    result['rows']=[{'count':0}]
    result['evidence_coverage']=build_evidence_coverage(step,result,graph_version='PanKgraph_08_04',
        query=QUERY,parameters={'gene':'ENSG00000001626'},validation_verified=True)
    assert complete_empty_message({'s1':result}) is None
    assert result['evidence_coverage']['query_scope']['complete_for_requested_scope']


def test_current_cftr_screenshot_contradiction_is_corrected_across_stream_chunks():
    _,result=check()
    guard=ScopeTextFilter({'s1':result})
    text=('No enrichment records for CFTR in other (non-ductal) cell types were returned, so '
          'comparison is limited to these two ductal-lineage entries; this reflects retrieval scope, '
          'not confirmed absence elsewhere. This is enrichment evidence (one-vs-rest DE), '
          'not an annotated marker-gene relationship. [G1]')
    output=''.join(guard.feed(text[i:i+9]) for i in range(0,len(text),9))+guard.feed('',final=True)
    assert 'comparison is limited' not in output
    assert 'all matching cell types' in output
    assert 'remaining cell types profiled in its source analysis' in output
    assert 'not an annotated marker-gene relationship' in output
    assert '[G1]' in output
    assert guard.corrections==1


def test_source_scope_guard_works_for_restricted_query_without_claiming_broad_search():
    _,result=check(restricted=True)
    guard=ScopeTextFilter({'s1':result})
    output=guard.feed('This one-versus-rest comparison is limited to these two returned cell types. [G1]',final=True)
    assert 'remaining cell types profiled' in output
    assert 'all matching cell types' not in output
    assert '[G1]' in output


def test_unrelated_numerical_conclusions_and_legitimate_query_scope_are_unchanged():
    _,result=check()
    text='CFTR has a log2 fold change of 7.62 [G1].\n\nEnrichment is not a marker annotation.'
    guard=ScopeTextFilter({'s1':result})
    assert guard.feed(text,final=True)==text
    assert guard.corrections==0
    _,restricted=check(restricted=True)
    text='The query checked only ductal cells; it cannot establish enrichment elsewhere.'
    assert ScopeTextFilter({'s1':restricted}).feed(text,final=True)==text


def test_one_complete_category_does_not_hide_another_incomplete_cell_check():
    _,complete=check();_,partial=check(status='partial')
    # The exact same category/gene/filter scope is fully covered by s1.
    assert broad_cell_search({'s1':complete,'s2':partial})
    # A distinct condition in the incomplete check is still not covered.
    step,partial=check(status='partial',condition=True)
    assert not broad_cell_search({'s1':complete,'s2':partial})


def test_prepared_model_contract_separates_verified_scope_and_examples(tmp_path):
    from types import SimpleNamespace
    from pankagent_vnext.llm import ClaudeGateway, ANSWER_CONTRACT
    _,result=check(count=130)
    gateway=ClaudeGateway(SimpleNamespace(state_dir=tmp_path,budget_usd=20,anthropic_key='',plan_timeout=45))
    prepared=gateway.prepare_answer('Does CFTR show cell-type enrichment?',{'s1':result})
    payload=json.loads(prepared.body)
    assert payload['evidence'][0]['evidence_coverage']['query_scope']['complete_for_requested_scope']
    assert prepared.profile['evidence_coverage_version']==VERSION
    assert prepared.profile['context_sampled']
    assert 'all matching cell types' in payload['verified_search_scope']
    assert 'zero matches means no matching PanKgraph record' in ANSWER_CONTRACT


def test_true_denial_of_limited_comparison_is_not_rewritten():
    _,result=check()
    text='The one-versus-rest comparison is not limited to these two returned populations.'
    guard=ScopeTextFilter({'s1':result})
    assert guard.feed(text,final=True)==text
    assert guard.corrections==0


@pytest.mark.parametrize('text',[
    'The QTL comparison is limited to these two returned tissues, Islets and Pancreas. [G2]',
    'The comparison is limited to these two returned tissues. [G1]',
    'This one-versus-rest comparison is limited to these two returned cell types. [G2]',
    'This one-versus-rest comparison is limited to these two returned cell types. [G99]',
    'The comparison is limited to these two returned groups.',
    'This one-versus-rest comparison is limited to these two returned cell types.',
])
def test_scope_correction_never_borrows_enrichment_support_for_another_category(text):
    _,enrichment=check()
    qtl={'step_id':'s2','status':'complete','nodes':[],'edges':[],
         'requested_scope':{'relation_types':['PART_OF_QTL_SIGNAL']}}
    guard=ScopeTextFilter({'s1':enrichment,'s2':qtl})
    assert guard.feed(text,final=True)==text
    assert guard.corrections==0


def test_mixed_profile_correction_uses_the_cited_measurement_scope_only():
    _,restricted=check(restricted=True)
    _,broad=check()
    qtl={'step_id':'s3','status':'complete','nodes':[],'edges':[],
         'requested_scope':{'relation_types':['PART_OF_QTL_SIGNAL']}}
    text='This one-versus-rest comparison is limited to these two returned cell types. [G1]'
    guard=ScopeTextFilter({'s1':restricted,'s2':broad,'s3':qtl})
    output=guard.feed(text,final=True)
    assert 'remaining cell types profiled' in output
    assert 'complete PanKgraph search' not in output
    assert '[G1]' in output and '[G2]' not in output


def test_cited_complete_enrichment_correction_still_works_in_mixed_profile():
    _,enrichment=check()
    qtl={'step_id':'s2','status':'complete','nodes':[],'edges':[],
         'requested_scope':{'relation_types':['PART_OF_QTL_SIGNAL']}}
    text='No other cell types were searched. [G1]'
    guard=ScopeTextFilter({'s1':enrichment,'s2':qtl})
    assert 'all matching cell types' in guard.feed(text,final=True)


@pytest.mark.parametrize('relation',['GENE_DETECTED_IN','GENE_ACTIVITY_SCORE_IN'])
def test_complete_retrieval_cannot_redefine_unrecorded_source_comparison(relation):
    query=QUERY.replace('GENE_ENRICHED_IN',relation)
    step,result=check(query=query)
    step['relation_types']=[relation]
    result['requested_scope']['relation_types']=[relation]
    for edge in result['edges']:
        edge['type']=relation
        edge['properties'].pop('comparison')
    result['evidence_coverage']=build_evidence_coverage(step,result,
        graph_version='PanKgraph_08_04',query=query,parameters={'gene':'ENSG00000001626'},
        validation_verified=True)
    assert broad_cell_search({'s1':result})
    assert not coverage_for_answer(result)['source_comparisons']
    for text in ('The comparison is limited to these two returned cell types. [G1]',
                 'No other cell types were examined in the original analysis. [G1]'):
        guard=ScopeTextFilter({'s1':result})
        assert guard.feed(text,final=True)==text
        assert guard.corrections==0
    output=ScopeTextFilter({'s1':result}).feed('No other cell types were queried. [G1]',final=True)
    assert 'all matching cell types' in output
    assert 'one-versus-rest' not in output


@pytest.mark.parametrize('change',['query','parameters','release','missing_query'])
def test_modified_execution_identity_cannot_reuse_verified_coverage(change):
    _,result=check()
    if change=='query': result['queries'][-1]['cypher']+= ' LIMIT 2'
    if change=='parameters': result['queries'][-1]['parameters']['gene']='other_gene'
    if change=='release': result['graph_version']='other_release'
    if change=='missing_query': result['queries']=[]
    answer_scope=coverage_for_answer(result)
    assert answer_scope['query_scope']['verification']=='unknown'
    assert not answer_scope['query_scope']['complete_for_requested_scope']
    assert answer_scope['source_comparisons'][0]['record_count']==2
    assert not broad_cell_search({'s1':result})


def test_coloc_linkage_roles_counts_and_reference_identity_survive_context_reduction():
    _,result=check(count=130)
    result['coloc_linkage']={'version':'fixture','groups':[{'primary_record_count':2,
        'linked_record_count':2,'records':[{'gwas_signal_id':'signal-A',
        'qtl_signal_id':'signal-B','match_kinds':['verified_gwas_credible_set_member'],
        'supporting_references':[{'record_sha256':'reference-hash','step_id':'s1_gwas'}]}]}]}
    context=scientific_excerpt(compact_evidence({'s1':result}))[0]
    assert context['coloc_linkage']==result['coloc_linkage']
    from pankagent_vnext.llm import ANSWER_CONTRACT
    assert 'common gene or disease alone is not a shared-signal match' in ANSWER_CONTRACT
    assert 'never erase primary recorded colocalization' in ANSWER_CONTRACT


def test_actual_cftr_restricted_check_and_broad_check_share_only_exact_scope():
    _,restricted=check(restricted=True)
    _,broad=check()
    assert broad_cell_search({'s1':restricted,'s2':broad})
    text='No other cell types were searched.'
    guarded=ScopeTextFilter({'s1':restricted,'s2':broad}).feed(text,final=True)
    assert 'all matching cell types' in guarded


@pytest.mark.parametrize('different',['gene','condition','category','untyped'])
def test_broad_check_does_not_cover_other_gene_condition_category_or_unknown_scope(different):
    _,broad=check()
    step,restricted=check(restricted=True)
    if different=='gene':
        step['constraints'][0]['value']='ENSG_OTHER'
    if different=='condition':
        step['constraints'].append({'relationship_type':'GENE_ENRICHED_IN','property':'condition','value':'T1D'})
    if different=='category':
        step['relation_types']=['MARKER_GENE_OF']
    if different=='untyped':
        step['constraints'][0].pop('entity_type')
    restricted['evidence_coverage']=build_evidence_coverage(step,restricted,graph_version='PanKgraph_08_04',
        query=QUERY,parameters={'gene':'ENSG00000001626'},validation_verified=True)
    assert not broad_cell_search({'s1':restricted,'s2':broad})


def test_count_projection_is_complete_aggregate_not_enumerated_cell_membership():
    step,result=check(query=QUERY.replace('RETURN g,r,c','RETURN count(DISTINCT c) AS count'))
    result.update(nodes=[],edges=[],rows=[{'count':2}])
    result['evidence_coverage']=build_evidence_coverage(step,result,graph_version='PanKgraph_08_04',
        query=result['queries'][-1]['cypher'],parameters=result['queries'][-1]['parameters'],validation_verified=True)
    coverage=coverage_for_answer(result)
    assert coverage['query_scope']['complete_for_requested_scope']
    assert coverage['result_representation']=='aggregate_result'
    assert not coverage['record_membership_enumerated']
    assert not broad_cell_search({'s1':result})


def test_lossy_projection_cannot_get_exhaustive_evidence_claim_from_complete_status():
    _,result=check(query=QUERY.replace('RETURN g,r,c','RETURN g'))
    assert not result['evidence_coverage']['query_scope']['complete_for_requested_scope']
    assert not result['evidence_coverage']['record_membership_enumerated']
    assert not broad_cell_search({'s1':result})
