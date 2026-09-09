import pytest
from pankagent_vnext.graph import tokenize
from pankagent_vnext.scientific_projection import validation_errors,projection_contract

MATCH='MATCH (g:Gene)-[r:GENE_ENRICHED_IN]->(c:anatomical_structure)'
SPEC={'graph_version':'PanKgraph_08_04','relation_types':['GENE_ENRICHED_IN'],'constraints':[]}


def test_executed_collect_query_with_final_semicolon_retains_coverage():
    query=('MATCH (g:Gene)-[r:GENE_ENRICHED_IN]->(c:anatomical_structure) '
           'WHERE g.id = $gene '
           'WITH collect(DISTINCT g) + collect(DISTINCT c) AS nodes, '
           'collect(DISTINCT r) AS edges RETURN nodes, edges;')
    result=projection_contract(tokenize(query),SPEC,{'gene':'ENSG00000001626'})
    assert result['representation']=='full_records'
    assert result['record_membership_enumerated']
    assert not result['missing_relations']
    repeated=projection_contract(tokenize(query+';'),SPEC,{'gene':'ENSG00000001626'})
    assert repeated['representation']=='unverified_projection'
    assert repeated['missing_relations']==['GENE_ENRICHED_IN']

@pytest.mark.parametrize('projection',[
    'RETURN g,r,c',
    'RETURN collect(DISTINCT r) AS edges,collect(g)+collect(c) AS nodes',
    'RETURN {nodes:collect(g)+collect(c),edges:collect(r)} AS graph',
    'WITH collect(g)+collect(c) AS nodes,collect(r) AS edges RETURN {nodes:nodes,edges:edges}',
    'WITH r AS measured,g,c RETURN g,measured,c',
    'RETURN [r],[g,c]',
])
def test_lossless_relationship_records_and_collections(projection):
    assert validation_errors(tokenize(MATCH+' '+projection),SPEC,{})==[]
    assert projection_contract(tokenize(MATCH+' '+projection),SPEC)['record_membership_enumerated']

@pytest.mark.parametrize('projection',[
    'RETURN g', 'RETURN {nodes:collect(g)+collect(c)}',
    'WITH g AS focal RETURN focal',
    'RETURN head(collect(r))','WITH collect(r) AS edges RETURN head(edges)',
    'RETURN collect(r)[..1]', 'RETURN r.log2_fold_change',
    'WITH collect(r) AS records RETURN count(records)',
    'WITH count(r) AS records RETURN count(records)',
    'WITH collect(r) AS records RETURN count(*)',
    'RETURN count(r)+count(r)',
    'RETURN r', 'RETURN collect(DISTINCT r) AS edges',
    'WITH r AS measured RETURN measured','RETURN [r]',
    'RETURN r,g.id,c.id',
    'RETURN g.id,c.id,r.data_source',
    'RETURN g.id,c.id,r.condition',
    'RETURN g.id,c.id,r.expression_call',
    'RETURN avg(r.source_row)',
])
def test_lost_measurements_or_unidentified_statistics_rejected(projection):
    errors=validation_errors(tokenize(MATCH+' '+projection),SPEC,{})
    assert errors and errors[0].startswith('scientific_projection_missing:GENE_ENRICHED_IN:')

@pytest.mark.parametrize('projection',[
    'RETURN count(r) AS total','RETURN count(*) AS total',
    'RETURN count(DISTINCT c) AS cells',
    'RETURN count(DISTINCT c.id) AS cells',
    'WITH r AS evidence RETURN count(evidence) AS total',
    'RETURN avg(r.log2_fold_change) AS mean_effect',
])
def test_aggregate_not_represented_as_full_individual_records(projection):
    result=projection_contract(tokenize(MATCH+' '+projection),SPEC)
    assert result['missing_relations']==[]
    assert result['representation']=='aggregate_result'
    assert not result['record_membership_enumerated']

@pytest.mark.parametrize('projection',[
    'RETURN g.id AS gene,c.id AS cell,r.log2_fold_change AS effect',
    'RETURN g,c,r.log2_fold_change AS effect',
    'RETURN {gene:g.id,cell:c.id,effect:r.log2_fold_change}',
])
def test_statistics_require_endpoint_identity(projection):
    result=projection_contract(tokenize(MATCH+' '+projection),SPEC)
    assert result['missing_relations']==[] and result['representation']=='identified_statistics'


def test_statistic_with_both_endpoints_bound_in_scope_is_valid():
    spec={**SPEC,'constraints':[{'entity_type':'Gene','property':'id','value':'g'},
                                {'entity_type':'anatomical_structure','property':'id','value':'c'}]}
    query=MATCH+' WHERE g.id="g" AND c.id="c" RETURN r.log2_fold_change AS effect'
    assert projection_contract(tokenize(query),spec)['representation']=='identified_statistics'

