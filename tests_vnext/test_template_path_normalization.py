"""Ordinary cohort templates preserve scope when a model over-specifies paths."""
import asyncio
from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from pankagent_vnext.bounded_paths import plan_issue
from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.llm import ClaudeGateway, plan_structure_issue
from pankagent_vnext.query_templates import compile_query, normalize_template_paths
from pankagent_vnext.release_schema import REGISTRY


QUESTION = 'How many PLN scRNAseq samples from T1D stage 3 HPAP donors available?'
RELEASE = REGISTRY['release']


def proposal(reverse=False):
    # Representative first-role population proposal, not a recorded model reply.
    step = {
        'id': 's1', 'question': QUESTION, 'relation_types': ['HAS_SAMPLE'],
        'complete': True, 'depends_on': [], 'evidence_combination': 'cooccurrence',
        'constraints': [
            {'entity_type': 'donor', 'owner_role': 'cohort', 'property': 'data_source',
             'operator': '=', 'value': 'HPAP'},
            {'entity_type': 'donor', 'owner_role': 'cohort', 'property': 't1d_stage',
             'operator': '=', 'value': 'Stage 3: presence of clinical symptoms'},
            {'entity_type': 'Sample_node', 'owner_role': 'sample', 'property': 'data_modality',
             'operator': '=', 'value': 'scRNA-seq'},
            {'entity_type': 'anatomical_structure', 'owner_role': 'tissue', 'property': 'id',
             'operator': '=', 'value': 'UBERON_0015865'},
        ],
        'path_spec': {'version': 'bounded-path-v1', 'nodes': [
            {'role': 'cohort', 'entity_types': ['donor']},
            {'role': 'sample', 'entity_types': ['Sample_node']},
            {'role': 'tissue', 'entity_types': ['anatomical_structure']},
        ], 'edges': [
            {'role': 'donor_samples', 'from': 'cohort', 'to': 'sample',
             'types_any': ['HAS_SAMPLE'], 'direction': 'out'},
            {'role': 'tissue_samples', 'from': 'sample', 'to': 'tissue',
             'types_any': ['HAS_SAMPLE'], 'direction': 'in'},
        ]},
    }
    if reverse:
        step['path_spec']['nodes'].reverse()
        step['path_spec']['edges'].reverse()
        for edge in step['path_spec']['edges']:
            edge['from'], edge['to'] = edge['to'], edge['from']
            edge['direction'] = {'in': 'out', 'out': 'in'}[edge['direction']]
    return {'interpreted_question': QUESTION, 'steps': [step], 'clarification': None}


def prepared(step):
    step = deepcopy(step)
    step['graph_version'] = RELEASE
    step['semantic_registry'] = {'donor_required': True, 'inventory_sha256': 'fixture-current'}
    step['semantic_request'] = {'source': 'user_request', 'question': QUESTION}
    step['sample_requirements'] = {'paired': False, 'separate_bindings': False}
    step['request_filter_bindings'] = []
    step['resolved_constraints'] = []
    step['resolved_entities'] = []
    for index, constraint in enumerate(step['constraints']):
        step['request_filter_bindings'].append({
            'constraint_index': index, 'canonical_binding': deepcopy(constraint),
            'authorization_kind': 'verified_test_request_filter', 'source': 'immutable_user_request',
            'request_sha256': hashlib.sha256(QUESTION.encode()).hexdigest(), 'graph_release': RELEASE,
        })
        if constraint['property'] == 'id':
            step['resolved_entities'].append({
                'constraint_index': index, 'requested': deepcopy(constraint), 'state': 'resolved',
                'graph_version': RELEASE, 'entity_type': constraint['entity_type'],
                'labels': [constraint['entity_type']], 'id': constraint['value'],
            })
        else:
            step['resolved_constraints'].append({
                'canonical_binding': deepcopy(constraint), 'match_kind': 'verified_runtime_fixture',
                'graph_release': RELEASE, 'inventory_sha256': 'fixture-current',
            })
    return step


