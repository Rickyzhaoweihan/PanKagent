import copy

from pankagent_vnext.anatomy_paths import REGISTRY, ROLES
from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.graph_contract import generation_request

FAILURE = 'invalid_anatomical_endpoint_role:HAS_CELL_TYPE_requires_cell_type_to_tissue'


def plan(value='UBERON_0001264'):
    return {'id': 's1', 'question': 'Find pancreatic cell types and their marker annotations',
            'relation_types': ['HAS_CELL_TYPE'], 'complete': True,
            'constraints': [{'entity_type': 'anatomical_structure', 'property': 'id', 'operator': '=', 'value': value}]}


def test_same_label_reversed_tissue_anchor_is_rejected():
    valid = "MATCH (c:anatomical_structure)-[r:HAS_CELL_TYPE]->(t:anatomical_structure {id:'UBERON_0001264'}) RETURN c,r,t"
    assert validate_cypher(valid, plan()) == []
    assert FAILURE in validate_cypher(
        "MATCH (t:anatomical_structure {id:'UBERON_0001264'})-[r:HAS_CELL_TYPE]->(c:anatomical_structure) RETURN c,r,t", plan())


def test_reversed_pattern_syntax_and_undirected_remain_valid():
    for pattern in ['<-[r:HAS_CELL_TYPE]-', '-[r:HAS_CELL_TYPE]-']:
        query = "MATCH (t:anatomical_structure {id:'UBERON_0001264'})" + pattern + '(c:anatomical_structure) RETURN c,r,t'
        assert validate_cypher(query, plan()) == []


def test_alias_and_parameter_do_not_hide_wrong_direction():
    query = 'MATCH (t:anatomical_structure) WHERE t.id=$tissue WITH t AS organ MATCH (organ)-[r:HAS_CELL_TYPE]->(c:anatomical_structure) RETURN organ,r,c'
    assert FAILURE in validate_cypher(query, plan(), {'tissue': 'UBERON_0001264'})
    assert FAILURE not in validate_cypher(query.replace('(organ)-[r:HAS_CELL_TYPE]->', '(organ)<-[r:HAS_CELL_TYPE]-'), plan(), {'tissue': 'UBERON_0001264'})


def test_resolved_name_predicate_uses_canonical_role():
    step = plan()
    step['constraints'][0].update(property='name', value='pancreas')
    step['resolved_entities'] = [{'constraint_index': 0, 'state': 'resolved', 'entity_type': 'anatomical_structure', 'id': 'UBERON_0001264', 'name': 'pancreas'}]
    query = "MATCH (t:anatomical_structure {name:'pancreas'})-[r:HAS_CELL_TYPE]->(c:anatomical_structure) RETURN t,r,c"
    assert FAILURE in validate_cypher(query, step)


def test_union_branches_are_checked_independently():
    good = "MATCH (c:anatomical_structure)-[r:HAS_CELL_TYPE]->(t:anatomical_structure {id:'UBERON_0001264'}) RETURN c,r,t"
    bad = "MATCH (c:anatomical_structure)<-[r:HAS_CELL_TYPE]-(t:anatomical_structure {id:'UBERON_0001264'}) RETURN c,r,t"
    assert validate_cypher(good + ' UNION ALL ' + good, plan()) == []
    assert FAILURE in validate_cypher(good + ' UNION ALL ' + bad, plan())


def test_optional_edge_is_validated_without_becoming_mandatory():
    query = "MATCH (t:anatomical_structure {id:'UBERON_0001264'}) OPTIONAL MATCH (t)-[r:HAS_CELL_TYPE]->(c:anatomical_structure) RETURN t,r,c"
    step = plan(); step['relation_types'] = []
    assert FAILURE in validate_cypher(query, step)


def test_recorded_tissue_with_cl_prefix_is_not_assumed_cell_type():
    tissue_id = next(k for k,v in ROLES.items() if k.startswith('CL_') and v == 'tissue')
    query = "MATCH (t:anatomical_structure {id:'" + tissue_id + "'})-[r:HAS_CELL_TYPE]->(c:anatomical_structure) RETURN t,r,c"
    assert FAILURE in validate_cypher(query, plan(tissue_id))
    assert REGISTRY['node_count'] == len(ROLES) == 58


def test_known_cell_as_target_and_explicit_categories_rejected():
    query = "MATCH (t:anatomical_structure)-[r:HAS_CELL_TYPE]->(c:anatomical_structure {id:'CL_0000169'}) RETURN t,r,c"
    assert FAILURE in validate_cypher(query, plan('CL_0000169'))
    step = plan(); step['constraints'] = [{'entity_type':'anatomical_structure','property':'category','operator':'=','value':'tissue'}]
    query = "MATCH (t:anatomical_structure)-[r:HAS_CELL_TYPE]->(c:anatomical_structure) WHERE t.category='tissue' RETURN t,r,c"
    assert FAILURE in validate_cypher(query, step)


def test_no_query_rewrite_or_constraint_changes_and_guidance_is_clear():
    step = plan(); saved = copy.deepcopy(step)
    query = "MATCH (t:anatomical_structure {id:'UBERON_0001264'})-[r:HAS_CELL_TYPE]->(c:anatomical_structure) RETURN t,r,c"
    validate_cypher(query, step)
    assert step == saved
    guidance = generation_request(step, step['question'])
    assert '(cell)-[:HAS_CELL_TYPE]->(tissue)' in guidance
    assert 'never tissue -> cell' in guidance


def test_marker_cannot_attach_to_enclosing_tissue_instead_of_cell():
    step = plan(); step['relation_types'].append('MARKER_GENE_OF')
    bad = "MATCH (gene:Gene)-[marker:MARKER_GENE_OF]->(tissue:anatomical_structure) MATCH (cell:anatomical_structure)-[hierarchy:HAS_CELL_TYPE]->(tissue) WHERE tissue.id='UBERON_0001264' RETURN gene,marker,cell,hierarchy,tissue"
    assert 'invalid_anatomical_endpoint_role:MARKER_GENE_OF_requires_Gene_to_cell_type' in validate_cypher(bad, step)
    good = "MATCH (gene:Gene)-[marker:MARKER_GENE_OF]->(cell:anatomical_structure)-[hierarchy:HAS_CELL_TYPE]->(tissue:anatomical_structure) WHERE tissue.id='UBERON_0001264' RETURN gene,marker,cell,hierarchy,tissue"
    assert validate_cypher(good, step) == []


def test_other_role_rules_use_recorded_categories_not_prefixes():
    spec = plan('CL_0000169'); spec['relation_types'] = ['PART_OF']
    query = "MATCH (c:anatomical_structure {id:'CL_0000169'})-[r:PART_OF]->(t:anatomical_structure) RETURN c,r,t"
    assert any(e.startswith('invalid_anatomical_endpoint_role:PART_OF') for e in validate_cypher(query, spec))
    tissue_id = next(k for k,v in ROLES.items() if k.startswith('CL_') and v == 'tissue')
    spec = plan(tissue_id); spec['relation_types'] = ['SUBCLASS_OF']
    query = "MATCH (c:anatomical_structure {id:'" + tissue_id + "'})-[r:SUBCLASS_OF]->(parent:anatomical_structure) RETURN c,r,parent"
    assert validate_cypher(query, spec) == []


def test_category_rules_have_full_inventory_counts():
    assert {k:v['complete_inventory_edges'] for k,v in REGISTRY['endpoint_roles'].items()} == {
        'HAS_CELL_TYPE':8, 'MARKER_GENE_OF':124, 'PART_OF':19, 'SUBCLASS_OF':23, 'HAS_STATE':4}
