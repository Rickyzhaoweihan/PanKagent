import pytest
from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.release_schema import normalize_constraints, REGISTRY
from pankagent_vnext.investigations import group_plan, coverage
from pankagent_vnext.semantic_registry import resolve


def step(**kw):
    return {'id':'s1','question':'QTL for GCLC in pancreas','graph_version':REGISTRY['release'],'constraints':[], 'complete':True,**kw}

@pytest.mark.parametrize('bad',[
 'MATCH (g:Gene)-[r:PART_OF_QTL_SIGNAL]->(v:variants) RETURN g,r,v',
 "MATCH (v:variants)-[r:PART_OF_QTL_SIGNAL]->(g:Gene) WHERE g.tissue_name='Pancreas' RETURN v,r,g",
 'MATCH (g:Gene)-[r:PART_OF_GWAS_SIGNAL]->(d:disease) RETURN g,r,d',
 'MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease) RETURN r.pip',
 'MATCH (g:Gene) OPTIONAL MATCH (g)-[r:PART_OF_QTL_SIGNAL]->(d:disease) RETURN g,r,d',
 'MATCH (v:variants)-[r:PART_OF_QTL_SIGNAL]->(g:Gene) WITH r AS q RETURN q.tissue_typo',
])
def test_wrong_direction_owner_and_alias_rejected(bad):
    assert validate_cypher(bad,step())


def test_correct_relation_owner_and_categories():
    s=normalize_constraints(step(relation_types=['PART_OF_QTL_SIGNAL'],constraints=[{'property':'tissue_name','operator':'=','value':'pancreas','entity_type':None}]))
    assert s['constraints'][0]['value']=='Pancreas'
    good="MATCH (v:variants)-[r:PART_OF_QTL_SIGNAL]->(g:Gene) WHERE r.tissue_name='Pancreas' RETURN v,r,g"
    assert validate_cypher(good,s)==[]
    assert validate_cypher(good.replace('r.tissue_name','g.tissue_name'),s)
    assert validate_cypher(good+' UNION '+good.replace('Pancreas','Liver'),s)


def test_alternative_edge_types_are_not_mandatory_joins():
    q='MATCH (g:Gene)-[r:GENE_DETECTED_IN|GENE_ENRICHED_IN]->(c:anatomical_structure) RETURN g,r,c'
    assert validate_cypher(q,step())==[]


def test_casing_and_functional_assay_explanation():
    s=step(question='Find HPAP pancreatic islet perifusion donors',relation_types=['HAS_SAMPLE'],constraints=[{'property':'data_modality','entity_type':'Sample_node','operator':'=','value':'perifusion'},{'property':'name','entity_type':'anatomical_structure','operator':'=','value':'pancreatic islet'}])
    s=normalize_constraints(s)
    assert s['constraints'][1]['value']=='pancreatic islet (islet of Langerhans)'
    p=resolve(s,{'sources':['HPAP'],'modalities':['Perifusion']},REGISTRY['release'])
    assert p['sample_requirements']['modality_groups']==[['Perifusion']]
    assert not p['semantic_issues']
    assert 'RNA' not in p['semantic_summary']


def test_three_groups_twelve_checks_and_explicit_coverage():
    kinds=['GENE_DETECTED_IN','GENE_ENRICHED_IN','MARKER_GENE_OF','T1D_DEG_IN','EFFECTOR_GENE_OF','PART_OF_QTL_SIGNAL','SIGNAL_COLOC_WITH','ASSOCIATED_WITH_GO','FUNCTION_ANNOTATION','FGSEA_ENRICHED_IN','PHYSICAL_INTERACTION','GENETIC_INTERACTION']
    p=group_plan({'steps':[step(id=str(i),relation_types=[k]) for i,k in enumerate(kinds)]})
    assert len(p['display_groups'])==3 and len(p['steps'])==12
    assert sum(len(g['step_ids']) for g in p['display_groups'])==12
    assert coverage(p,{'0':{'status':'empty'}})[0]['status']=='empty'
    assert coverage(p,{})[-1]['status']=='awaiting_confirmation'
    assert group_plan({'steps':p['steps']+[step(id='extra')]})['clarification']


def test_verified_direction_allows_undirected_partner_lookup():
    assert validate_cypher('MATCH (a:Gene)-[r:PHYSICAL_INTERACTION]-(b:Gene) RETURN a,r,b',step())==[]


def test_unsupported_variable_length_path_is_rejected():
    assert validate_cypher('MATCH (a:Gene)-[r:PHYSICAL_INTERACTION*1..3]->(b:Gene) RETURN a,r,b',step())


def test_schema_case_normalization_preserves_values_and_unknowns():
    from pankagent_vnext.release_schema import canonicalize_symbols
    query="MATCH (g:Gene)-[r:signal_coloc_with]->(d:Disease) WHERE d.name='Disease' RETURN d, {x:'Disease'}"
    result, changes=canonicalize_symbols(query)
    assert '(d:`disease`)' in result and '`SIGNAL_COLOC_WITH`' in result
    assert "d.name='Disease'" in result and "{x:'Disease'}" in result
    assert len(changes)==2
    assert canonicalize_symbols('MATCH (d:Disease_ontology) RETURN d')[0]=='MATCH (d:Disease_ontology) RETURN d'