@pytest.mark.parametrize('reverse', [False, True])
def test_same_sample_template_keeps_every_constraint_and_role_in_both_orientations(reverse):
    original = proposal(reverse)
    snapshot = deepcopy(original)
    assert plan_issue(original['steps'][0]) == (None if reverse else 'missing_path_anchor_identity')
    result = normalize_template_paths(QUESTION, original)
    step = result['steps'][0]
    assert 'path_spec' not in step
    assert step['constraints'] == original['steps'][0]['constraints']
    assert step['relation_types'] == original['steps'][0]['relation_types']
    assert step['template_topology_normalization']['original_path_spec'] == original['steps'][0]['path_spec']
    assert original == snapshot
    assert normalize_template_paths(QUESTION, result) == result
    assert plan_structure_issue(result) is None
    ready = prepared(step)
    query = compile_query(ready)
    assert query['template_id'] == 'donor_tissue_same_sample_records'
    assert query['parameters'] == {
        'template_0': 'HPAP', 'template_1': 'Stage 3: presence of clinical symptoms',
        'template_2': 'scRNA-seq', 'template_3': 'UBERON_0015865',
    }
    assert '(d:`donor`)-[rd:`HAS_SAMPLE`]->(s:`Sample_node`)<-[rt:`HAS_SAMPLE`]-(t:`anatomical_structure`)' in query['cypher']
    assert validate_cypher(query['cypher'], ready, query['parameters']) == []
    assert 'LIMIT' not in query['cypher']


@pytest.mark.parametrize('mutation', [
    'explicit_path', 'dependent', 'consumed', 'combined', 'binding', 'edge_property',
    'wrong_direction', 'ambiguous_owner', 'repeated_type', 'extra_relation',
    'incomplete', 'independent', 'distinct', 'paired', 'separate',
])
def test_non_equivalent_or_role_consuming_paths_retain_original_contract(mutation):
    plan = proposal()
    step = plan['steps'][0]
    question = QUESTION
    if mutation == 'explicit_path': question = 'Show the connected donor sample tissue path.'
    if mutation == 'dependent': step['depends_on'] = ['parent']
    if mutation == 'consumed': plan['steps'].append({'id': 's2', 'depends_on': ['s1']})
    if mutation == 'combined': plan['combine_operations'] = [{'inputs': [{'step_id': 's1'}]}]
    if mutation == 'binding': step['input_bindings'] = [{'target_role': 'cohort', 'entity_type': 'donor'}]
    if mutation == 'edge_property': step['constraints'].append({
        'owner_role': 'donor_samples', 'owner_kind': 'relationship',
        'relationship_type': 'HAS_SAMPLE', 'property': 'data_source', 'value': 'HPAP'})
    if mutation == 'wrong_direction': step['path_spec']['edges'][1]['direction'] = 'out'
    if mutation == 'ambiguous_owner': step['constraints'][0]['entity_type'] = None
    if mutation == 'repeated_type': step['path_spec']['nodes'][2]['entity_types'] = ['donor']
    if mutation == 'extra_relation': step['relation_types'].append('HAS_DONOR')
    if mutation == 'incomplete': step['complete'] = False
    if mutation == 'independent': step['evidence_combination'] = 'independent'
    if mutation == 'distinct': step['path_spec']['nodes'][2]['distinct_from'] = ['cohort']
    if mutation == 'paired': step['sample_requirements'] = {'paired': True}
    if mutation == 'separate': step['sample_requirements'] = {'separate_bindings': True}
    assert normalize_template_paths(question, plan) == plan


def test_normalization_cannot_replace_missing_runtime_identity_or_inventory_proofs():
    step = prepared(normalize_template_paths(QUESTION, proposal())['steps'][0])
    valid = compile_query(step)
    step['resolved_entities'] = []
    assert compile_query(step) is None
    assert validate_cypher(valid['cypher'], step, valid['parameters']) == [
        'normalized_topology_requires_verified_template']
    step = prepared(normalize_template_paths(QUESTION, proposal())['steps'][0])
    step['resolved_constraints'] = []
    assert compile_query(step) is None
    assert validate_cypher(valid['cypher'], step, valid['parameters']) == [
        'normalized_topology_requires_verified_template']


