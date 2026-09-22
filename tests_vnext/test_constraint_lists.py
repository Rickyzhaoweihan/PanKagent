"""Regression for replay QTL/condition list encodings without network calls."""
from copy import deepcopy
import hashlib

import pytest

from pankagent_vnext.constraint_values import VALUE_SCHEMA, list_value
from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.planning_compile import compile_property_owners
from pankagent_vnext.planning_output import matches_schema
from pankagent_vnext.query_templates import compile_query as _compile_query


RELEASE = 'PanKgraph_08_04'
GROUNDING = {'status': 'ready', 'identity': {'graph_release': RELEASE}, 'mentions': []}
QTL_QUERY = ("MATCH (v:variants)-[r:PART_OF_QTL_SIGNAL]->(g:Gene) "
             "WHERE g.id='ENSG00000225190' AND r.tissue_name IN ['Pancreas','Islet'] RETURN v,r,g")


def _authorized(step):
    """Attach exact request proofs to a copy of a template-unit fixture."""
    prepared = deepcopy(step)
    question = 'Constraint-list template fixture: ' + '; '.join(
        f"{constraint.get('entity_type') or constraint.get('relationship_type') or 'node'}."
        f"{constraint.get('property')} {constraint.get('operator', '=')} "
        f"{constraint.get('value')}"
        for constraint in prepared.get('constraints') or [])
    prepared['semantic_request'] = {'source': 'user_request', 'question': question}
    digest = hashlib.sha256(question.encode()).hexdigest()
    prepared['request_filter_bindings'] = [{
        'constraint_index': index,
        'canonical_binding': deepcopy(constraint),
        'authorization_kind': 'verified_request_filter',
        'source': 'immutable_user_request',
        'request_sha256': digest,
        'graph_release': prepared.get('graph_version'),
    } for index, constraint in enumerate(prepared.get('constraints') or [])]
    return prepared


def compile_query(step):
    return _compile_query(_authorized(step))


def source(value, *, prop='tissue_name', relation='PART_OF_QTL_SIGNAL', operator='IN'):
    return {'steps': [{'id': 's1', 'question': 'Retrieve the requested recorded evidence.',
                       'complete': True, 'depends_on': [], 'relation_types': [relation],
                       'constraints': [
                           {'property': 'id', 'operator': '=', 'value': 'ENSG00000225190',
                            'entity_type': 'Gene'},
                           {'property': prop, 'operator': operator, 'value': value,
                            'entity_type': None, 'owner_kind': 'relationship',
                            'relationship_type': relation}]}]}


def compiled(raw):
    result, issue = compile_property_owners(raw, GROUNDING)
    assert issue is None
    step = result['steps'][0]
    step['graph_version'] = RELEASE
    # The query template's independent identity gate still requires a resolved
    # anchor; this fixture supplies the saved identity, never a live graph read.
    step['resolved_entities'] = [{'constraint_index': 0, 'requested': deepcopy(step['constraints'][0]),
                                  'state': 'resolved', 'graph_version': RELEASE, 'labels': ['Gene'],
                                  'entity_type': 'Gene', 'id': 'ENSG00000225190', 'name': 'PLEKHM1'}]
    return step


@pytest.mark.parametrize('value', [
    ['Pancreas', 'Islet'], '["Pancreas", "Islet"]', 'Pancreas,Islet',
])
def test_qtl_tissue_list_compiles_and_validates_without_losing_either_tissue(value):
    raw = source(value)
    original = deepcopy(raw)
    step = compiled(raw)
    tissue = step['constraints'][1]
    assert tissue['value'] == ['Pancreas', 'Islet']
    assert tissue['relationship_type'] == 'PART_OF_QTL_SIGNAL'
    assert tissue['owner_kind'] == 'relationship' and tissue['entity_type'] is None
    assert raw == original
    if value != tissue['value']:
        record = next(row for row in step['constraint_compilation'] if row['constraint_index'] == 1)
        assert record['requested']['value'] == value
        assert record['canonical_binding'] == tissue
    assert validate_cypher(QTL_QUERY, step) == []
    template = compile_query(step)
    assert template is not None
    assert template['parameters']['template_1'] == ['Pancreas', 'Islet']
    assert validate_cypher(template['cypher'], step, template['parameters']) == []
    # Compilation remains stable when a saved canonical plan is reconsidered.
    assert compiled({'steps': [step]})['constraints'] == step['constraints']


@pytest.mark.parametrize('query', [
    QTL_QUERY.replace("['Pancreas','Islet']", "['Pancreas']"),
    QTL_QUERY.replace("['Pancreas','Islet']", "['Pancreas','Islet','Liver']"),
    QTL_QUERY.replace(" AND r.tissue_name IN ['Pancreas','Islet']", ''),
    QTL_QUERY.replace('r.tissue_name', 'g.tissue_name'),
    QTL_QUERY.replace('RETURN v,r,g', "AND r.tissue_id='UBERON_0001264' RETURN v,r,g"),
    ("MATCH (v:variants)-[r:PART_OF_QTL_SIGNAL]->(g:Gene), "
     "(w:variants)-[other:PART_OF_GWAS_SIGNAL]->(d:disease) "
     "WHERE g.id='ENSG00000225190' AND other.tissue_name IN ['Pancreas','Islet'] RETURN v,r,g,w,other,d"),
])
def test_list_recovery_keeps_missing_extra_and_wrong_owner_filters_blocked(query):
    assert validate_cypher(query, compiled(source('Pancreas,Islet')))