def test_opposite_mandatory_edges_do_not_count_as_undirected_union():
    q="MATCH (a:Gene)-[r:PHYSICAL_INTERACTION]->(b:Gene) WHERE a.name='GLIS3' MATCH (b)-[s:PHYSICAL_INTERACTION]->(a) WHERE b.name='GLIS3' RETURN a,r,b,s"
    s=step(constraints=[{'property':'name','entity_type':'Gene','operator':'=','value':'GLIS3'}])
    errors=validate_cypher(q,s)
    assert 'interaction_partner_scope_requires_undirected_match' in errors
    assert 'overspecified_interaction_anchor' in errors


def test_shared_pathway_properties_on_unlabelled_endpoint_and_alias():
    q="MATCH (g:Gene)-[r:FUNCTION_ANNOTATION]->(p) WITH g,r,p AS pathway RETURN g,r,pathway.id"
    assert validate_cypher(q,step()) == []
    assert validate_cypher(q.replace('pathway.id','pathway.go_domain'),step())


def test_recorded_stage_is_not_diagnosed_diabetes_filter():
    from pankagent_vnext.semantic_registry import STAGES
    vocab={'stages':list(STAGES.values()),'sources':['HPAP']}
    p=resolve(step(question='Find HPAP stage-1 T1D donors',constraints=[{'property':'id','entity_type':'disease','operator':'=','value':'MONDO_0005147'}]),vocab,REGISTRY['release'])
    assert not any(c.get('entity_type')=='disease' for c in p['constraints'])
    assert any(c['property']=='t1d_stage' and c['value']==STAGES['1'] for c in p['constraints'])
    p=resolve(step(question='Find HPAP stage-1 donors with diagnosed type 1 diabetes'),vocab,REGISTRY['release'])
    assert any(c.get('entity_type')=='disease' for c in p['constraints'])


def test_requested_category_coverage_prevents_partial_comprehensive_plan():
    from pankagent_vnext.investigations import category_issue, required_categories
    plan={'steps':[step(relation_types=['GENE_DETECTED_IN'])]}
    assert 'missing_requested_categories' in category_issue('Give me a comprehensive profile of GLIS3',plan)
    assert category_issue('Where is GLIS3 detected?',plan) is None
    assert len(required_categories('Give me comprehensive gene insights for GLIS3'))==12
    assert required_categories('Use CFTR instead, keeping the previous categories')==set()


def test_stage_generator_wording_preserves_original_and_canonical_filter():
    from pankagent_vnext.graph_contract import generation_request
    from pankagent_vnext.semantic_registry import STAGES
    p=resolve(step(question='Find HPAP stage-1 T1D donors',relation_types=['HAS_DONOR']),{'stages':list(STAGES.values()),'sources':['HPAP']},REGISTRY['release'])
    q=generation_request(p,p['question'])
    assert p['question']=='Find HPAP stage-1 T1D donors'
    assert 'stage-1 donor-stage metadata donors' in q
    assert STAGES['1'] in q


def test_retrieval_metadata_is_not_display_or_model_context():
    from pankagent_vnext.app import aggregate_evidence
    result=aggregate_evidence({'s1':{'status':'partial','nodes':[],'edges':[],'truncated':True}})
    assert result['retrieval']['truncated'] is True
    assert result['retrieval']['checks']==1
    assert 'display_counts' not in result['retrieval']


def test_registered_profile_keeps_twelve_checks_and_dependency():
    from pankagent_vnext.investigations import generic_profile_gene, expand_registered_profile
    q='Give me a comprehensive gene profile of GLIS3.'
    assert generic_profile_gene(q)=='GLIS3'
    assert generic_profile_gene(q+' Only pancreas.') is None
    p=group_plan(expand_registered_profile(q,'GLIS3'))
    assert len(p['steps'])==12 and len(p['display_groups'])==3
    assert p['steps'][9]['depends_on']==['s9']
    assert p['steps'][9]['constraints']==[]
    assert all(s['constraints'][0]['value']=='GLIS3' for i,s in enumerate(p['steps']) if i!=9)


def test_opposite_directed_patterns_cannot_disguise_wrong_qtl_direction():
    q='MATCH (g:Gene)-[r:PART_OF_QTL_SIGNAL]->(v:variants) MATCH (v)-[s:PART_OF_QTL_SIGNAL]->(g) RETURN g,r,v,s'
    assert 'invalid_relationship_endpoints:PART_OF_QTL_SIGNAL' in validate_cypher(q,step())
    assert validate_cypher('MATCH (g:Gene)-[r:PART_OF_QTL_SIGNAL]-(v:variants) RETURN g,r,v',step())==[]
