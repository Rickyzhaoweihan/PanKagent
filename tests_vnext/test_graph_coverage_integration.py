"""Exercise coverage at the adapter boundary, with no model/database network."""
from copy import deepcopy
import asyncio
from test_graph import FakeAdapter
from pankagent_vnext.evidence_context import compact_evidence, scientific_excerpt
from pankagent_vnext.evidence_status import outcome_message

QUERY = "MATCH (g:Gene)-[r:GENE_ENRICHED_IN]->(c:anatomical_structure) WHERE g.name='CFTR' RETURN g,r,c"
STEP = {'id':'s1','question':'Which cell types have CFTR enrichment?',
        'constraints':[{'entity_type':'Gene','property':'name','operator':'=','value':'CFTR'}],
        'relation_types':['GENE_ENRICHED_IN'],'complete':True,'depends_on':[]}


async def emit(*args):
    pass


def test_accepted_query_attests_scope_before_synthesis_sampling():
    adapter=FakeAdapter([[QUERY]])
    adapter.answer={
        'nodes':[{'id':'g','labels':['Gene'],'properties':{'name':'CFTR'}},
                 {'id':'cell','labels':['anatomical_structure'],'properties':{}}],
        'edges':[{'start_id':'g','end_id':'cell','type':'GENE_ENRICHED_IN',
                  'properties':{'comparison':'one_vs_rest'}}],
        'rows':[], 'status':'complete','truncated':False,
    }
    result=asyncio.run(adapter.execute(deepcopy(STEP),{},emit))
    assert result['evidence_coverage']['query_scope']['cell_type_scope']=='all_matching'
    assert result['evidence_coverage']['query_scope']['complete_for_requested_scope']
    compact=scientific_excerpt(compact_evidence({'s1':result}))[0]
    assert compact['evidence_coverage']['query_scope']['complete_for_requested_scope']
    assert compact['evidence_coverage']['source_comparisons'][0]['comparator']=='remaining_cell_types_in_source_analysis'


def test_current_complete_empty_gets_record_absence_without_another_model():
    adapter=FakeAdapter([[QUERY]])
    adapter.answer={'nodes':[],'edges':[],'rows':[],'status':'empty','truncated':False}
    result=asyncio.run(adapter.execute(deepcopy(STEP),{},emit))
    message=outcome_message({'s1':result})
    assert 'contains no matching cell-type enrichment records' in message
    assert 'search completed' in message
    assert len(adapter.generated)==len(adapter.retrieved)==1


def test_failed_validation_cannot_attest_no_matching_records():
    wrong=QUERY.replace('GENE_ENRICHED_IN','SIGNAL_COLOC_WITH')
    adapter=FakeAdapter([[wrong],[wrong]])
    result=asyncio.run(adapter.execute(deepcopy(STEP),{},emit))
    assert result['status']=='failed'
    assert not result.get('evidence_coverage',{}).get('query_scope',{}).get('complete_for_requested_scope')
    assert not adapter.retrieved
    assert 'retrieval failure' in outcome_message({'s1':result})


def test_materialization_truncation_never_becomes_complete_category_search():
    adapter=FakeAdapter([[QUERY]])
    adapter.answer.update(status='partial',truncated=True)
    result=asyncio.run(adapter.execute(deepcopy(STEP),{},emit))
    assert result['evidence_coverage']['query_scope']['complete_for_requested_scope'] is False


def test_scalar_dependency_cannot_be_mistaken_for_no_matching_entities():
    for count in (0, 12):
        adapter = FakeAdapter([])
        child = {**STEP, 'depends_on': ['count_step']}
        parent = {'status': 'complete', 'truncated': False, 'nodes': [], 'edges': [], 'rows': [{'count': count}]}
        result = asyncio.run(adapter.execute(child, {'count_step': parent}, emit))
        assert result['status'] == 'failed'
        assert result['validation'][-1]['reasons'] == ['dependency_missing_entity_ids:count_step']
        assert not adapter.generated and not adapter.retrieved


def test_explicit_bounded_population_propagates_without_global_completeness():
    adapter = FakeAdapter([['MATCH (n) WHERE n.id IN $dep_0 RETURN n']])
    child = {'id': 'child', 'question': 'Inspect the selected entities', 'constraints': [],
             'depends_on': ['selected'], 'complete': True, 'relation_types': []}
    parent = {'status': 'partial', 'truncated': False, 'nodes': [{'id': 'a'}],
              'requested_scope': {'complete': False}}
    result = asyncio.run(adapter.execute(child, {'selected': parent}, emit))
    assert result['status'] == 'partial'
    assert result['bounded_dependency_step_ids'] == ['selected']
    assert not result['evidence_coverage']['query_scope']['complete_for_requested_scope']