@pytest.mark.parametrize('value', [
    ['NonDiabetic', 'Type1Diabetic'], '["NonDiabetic", "Type1Diabetic"]',
])
def test_explicit_condition_lists_use_the_same_template_and_validation_contract(value):
    step = compiled(source(value, prop='condition', relation='GENE_DETECTED_IN'))
    query = ("MATCH (g:Gene)-[r:GENE_DETECTED_IN]->(c:anatomical_structure) "
             "WHERE g.id='ENSG00000225190' AND r.condition IN ['NonDiabetic','Type1Diabetic'] RETURN g,r,c")
    assert step['constraints'][1]['value'] == ['NonDiabetic', 'Type1Diabetic']
    assert validate_cypher(query, step) == []
    template = compile_query(step)
    assert template is not None
    assert validate_cypher(template['cypher'], step, template['parameters']) == []
    assert validate_cypher(query.replace("['NonDiabetic','Type1Diabetic']", "['NonDiabetic']"), step)


@pytest.mark.parametrize('value', ['NonDiabetic,Type1Diabetic', 'NonDiabetic or Type1Diabetic'])
def test_unknown_condition_list_encoding_fails_early_without_guessing_categories(value):
    raw = source(value, prop='condition', relation='GENE_DETECTED_IN')
    original = deepcopy(raw)
    result, issue = compile_property_owners(raw, GROUNDING)
    assert issue == 'invalid_constraint_list:s1:condition:use_native_array'
    assert result['steps'][0]['constraints'][1]['value'] == value
    assert raw == original
    assert compile_query({**raw['steps'][0], 'graph_version': RELEASE}) is None


def test_legacy_comma_recovery_requires_exact_owner_release_and_recorded_members():
    variants = []
    for value in ['Pancreas,Unknown', 'pancreas,Islet', 'Pancreas,', 'Pancreas or Islet']:
        variants.append(source(value))
    wrong_owner = source('Pancreas,Islet')
    wrong_owner['steps'][0]['constraints'][1].update(entity_type='Gene', owner_kind='node')
    variants.append(wrong_owner)
    wrong_relation = source('Pancreas,Islet')
    wrong_relation['steps'][0]['constraints'][1]['relationship_type'] = 'PART_OF_GWAS_SIGNAL'
    variants.append(wrong_relation)
    for raw in variants:
        assert compile_property_owners(raw, GROUNDING)[1] is not None
    for grounding in [{**GROUNDING, 'status': 'unavailable'},
                      {**GROUNDING, 'identity': {'graph_release': 'another-release'}}]:
        result, _ = compile_property_owners(source('Pancreas,Islet'), grounding)
        assert result['steps'][0]['constraints'][1]['value'] == 'Pancreas,Islet'
        assert 'invalid_constraint_list:tissue_name' in validate_cypher(QTL_QUERY, result['steps'][0])


def test_literal_commas_are_preserved_in_scalar_or_single_list_member():
    assert list_value(['Pancreas,Islet']) == ['Pancreas,Islet']
    assert list_value('["Pancreas,Islet"]') == ['Pancreas,Islet']
    for value, operator, predicate in [
        ('Pancreas,Islet', '=', "= 'Pancreas,Islet'"),
        (['Pancreas,Islet'], 'IN', "IN ['Pancreas,Islet']"),
    ]:
        step = compiled(source(value, operator=operator))
        query = QTL_QUERY.replace("IN ['Pancreas','Islet']", predicate)
        assert step['constraints'][1]['value'] == value
        assert validate_cypher(query, step) == []
        assert validate_cypher(QTL_QUERY, step)
    # A recorded complete literal prevents ambiguous splitting, even when
    # each comma-separated fragment is also recorded independently.
    with pytest.raises(ValueError, match='invalid_constraint_list'):
        list_value('A,B', categories=['A', 'B', 'A,B'])
    with pytest.raises(ValueError, match='invalid_constraint_list'):
        list_value('A,B', categories=['A', 'A', 'B'])


@pytest.mark.parametrize('value', ['', 'A,B', '"A,B"', 'null', '{}', '[]', [],
                                 [['A']], [None], [{'name': 'A'}], [float('nan')]])
def test_unsupported_list_shapes_fail_closed(value):
    with pytest.raises(ValueError, match='invalid_constraint_list'):
        list_value(value)


def test_native_list_schema_preserves_string_elements_and_existing_scalars():
    from pankagent_vnext.llm import PLAN_SCHEMA
    assert PLAN_SCHEMA['properties']['steps']['items']['properties']['constraints']['items']['properties']['value'] == VALUE_SCHEMA
    for value in ['0.1', 'literal,with,comma', ['Pancreas', 'Islet'], ['literal,with,comma']]:
        assert matches_schema(value, VALUE_SCHEMA)
    for value in [None, {'a': 'b'}, [['Pancreas']], [None], [1]]:
        assert not matches_schema(value, VALUE_SCHEMA)


def test_validator_accepts_legacy_json_lists_but_never_raw_comma_strings():
    step = {**source('["Pancreas", "Islet"]')['steps'][0], 'graph_version': RELEASE}
    assert validate_cypher(QTL_QUERY, step) == []
    step['constraints'][1]['value'] = 'Pancreas,Islet'
    assert 'invalid_constraint_list:tissue_name' in validate_cypher(QTL_QUERY, step)
