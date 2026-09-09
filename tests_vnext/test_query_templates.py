from copy import deepcopy
import pytest
from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.query_templates import compile_query
from pankagent_vnext.release_schema import REGISTRY

RELEASE = REGISTRY['release']


def step(kind='GENE_ENRICHED_IN', constraints=None, **kw):
    if constraints is None:
        constraints = [{'entity_type': 'Gene', 'property': 'name', 'operator': '=', 'value': 'CFTR'}]
    value = {'id': 's1', 'question': 'Retrieve matching evidence', 'graph_version': RELEASE,
             'complete': True, 'relation_types': [kind], 'constraints': deepcopy(constraints),
             'resolved_entities': []}
    for index, c in enumerate(constraints):
        if c.get('entity_type') == 'Gene' and c.get('property') in {'id', 'name'} and c.get('operator', '=') == '=':
            value['resolved_entities'].append({'constraint_index': index, 'requested': deepcopy(c),
                'state': 'resolved', 'graph_version': RELEASE, 'entity_type': 'Gene', 'labels': ['Gene'],
                'name': c['value'], 'id': 'ENSG00000001626'})
    value.update(kw)
    return value


def test_verified_gene_resolution_produces_complete_parameterized_query():
    s = step()
    original = deepcopy(s)
    result = compile_query(s)
    assert result['parameters'] == {'template_0': 'ENSG00000001626'}
    assert 'a.`id` = $template_0' in result['cypher'] and 'LIMIT' not in result['cypher']
    assert validate_cypher(result['cypher'], s, result['parameters']) == []
    assert s == original


@pytest.mark.parametrize('kind,target', [('PART_OF_QTL_SIGNAL', 'Gene'), ('PART_OF_GWAS_SIGNAL', 'disease')])
def test_variant_subtypes_share_one_complete_parent_path(kind, target):
    c = {'entity_type': 'variants', 'property': 'id', 'operator': '=', 'value': 'rs13393590'}
    s = step(kind, [c], resolved_entities=[{'constraint_index': 0, 'requested': deepcopy(c),
        'state': 'resolved', 'graph_version': RELEASE, 'entity_type': 'variants',
        'labels': ['variants', 'sequence_variant', 'snv'], 'id': 'rs13393590', 'name': 'rs13393590'}])
    result = compile_query(s)
    assert result['endpoint_coverage']['registered_path_count'] > 1
    assert result['endpoint_coverage']['all_paths_covered']
    assert result['cypher'].startswith(f'MATCH (a:`variants`)-[r:`{kind}`]->(b:`{target}`)')
    assert validate_cypher(result['cypher'], s, result['parameters']) == []


@pytest.mark.parametrize('kind', ['FUNCTION_ANNOTATION', 'FGSEA_ENRICHED_IN', 'HAS_SAMPLE', 'PHYSICAL_INTERACTION', 'HAS_CELL_TYPE'])
def test_distinct_scopes_and_same_type_roles_fall_through_to_gpu(kind):
    assert compile_query(step(kind)) is None


def test_coloc_template_keeps_primary_records_independent():
    s = step('SIGNAL_COLOC_WITH')
    result = compile_query(s)
    assert '(a:`Gene`)-[r:`SIGNAL_COLOC_WITH`]->(b:`disease`)' in result['cypher']
    assert 'PART_OF_QTL_SIGNAL' not in result['cypher']
    assert validate_cypher(result['cypher'], s, result['parameters']) == []


def test_relationship_owner_and_numeric_boundary_are_preserved():
    s = step()
    s['constraints'].extend([
        {'entity_type': None, 'owner_kind': 'relationship', 'relationship_type': 'GENE_ENRICHED_IN', 'property': 'data_source', 'operator': '=', 'value': 'source'},
        {'entity_type': None, 'property': 'rank_in_cell_type', 'operator': '>', 'value': '9007199254740993'},
        {'entity_type': None, 'property': 'padj', 'operator': '<', 'value': '1e-12'},
    ])
    result = compile_query(s)
    assert 'r.`data_source` = $template_1' in result['cypher']
    assert result['parameters']['template_2'] == 9007199254740993
    assert isinstance(result['parameters']['template_2'], int)
    assert result['parameters']['template_3'] == 1e-12


@pytest.mark.parametrize('constraint', [
    {'entity_type': None, 'property': 'data_source', 'operator': '=', 'value': 'HPAP'},
    {'entity_type': 'disease', 'property': 't1d_stage', 'operator': '=', 'value': 'Stage 3'},
    {'entity_type': 'Gene', 'owner_kind': 'relationship', 'property': 'name', 'operator': '=', 'value': 'CFTR'},
    {'entity_type': None, 'owner_kind': 'node', 'property': 'padj', 'operator': '<', 'value': '.05'},
    {'entity_type': None, 'relationship_type': 'PART_OF_QTL_SIGNAL', 'property': 'padj', 'operator': '<', 'value': '.05'},
])
def test_ambiguous_conflicting_and_wrong_owners_are_not_assigned(constraint):
    s = step()
    s['constraints'].append(constraint)
    assert compile_query(s) is None


