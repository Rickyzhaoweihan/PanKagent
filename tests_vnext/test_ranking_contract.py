"""The selected top set follows explicit biological direction and ranking."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from pankagent_vnext.graph import tokenize,validate_cypher
from pankagent_vnext.graph_contract import generation_request
from pankagent_vnext.ranking_contract import attach_to_plan,parse_intent,validation_errors,FIELDS,RELEASE

QUESTION='What are the 5 most upregulated genes in the beta cell in type 1 diabetes?'
BASE='MATCH (g:Gene)-[r:T1D_DEG_IN]->(c:anatomical_structure) '
GOOD=BASE+'WHERE r.log2_fold_change > 0 RETURN g,r,c ORDER BY r.log2_fold_change DESC LIMIT 5'

def step(question=QUESTION,relation='T1D_DEG_IN'):
    raw={'original_question':question,'steps':[{'id':'s1','question':question,'constraints':[],
        'relation_types':[relation],'graph_version':RELEASE,'complete':True}]}
    return attach_to_plan(raw)['steps'][0]

def errors(query,current=None,params=None):
    return validation_errors(tokenize(query),current or step(),params or {})


def test_actual_reported_misranking_rejected():
    bad=BASE+'RETURN g,r,c ORDER BY r.adjusted_p_value ASC LIMIT 5'
    assert 'missing_requested_effect_direction:positive' in errors(bad)
    assert any(e.startswith('wrong_requested_order:') for e in errors(bad))
    assert validate_cypher(GOOD,step())==[]
    assert validate_cypher(bad,step())


@pytest.mark.parametrize('question,direction,prop,order',[
 (QUESTION,'positive','log2_fold_change','DESC'),
 ('Find the 5 most downregulated genes','negative','log2_fold_change','ASC'),
 ('Find top5 most up-regulated genes','positive','log2_fold_change','DESC'),
 ('Find the 5 most statistically significant upregulated genes','positive','adjusted_p_value','ASC'),
 ('Find the 5 most significantly upregulated genes','positive','adjusted_p_value','ASC'),
 ('Rank upregulated genes by nominal p value','positive','p_value','ASC'),
])
def test_distinct_explicit_intents(question,direction,prop,order):
    contract=step(question)['ranking_contract']
    assert (contract['effect_direction'],contract['property'],contract['order'])==(direction,prop,order)


@pytest.mark.parametrize('question',['Is CFTR upregulated in T1D?', 'Is CFTR specifically enriched in ductal cells?', 'Show the recorded log2 fold change for CFTR'])
def test_yes_no_measurement_lookup_does_not_filter_out_negative_answer(question):
    assert not step(question).get('ranking_contract')


@pytest.mark.parametrize('question',['Find top 5 beta-cell genes','Find 5 genes','Find top 5 expressed genes'])
def test_ambiguous_top_is_not_given_invented_metric(question):
    if 'top' in question:
        assert step(question)['ranking_issue']['category']=='ranking_needs_clarification'
    else:
        assert not step(question).get('ranking_contract')


def test_ambiguous_detection_suggestions_name_real_metrics():
    issue=step('Find top 5 expressed genes','GENE_DETECTED_IN')['ranking_issue']
    assert len(issue['suggestions'])==2
    assert 'median' in issue['suggestions'][0]['instruction']


def test_expression_rank_is_not_effect_or_significance_rank():
    s=step('Find top 5 genes with highest median log CPM','GENE_DETECTED_IN')
    assert s['ranking_contract']['property']=='median_donor_log_cpm'
    assert 'effect_direction' not in s['ranking_contract']
    query=BASE.replace('T1D_DEG_IN','GENE_DETECTED_IN')+'RETURN g,r ORDER BY r.median_donor_log_cpm DESC LIMIT 5'
    assert errors(query,s)==[]


@pytest.mark.parametrize('fragment',[
 'r.log2_fold_change ASC','r.adjusted_p_value ASC', 'g.log2_fold_change DESC',
 '-r.log2_fold_change DESC', 'abs(r.log2_fold_change) DESC', 'r.log2_fold_change * -1 DESC',
 'r.adjusted_p_value ASC, r.log2_fold_change DESC',
])
def test_wrong_primary_order_or_owner_or_expression_rejected(fragment):
    assert errors(GOOD.replace('r.log2_fold_change DESC',fragment))


@pytest.mark.parametrize('fragment',['r.log2_fold_change >= 0','r.log2_fold_change < 0','r.log2_fold_change > 1','g.log2_fold_change > 0', 'NOT r.log2_fold_change > 0', 'r.log2_fold_change > 0 OR true'])
def test_direction_exact_owner_sign_and_boolean_scope(fragment):
    assert errors(GOOD.replace('r.log2_fold_change > 0',fragment))


def test_other_relationship_direction_cannot_satisfy_ranked_edge():
    query=BASE+'MATCH(g)-[e:T1D_DEG_IN]->(other) WHERE e.log2_fold_change>0 RETURN g,r ORDER BY r.log2_fold_change DESC LIMIT 5'
    assert 'effect_direction_not_bound_to_ranked_relationship' in errors(query)


@pytest.mark.parametrize('query',[
 BASE+'WHERE r.log2_fold_change>0 WITH g,r AS measured,c RETURN g,measured,c ORDER BY measured.log2_fold_change DESC LIMIT 5',
 BASE+'WHERE r.log2_fold_change>0 RETURN g,r.log2_fold_change AS effect ORDER BY effect DESC LIMIT 5',
 BASE+'WHERE r.log2_fold_change>0 WITH g,r.log2_fold_change AS effect RETURN g,effect AS magnitude ORDER BY magnitude DESC LIMIT 5',
 BASE+'WHERE r.log2_fold_change>0 WITH g,r AS r RETURN g,r ORDER BY r.log2_fold_change DESC LIMIT 5',
])
def test_simple_aliases_keep_exact_relationship_binding(query):
    assert errors(query)==[]


@pytest.mark.parametrize('query',[
 BASE+'WHERE r.log2_fold_change>0 RETURN g,-r.log2_fold_change AS effect ORDER BY effect DESC LIMIT 5',
 BASE+'WHERE r.log2_fold_change>0 WITH g,r.log2_fold_change AS effect RETURN g,0 AS effect ORDER BY effect DESC LIMIT 5',
 BASE+'WHERE r.log2_fold_change>0 RETURN g,r+r AS evidence ORDER BY evidence.log2_fold_change DESC LIMIT 5',
])
def test_expression_or_shadowed_alias_is_not_trusted(query):
    assert errors(query)


@pytest.mark.parametrize('query',[
 GOOD.replace('LIMIT 5','LIMIT 1'), GOOD.replace('LIMIT 5',''),
 GOOD+' SKIP 1', GOOD+' MATCH (x) RETURN x',
 GOOD.replace(' RETURN g,r,c',' WITH g,r,c LIMIT 100 RETURN g,r,c'),
 GOOD+' UNION '+GOOD, GOOD.replace(' DESC LIMIT',' DESC WITH g,r MATCH(x) RETURN g,r LIMIT'),
])
def test_top_number_scope_and_global_order_are_preserved(query):
    assert errors(query)


def test_requested_limit_parameter_supported():
    assert errors(GOOD.replace('LIMIT 5','LIMIT $n'),params={'n':5})==[]
    assert errors(GOOD.replace('LIMIT 5','LIMIT $n'),params={'n':True})


def test_original_intent_overrides_planner_substitution():
    p={'original_question':QUESTION,'steps':[step('Find 5 most significant genes')]}
    result=attach_to_plan(p)['steps'][0]
    assert result['ranking_contract']['property']=='log2_fold_change'


def revised(instruction,question='Find genes in alpha cells'):
    parent=step();raw={'steps':[{**parent,'question':question}],
        'revision_trace':{'before_steps':[parent],'instruction':instruction}}
    original=deepcopy(raw);new=attach_to_plan(raw)
    assert raw==original
    return new['steps'][0]


def test_unrelated_revision_preserves_number_direction_and_statistic():
    result=revised('Use alpha cells instead.')
    assert result['ranking_contract']['top_n']==5
    assert result['ranking_contract']['property']=='log2_fold_change'
    assert result['ranking_contract']['effect_direction']=='positive'


def test_direction_revision_changes_sort_but_keeps_limit():
    result=revised('Use downregulated instead.')['ranking_contract']
    assert (result['effect_direction'],result['order'],result['top_n'])==('negative','ASC',5)


def test_significance_revision_changes_metric_but_keeps_direction_and_limit():
    result=revised('Rank by adjusted p value instead.')['ranking_contract']
    assert (result['property'],result['effect_direction'],result['top_n'])==('adjusted_p_value','positive',5)


def test_explicit_remove_limit_does_not_remove_direction():
    result=revised('Remove the limit and return all genes.')['ranking_contract']
    assert 'top_n' not in result
    assert result['effect_direction']=='positive'


def test_changed_direction_to_both_requires_explicit_scope():
    assert revised('Include upregulated and downregulated genes.')['ranking_issue']


def test_other_release_does_not_silently_reuse_contract():
    assert not attach_to_plan({'steps':[{'question':QUESTION,'relation_types':['T1D_DEG_IN']}]},'other')['steps'][0].get('ranking_contract')
    assert errors(GOOD,{**step(),'graph_version':'other'})==['ranking_contract_stale']


def test_initial_generator_gets_constraints_without_another_model_call():
    text=generation_request(step(),QUESTION)
    assert 'log2_fold_change > 0' in text and 'ORDER BY' in text and 'LIMIT 5' in text
    assert 'Do not substitute p-value ranking' in text


def test_fields_against_independent_release_inventory():
    path=Path(__file__).parents[1]/'pankagent_vnext/release_schema.json'
    if not path.exists():pytest.skip('Full registry compared in tracked source; minimal promotion omits it.')
    data=json.loads(path.read_text())
    for kind,fields in FIELDS.items():assert set(fields.values())<=set(data['relations'][kind]['properties'])


def test_unrequested_significance_cutoff_cannot_change_effect_ranked_population():
    bad=GOOD.replace('> 0','> 0 AND r.adjusted_p_value < 0.05')
    assert 'unrequested_ranking_cutoff:adjusted_p_value' in errors(bad)
    current=step();current['constraints']=[{'relationship_type':'T1D_DEG_IN','property':'adjusted_p_value','operator':'<','value':'0.05'}]
    assert errors(bad,current)==[]


def test_other_relationship_threshold_does_not_authorize_ranked_edge_threshold():
    current=step();current['constraints']=[{'relationship_type':'GENE_ENRICHED_IN','property':'adjusted_p_value','operator':'<','value':'0.05'}]
    assert errors(GOOD.replace('> 0','> 0 AND r.adjusted_p_value < 0.05'),current)


def test_additional_context_check_does_not_allow_primary_rank_loss():
    raw={'original_question':QUESTION,'steps':[step('Find 5 most significant genes'),{'id':'context','relation_types':['FUNCTION_ANNOTATION'],'question':'Describe annotations'}]}
    assert attach_to_plan(raw)['steps'][0]['ranking_contract']['property']=='log2_fold_change'


@pytest.mark.parametrize('query',[
 GOOD.replace('RETURN g,r,c','WITH g,r,c').replace('LIMIT 5','WITH collect(g) AS nodes,collect(r) AS edges LIMIT 5 RETURN nodes,edges'),
 GOOD.replace('RETURN g,r,c','RETURN collect(g) AS genes,r'),
])
def test_requested_limit_selects_measurements_before_graph_aggregation(query):
    assert 'ranking_limit_must_precede_graph_aggregation' in errors(query)


def test_selected_measurements_can_be_collected_after_limit():
    query=GOOD.replace('RETURN g,r,c','WITH g,r,c')+' RETURN collect(g) AS nodes,collect(r) AS edges'
    assert errors(query)==[]


@pytest.mark.parametrize('suffix',['+5','*2','/5','-1'])
def test_limit_literal_prefix_is_not_the_requested_limit(suffix):
    assert 'unsupported_top_limit_expression' in errors(GOOD+suffix)


@pytest.mark.parametrize('predicate',[
 'r.log2_fold_change > 0+5', 'r.log2_fold_change > 0*10', '-r.log2_fold_change > 0',
 'abs(r.log2_fold_change)>0', 'coalesce(r.log2_fold_change,0)>0',
 'r.log2_fold_change > false', "r.log2_fold_change > '0'",
])
def test_direction_requires_numeric_comparison_not_expression_prefix(predicate):
    assert 'missing_requested_effect_direction:positive' in errors(GOOD.replace('r.log2_fold_change > 0',predicate))


def test_exact_projected_effect_alias_can_preserve_sign_filter():
    query=BASE+'WITH g,r.log2_fold_change AS effect WHERE effect>0 RETURN g,effect ORDER BY effect DESC LIMIT 5'
    assert errors(query)==[]


def test_projected_statistic_alias_does_not_hide_unrequested_cutoff():
    query=BASE+'WHERE r.log2_fold_change>0 WITH g,r,r.adjusted_p_value AS p WHERE p<0.05 RETURN g,r ORDER BY r.log2_fold_change DESC LIMIT 5'
    assert 'unrequested_ranking_cutoff:adjusted_p_value' in errors(query)
    current=step();current['constraints']=[{'relationship_type':'T1D_DEG_IN','property':'adjusted_p_value','operator':'<','value':'0.050'}]
    assert errors(query,current)==[]


@pytest.mark.parametrize('option,expected',[(0,'log2_fold_change'),(1,'adjusted_p_value')])
def test_clarification_suggestion_actually_resolves_metric_without_losing_top(option,expected):
    parent=step('Find top 5 beta-cell genes')
    instruction=parent['ranking_issue']['suggestions'][option]['instruction']
    plan={'steps':[parent],'revision_trace':{'before_steps':[parent],'instruction':instruction}}
    revised_step=attach_to_plan(plan)['steps'][0]
    assert 'ranking_issue' not in revised_step
    assert revised_step['ranking_contract']['property']==expected
    assert revised_step['ranking_contract']['top_n']==5


@pytest.mark.parametrize('query',[
 GOOD.replace('[r:T1D_DEG_IN]','[r:T1D_DEG_IN {adjusted_p_value:0.001}]'),
 GOOD.replace('> 0','> 0 AND r.adjusted_p_value IN [0.001,0.002]'),
 GOOD.replace(' WHERE',' MATCH(other:Gene)-[e:T1D_DEG_IN]->(othercell) WHERE').replace('RETURN g,r,c','RETURN other,e,othercell'),
 GOOD.replace(' WHERE',' MATCH(x:Gene) WHERE').replace('RETURN g,r,c','RETURN g,r,c,x'),
])
def test_reviewed_cutoff_and_population_shape_bypasses_are_rejected(query):
    assert errors(query)
    assert validate_cypher(query,step())


@pytest.mark.parametrize('tail',['RETURN c,r','RETURN 5 AS gene_count','RETURN size(collect(g)) AS total','RETURN g.id = c.id AS same_identity'])
def test_ranked_gene_identity_cannot_be_replaced_with_other_result(tail):
    query=GOOD.replace('RETURN g,r,c','WITH g,r,c')+' '+tail
    assert 'ranking_must_return_selected_gene_identities' in errors(query)


def test_graph_collection_after_selection_keeps_gene_identity():
    query=GOOD.replace('RETURN g,r,c','WITH g,r,c')+' WITH collect(g)+collect(c) AS nodes,collect(r) AS edges RETURN nodes,edges'
    assert errors(query)==[]


def test_direct_gene_identifier_projection_is_supported():
    query=GOOD.replace('RETURN g,r,c','RETURN g.id AS gene_id,r.log2_fold_change AS effect')
    assert errors(query)==[]


@pytest.mark.parametrize('query',[
 GOOD.replace('RETURN g,r,c',"RETURN g.id + '_other' AS gene_id,r,c"),
 GOOD.replace('RETURN g,r,c','RETURN [g][0..0] AS nodes,r,c'),
 GOOD.replace('RETURN g,r,c','RETURN CASE WHEN false THEN g ELSE c END AS node,r,c'),
 GOOD.replace('RETURN g,r,c','WITH g,r,c')+' RETURN collect(g)[0..1] AS nodes,collect(r) AS edges',
])
def test_selected_gene_projection_cannot_be_transformed_or_omitted(query):
    assert 'ranking_must_return_selected_gene_identities' in errors(query)
    assert validate_cypher(query,step())


def test_unsliced_graph_map_after_selection_is_supported():
    query=GOOD.replace('RETURN g,r,c','WITH g,r,c')+' RETURN {nodes:collect(g)+collect(c),edges:collect(r)} AS graph'
    assert errors(query)==[]


def test_wildcard_keeps_selected_identities():
    assert errors(GOOD.replace('RETURN g,r,c','RETURN *'))==[]


def test_explicit_remove_limit_is_enforced_by_query_validation():
    current=revised('Remove the limit and return all genes.')
    assert current['complete'] is True
    assert 'incomplete_limit_or_slice' in validate_cypher(GOOD,current)
    assert errors(GOOD.replace(' LIMIT 5',''),current)==[]


def test_direction_only_count_request_still_accepts_legitimate_scalar_count():
    current=step('Count how many genes are upregulated in beta cells in T1D')
    query=BASE+'WHERE r.log2_fold_change>0 RETURN count(DISTINCT g) AS gene_count'
    assert 'property' not in current['ranking_contract']
    assert errors(query,current)==[]
    assert validate_cypher(query,current)==[]
    assert errors(query.replace('WHERE r.log2_fold_change>0 ',''),current)