@pytest.mark.parametrize('mutation', [
    'no_tissue_identity', 'no_donor_predicate', 'empty_identity', 'whitespace_identity',
    'identity_list', 'duplicate_identity', 'numeric_operator', 'numeric_age',
    'malformed_value', 'unsupported_operator',
])
def test_template_prerequisites_and_supported_shapes_are_required_before_normalization(mutation):
    plan = proposal()
    constraints = plan['steps'][0]['constraints']
    if mutation == 'no_tissue_identity': constraints.pop()
    if mutation == 'no_donor_predicate': constraints[:] = constraints[2:]
    if mutation == 'empty_identity': constraints[-1]['value'] = ''
    if mutation == 'whitespace_identity': constraints[-1]['value'] = ' '
    if mutation == 'identity_list': constraints[-1].update(operator='IN', value=['UBERON_0015865'])
    if mutation == 'duplicate_identity': constraints.append(deepcopy(constraints[-1]))
    if mutation == 'numeric_operator': constraints[1].update(operator='>', value=3)
    if mutation == 'numeric_age': constraints[1].update(property='age', value=18)
    if mutation == 'malformed_value': constraints[2]['value'] = {'value': 'scRNA-seq'}
    if mutation == 'unsupported_operator': constraints[2]['operator'] = 'LIKE'
    assert normalize_template_paths(QUESTION, plan) == plan


def test_later_scope_change_cannot_switch_to_optional_tissue_template():
    step = prepared(normalize_template_paths(QUESTION, proposal())['steps'][0])
    step['constraints'].pop()
    step['resolved_entities'] = []
    unmarked = deepcopy(step)
    unmarked.pop('template_topology_normalization')
    fallback = compile_query(unmarked)
    assert fallback['template_id'] == 'directed_relation_records'
    assert 'OPTIONAL MATCH' in fallback['cypher']
    assert compile_query(step) is None
    assert validate_cypher(fallback['cypher'], step, fallback['parameters']) == [
        'normalized_topology_requires_verified_template']


@pytest.mark.parametrize('mutation', ['optional_tissue', 'different_sample', 'changed_parameter'])
def test_fallback_candidates_cannot_change_mandatory_same_sample_topology(mutation):
    step = prepared(normalize_template_paths(QUESTION, proposal())['steps'][0])
    compiled = compile_query(step)
    query, parameters = compiled['cypher'], deepcopy(compiled['parameters'])
    if mutation == 'optional_tissue':
        query = query.replace(
            '(s:`Sample_node`)<-[rt:`HAS_SAMPLE`]-(t:`anatomical_structure`)',
            '(s:`Sample_node`) OPTIONAL MATCH (s)<-[rt:`HAS_SAMPLE`]-(t:`anatomical_structure`)')
    if mutation == 'different_sample':
        query = query.replace(
            '(s:`Sample_node`)<-[rt:`HAS_SAMPLE`]-(t:`anatomical_structure`)',
            '(s:`Sample_node`), (other:`Sample_node`)<-[rt:`HAS_SAMPLE`]-(t:`anatomical_structure`)')
    if mutation == 'changed_parameter': parameters['template_3'] = 'unrequested-tissue'
    assert validate_cypher(query, step, parameters) == [
        'normalized_topology_parameter_mismatch' if mutation == 'changed_parameter'
        else 'normalized_topology_requires_verified_template']


def test_negative_predicate_survives_normalization_and_compilation():
    plan = proposal()
    plan['steps'][0]['constraints'][2]['operator'] = '!='
    step = prepared(normalize_template_paths(QUESTION, plan)['steps'][0])
    query = compile_query(step)
    assert 's.`data_modality` <> $template_2' in query['cypher']
    assert query['parameters']['template_2'] == 'scRNA-seq'
    assert validate_cypher(query['cypher'], step, query['parameters']) == []


def test_gateway_normalizes_before_bounded_path_validation(monkeypatch):
    async def session(gateway, question, user, system, schema, output_limit, finalize, **kwargs):
        return finalize(proposal(), [])

    monkeypatch.setattr('pankagent_vnext.planning_session.run', session)
    gateway = object.__new__(ClaudeGateway)
    gateway.settings = SimpleNamespace(anthropic_key='mock-only', model='mock')
    result = asyncio.run(gateway.plan(QUESTION, []))
    assert not result.get('clarification')
    assert 'path_spec' not in result['steps'][0]
    assert result['execution_mode'] == 'parallel'
    assert result['steps'][0]['constraints'] == proposal()['steps'][0]['constraints']