@pytest.mark.parametrize('value', ['NaN', 'Infinity', float('nan'), float('inf'), True, {}, ['1']])
def test_invalid_numeric_values_are_not_silently_cast(value):
    s = step()
    s['constraints'].append({'entity_type': None, 'property': 'padj', 'operator': '<', 'value': value})
    assert compile_query(s) is None


def test_lists_preserve_each_constraint_member_and_are_not_mutated():
    s = step()
    s['constraints'].append({'entity_type': 'anatomical_structure', 'property': 'id', 'operator': 'IN', 'value': '["CL_0002079","CL_0002079_MUC5B"]'})
    result = compile_query(s)
    assert result['parameters']['template_1'] == ['CL_0002079', 'CL_0002079_MUC5B']
    assert 'b.`id` IN $template_1' in result['cypher']
    assert isinstance(s['constraints'][1]['value'], str)


@pytest.mark.parametrize('change', [
    {'graph_version': 'old-release'}, {'state': 'ambiguous'}, {'labels': ['disease']},
    {'requested': {'entity_type': 'Gene', 'property': 'name', 'value': 'ADYC3'}}, {'id': ''},
])
def test_stale_or_wrong_resolution_cannot_replace_a_constraint(change):
    s = step()
    s['resolved_entities'][0].update(change)
    assert compile_query(s) is None


def test_duplicate_resolution_is_not_arbitrarily_selected():
    s = step()
    s['resolved_entities'].append(deepcopy(s['resolved_entities'][0]))
    assert compile_query(s) is None


@pytest.mark.parametrize('kw', [{'graph_version': 'other'}, {'complete': False}, {'depends_on': ['s0']}, {'ranking': {'limit': 3}}, {'semantic_issues': ['unknown stage']}])
def test_ineligible_steps_keep_generator_route(kw):
    assert compile_query(step(**kw)) is None


def test_values_are_parameters_not_cypher_source():
    s = step()
    raw = "ND' RETURN 1 //"
    s['constraints'].append({'entity_type': None, 'property': 'condition', 'operator': '=', 'value': raw})
    result = compile_query(s)
    assert raw not in result['cypher'] and result['parameters']['template_1'] == raw


def test_numeric_equality_does_not_compare_number_to_string():
    s = step('PART_OF_QTL_SIGNAL')
    s['constraints'].append({'entity_type': None, 'property': 'pip', 'operator': '=', 'value': '0.2'})
    result = compile_query(s)
    assert result['parameters']['template_1'] == 0.2
    assert isinstance(result['parameters']['template_1'], float)


def test_numeric_in_values_are_typed_without_dropping_members():
    s = step()
    s['constraints'].append({'entity_type': None, 'property': 'rank_in_cell_type', 'operator': 'IN', 'value': '["1","2"]'})
    result = compile_query(s)
    assert result['parameters']['template_1'] == [1,2]


def test_unverified_numeric_storage_retains_gpu_route():
    s = step()
    s['constraints'].append({'entity_type': 'Gene', 'property': 'chr', 'operator': '>', 'value': '2'})
    assert compile_query(s) is None


def test_numeric_looking_identifiers_stay_strings():
    s = step()
    s['constraints'].append({'entity_type': 'Gene', 'property': 'alt_id_entrez', 'operator': '=', 'value': '12345'})
    assert compile_query(s)['parameters']['template_1'] == '12345'


@pytest.mark.parametrize('operator', ['!=', '<>'])
def test_scalar_inequality_keeps_typed_owner_value_and_operator(operator):
    s = step()
    s['constraints'].append({'entity_type': 'anatomical_structure', 'property': 'id', 'operator': operator, 'value': 'CL_0000171'})
    result = compile_query(s)
    assert result and 'b.`id` <> $template_1' in result['cypher']
    assert result['parameters']['template_1'] == 'CL_0000171'
    assert validate_cypher(result['cypher'], s, result['parameters']) == []
    assert validate_cypher(result['cypher'].replace('b.`id` <>', 'b.`id` ='), s, result['parameters'])
    wrong = deepcopy(s)
    wrong['constraints'][-1]['entity_type'] = 'Gene'
    assert validate_cypher(result['cypher'], wrong, result['parameters'])


def test_scalar_inequality_numeric_values_remain_numbers_not_strings():
    s = step()
    s['constraints'].append({'owner_kind': 'relationship', 'relationship_type': 'GENE_ENRICHED_IN', 'property': 'padj', 'operator': '!=', 'value': '0.0'})
    result = compile_query(s)
    assert result['parameters']['template_1'] == 0.0
    assert isinstance(result['parameters']['template_1'], float)
    assert validate_cypher(result['cypher'], s, result['parameters']) == []


def test_general_not_and_not_in_remain_unsupported():
    s = step()
    s['constraints'].append({'entity_type': 'anatomical_structure', 'property': 'id', 'operator': 'NOT IN', 'value': '["CL_0000171"]'})
    assert compile_query(s) is None
    assert validate_cypher('MATCH (a:Gene)-[r:GENE_ENRICHED_IN]->(b:anatomical_structure) WHERE a.name="CFTR" AND NOT b.id IN ["CL_0000171"] RETURN r', s)