@pytest.mark.parametrize('query',[
    'MATCH p=(g:Gene)-[r:GENE_ENRICHED_IN]->(c:anatomical_structure) RETURN p',
    'MATCH p=(g:Gene)-[:GENE_ENRICHED_IN]->(c:anatomical_structure) RETURN p',
    'MATCH p=(g:Gene)-[:GENE_ENRICHED_IN]->(c:anatomical_structure) RETURN relationships(p),nodes(p)',
])
def test_full_path_including_anonymous_relationships(query):
    assert projection_contract(tokenize(query),SPEC)['representation']=='full_records'


def test_path_relationships_without_nodes_are_not_identifiable_records():
    query='MATCH p=(g:Gene)-[:GENE_ENRICHED_IN]->(c:anatomical_structure) RETURN relationships(p)'
    assert validation_errors(tokenize(query),SPEC,{})


def test_fixed_id_constraints_do_not_supply_node_objects_for_whole_edge_graph():
    spec={**SPEC,'constraints':[{'entity_type':'Gene','property':'id','value':'g'},
                                {'entity_type':'anatomical_structure','property':'id','value':'c'}]}
    query=MATCH+' WHERE g.id="g" AND c.id="c" RETURN r'
    assert validation_errors(tokenize(query),spec,{})


def test_coloc_source_metadata_cannot_replace_recorded_statistic():
    spec={**SPEC,'relation_types':['SIGNAL_COLOC_WITH']}
    match='MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease)'
    assert validation_errors(tokenize(match+' RETURN g.id,d.id,r.data_source'),spec,{})
    assert not validation_errors(tokenize(match+' RETURN g.id,d.id,r.pp_h4_abf'),spec,{})


def test_every_union_branch_must_keep_scientific_evidence():
    query=MATCH+' RETURN g,r,c UNION '+MATCH+' RETURN g'
    assert validation_errors(tokenize(query),SPEC,{})


def test_coloc_and_unrelated_donor_queries():
    spec={**SPEC,'relation_types':['SIGNAL_COLOC_WITH']}
    query='MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease)'
    assert validation_errors(tokenize(query+' RETURN g'),spec,{})
    assert not validation_errors(tokenize(query+' RETURN g,r,d'),spec,{})
    assert not validation_errors(tokenize('MATCH (d:donor) RETURN d'),{'relation_types':['HAS_DONOR']},{})


@pytest.mark.parametrize('relation,source,target', [
    ('SIGNAL_COLOC_WITH','Gene','disease'),
    ('PART_OF_GWAS_SIGNAL','variants','disease'),
    ('PART_OF_QTL_SIGNAL','variants','Gene'),
])
def test_normalized_signal_linkage_checks_require_records_not_just_counts(relation, source, target):
    spec = {**SPEC, 'relation_types': [relation], 'coloc_scope': {'role': 'primary'}}
    match = f'MATCH (a:{source})-[r:{relation}]->(b:{target})'
    assert validation_errors(tokenize(match+' RETURN count(r) AS total'), spec, {})
    assert validation_errors(tokenize(match+' RETURN a,b'), spec, {})
    assert validation_errors(tokenize(match+' RETURN a,r,b'), spec, {}) == []
    assert validation_errors(tokenize(match.replace('MATCH ', 'MATCH p=')+' RETURN p'), spec, {}) == []
    ordinary = {**spec}
    ordinary.pop('coloc_scope')
    assert validation_errors(tokenize(match+' RETURN count(r) AS total'), ordinary, {}) == []


@pytest.mark.parametrize('relation,target', [('PART_OF_GWAS_SIGNAL', 'disease'), ('PART_OF_QTL_SIGNAL', 'Gene')])
def test_native_separate_membership_query_gets_full_record_coverage_without_normalization(relation, target):
    spec = {'graph_version': 'PanKgraph_08_04', 'relation_types': [relation], 'constraints': []}
    query = f'MATCH (v:variants)-[r:{relation}]->(target:{target}) RETURN v,r,target'
    actual = projection_contract(tokenize(query), spec)
    assert actual['representation'] == 'full_records'
    assert actual['record_membership_enumerated'] is True
    assert actual['missing_relations'] == []
    assert validation_errors(tokenize(query), spec, {}) == []
    # Native count questions still work, but cannot be presented as enumerated
    # signal membership or used as exact variant-signal linkage evidence.
    count = query.replace('RETURN v,r,target', 'RETURN count(r) AS total')
    aggregate = projection_contract(tokenize(count), spec)
    assert aggregate['representation'] == 'aggregate_result'
    assert aggregate['record_membership_enumerated'] is False
    assert validation_errors(tokenize(count), spec, {}) == []
    # Existing ordinary-table validation remains compatible; missing complete
    # record identity prevents linkage proof even if a statistic is returned.
    scalar = query.replace('RETURN v,r,target', 'RETURN r.pip AS pip')
    assert projection_contract(tokenize(scalar), spec)['record_membership_enumerated'] is False
    assert validation_errors(tokenize(scalar), spec, {}) == []
    assert validation_errors(tokenize(scalar), {**spec, 'coloc_scope': {'role': 'qtl'}}, {})
