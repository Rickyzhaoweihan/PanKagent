"""Typed filters use full-release mandatory endpoint implications, not IDs."""
import hashlib
import json
from pathlib import Path
import pytest
from pankagent_vnext.endpoint_types import REGISTRY
from pankagent_vnext.graph import _pattern_bindings, tokenize, validate_cypher

QUERY = 'MATCH (a)-[r:FGSEA_ENRICHED_IN]->(b) WHERE b.id=$cell RETURN a,r,b'
PARAMS = {'cell': 'CL_0000169'}

def step():
    return {'id':'s1','question':'Show beta-cell fGSEA evidence','graph_version':REGISTRY['release'],
        'relation_types':['FGSEA_ENRICHED_IN'],'complete':True,
        'constraints':[{'entity_type':'anatomical_structure','property':'id','operator':'=','value':PARAMS['cell']}]}

def bindings(query):
    return _pattern_bindings(tokenize(query), graph_release=REGISTRY['release'])[0]

def test_full_path_projection_and_ambiguous_source():
    assert len(REGISTRY['relations']['FGSEA_ENRICHED_IN']) == 2
    assert bindings(QUERY) == {'a':set(),'b':{'anatomical_structure'}}
    path=Path(__file__).parents[1]/'pankagent_vnext'/'release_schema.json'
    if path.exists():
        raw=path.read_bytes();full=json.loads(raw)
        assert hashlib.sha256(raw).hexdigest()==REGISTRY['source_registry_sha256']
        assert REGISTRY['relations']=={kind:[{'source':p['source'],'target':p['target']} for p in spec['paths']] for kind,spec in full['relations'].items()}

@pytest.mark.parametrize('query',[
    QUERY,
    QUERY.replace('WHERE b.id=$cell',"WHERE b.id='CL_0000169'"),
    QUERY.replace('(b)',"(b {id:'CL_0000169'})").replace(' WHERE b.id=$cell',''),
    'MATCH (b)<-[r:FGSEA_ENRICHED_IN]-(a) WHERE b.id=$cell RETURN a,r,b',
])
def test_unique_target_preserves_typed_filter(query):
    before=query
    assert bindings(query)['b']=={'anatomical_structure'}
    assert validate_cypher(query,step(),PARAMS)==[]
    assert query==before

@pytest.mark.parametrize('query',[
    QUERY.replace('->','-'),
    QUERY.replace('FGSEA_ENRICHED_IN','FGSEA_ENRICHED_IN|HAS_DONOR'),
    QUERY.replace('FGSEA_ENRICHED_IN','FGSEA_ENRICHED_IN|UNREGISTERED_RELATION'),
    QUERY.replace('r:FGSEA_ENRICHED_IN','r:FGSEA_ENRICHED_IN*1..2'),
    QUERY.replace('WHERE b.id','WHERE a.id'),
])
def test_ambiguous_unsupported_or_wrong_end_rejected(query):
    assert 'missing_required_filter:id' in validate_cypher(query,step(),PARAMS)

def test_explicit_wrong_target_and_source_never_coerced():
    query=QUERY.replace('(b)','(b:Gene)')
    assert bindings(query)['b']=={'Gene'}
    assert 'missing_required_filter:id' in validate_cypher(query,step(),PARAMS)
    query=QUERY.replace('(a)','(a:Gene)')
    assert bindings(query)=={'a':{'Gene'},'b':set()}
    assert 'missing_required_filter:id' in validate_cypher(query,step(),PARAMS)

@pytest.mark.parametrize('label',['kegg','reactome','ontology:kegg'])
def test_explicit_collection_label_preserved(label):
    query=QUERY.replace('(a)',f'(a:{label})')
    assert bindings(query)['a']==set(label.split(':'))
    assert validate_cypher(query,step(),PARAMS)==[]

def test_node_alias_inherits_but_scalar_alias_does_not():
    query='MATCH (a)-[r:FGSEA_ENRICHED_IN]->(b) WITH a,r,b AS cell WHERE cell.id=$cell RETURN a,r,cell'
    assert bindings(query)['cell']=={'anatomical_structure'}
    assert validate_cypher(query,step(),PARAMS)==[]
    for projection in ['b.name AS cell','b.name AS b','1 AS b']:
        name=projection.split()[-1]
        bad=f'MATCH (a)-[r:FGSEA_ENRICHED_IN]->(b) WITH {projection} WHERE {name}.id=$cell RETURN {name}'
        assert 'missing_required_filter:id' in validate_cypher(bad,step(),PARAMS)

def test_union_branches_are_independently_typed():
    assert validate_cypher(QUERY+' UNION ALL '+QUERY,step(),PARAMS)==[]
    for other in [QUERY.replace('(b)','(b:Gene)'),QUERY.replace(' WHERE b.id=$cell',''),QUERY.replace('->','-')]:
        for joined in [QUERY+' UNION ALL '+other,other+' UNION '+QUERY]:
            assert 'missing_required_filter:id' in validate_cypher(joined,step(),PARAMS)

def test_optional_relationship_or_predicate_cannot_satisfy_primary_filter():
    query='MATCH (a:kegg),(b) OPTIONAL MATCH (a)-[r:FGSEA_ENRICHED_IN]->(b) WHERE b.id=$cell RETURN a,r,b'
    assert bindings(query)['b']==set()
    assert 'missing_required_filter:id' in validate_cypher(query,step(),PARAMS)
    query='MATCH (a)-[r:FGSEA_ENRICHED_IN]->(b) OPTIONAL MATCH (b)-[s:HAS_STATE]->(state) WHERE b.id=$cell RETURN a,r,b'
    assert bindings(query)['b']=={'anatomical_structure'}
    assert 'missing_required_filter:id' in validate_cypher(query,step(),PARAMS)

def test_other_release_does_not_infer():
    for release in [None,'','other-release']:
        supplied=step();supplied['graph_version']=release
        assert _pattern_bindings(tokenize(QUERY),graph_release=release)[0]['b']==set()
        assert 'missing_required_filter:id' in validate_cypher(QUERY,supplied,PARAMS)

def test_common_parent_not_a_specific_collection():
    supplied=step();supplied['constraints']=[{'entity_type':'kegg','property':'id','operator':'=','value':'pathway-id'}]
    query="MATCH (a)-[r:FGSEA_ENRICHED_IN]->(b) WHERE a.id='pathway-id' RETURN a,r,b"
    assert 'missing_required_filter:id' in validate_cypher(query,supplied)

def test_ontology_id_literal_alone_cannot_infer_a_node_type():
    query='MATCH (b) WHERE b.id=$cell RETURN b'
    assert bindings(query)['b']==set()
    assert 'missing_required_filter:id' in validate_cypher(query,step(),PARAMS)
