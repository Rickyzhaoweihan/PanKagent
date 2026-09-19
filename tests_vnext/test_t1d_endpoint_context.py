"""T1D disease context must not become a contradictory anatomical endpoint."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.graph_contract import normalize_release_constraints
from pankagent_vnext.planning_compile import compile_property_owners
from tests_vnext.test_grounded_cell_owner import region_grounding
from tests_vnext.test_planning_compile import context, field, plan
from tests_vnext.test_planning_compiler_gateway import gateway_for


QUESTION = 'Which genes are differentially expressed in alpha cells in T1D?'
DISEASE = ('disease', 'MONDO_0005147', 'T1D')
SAVED = json.loads(Path(__file__).with_name('fixtures').joinpath('region_t1d_endpoint_proposal.json').read_text())


def grounding():
    return context(DISEASE, catalog_complete=True)


def compile_endpoint(constraint=None, *, question=QUESTION, data=None, relations=None):
    raw = plan(constraint or field('end_id', DISEASE[1]), relation='T1D_DEG_IN')
    if relations is not None:
        raw['steps'][0]['relation_types'] = relations
    return compile_property_owners(raw, data or grounding(), question=question)


def test_ownerless_grounded_t1d_context_uses_existing_disease_normalization():
    original = field('end_id', DISEASE[1])
    result, issue = compile_endpoint(original)
    assert issue is None and original == field('end_id', DISEASE[1])
    step = result['steps'][0]
    assert step['constraints'] == [field('id', DISEASE[1], 'disease', owner_kind='node')]
    assert step['constraint_compilation'][0]['requested'] == original
    normalized = normalize_release_constraints(step)
    assert normalized['constraints'] == []
    assert normalized['schema_bindings'][0]['to'] == 'required relationship T1D_DEG_IN'
    assert compile_property_owners(result, grounding(), question=QUESTION) == (result, None)


@pytest.mark.parametrize('question', [
    'Find T1D records whose raw end_id field is MONDO_0005147.',
    'For T1D compare the end id to MONDO_0005147.',
    'For T1D inspect the endpoint identifier MONDO_0005147.',
    'For T1D inspect the target identifier MONDO_0005147.',
    'For T1D inspect the endpoint ID MONDO_0005147.',
    'For T1D inspect the target ID MONDO_0005147.',
    'Find records whose endpoint is T1D.',
    'Find records whose target node equals T1D.',
    'Find genes in alpha cells.',
    'Previously T1D was investigated. Find genes in alpha cells.',
])
def test_explicit_raw_endpoint_or_absent_current_disease_context_is_preserved(question):
    result, issue = compile_endpoint(question=question)
    assert issue is None
    constraint = result['steps'][0]['constraints'][0]
    assert constraint == field('end_id', DISEASE[1], owner_kind='relationship', relationship_type='T1D_DEG_IN')
    assert normalize_release_constraints(result['steps'][0])['constraints'] == [constraint]


@pytest.mark.parametrize('constraint', [
    field('end_id', DISEASE[1], owner_kind='relationship'),
    field('end_id', DISEASE[1], relationship_type='T1D_DEG_IN'),
    field('T1D_DEG_IN.end_id', DISEASE[1]),
    field('end_id', DISEASE[1], operator='!='),
    field('end_id', DISEASE[1], operator='<>'),
    field('end_id', [DISEASE[1]], operator='IN'),
    field('end_id', [DISEASE[1]], operator='NOT IN'),
    field('end_id', 'MONDO_0005015'),
    field('end_id', 'T1D'),
    field('start_id', DISEASE[1]),
])
def test_explicit_edge_owners_other_values_and_operators_are_not_removed(constraint):
    result, issue = compile_endpoint(constraint)
    assert issue is None
    current = result['steps'][0]['constraints'][0]
    assert current['entity_type'] is None and current['owner_kind'] == 'relationship'
    assert current['value'] == constraint['value'] and current['operator'] == constraint['operator']
    assert normalize_release_constraints(result['steps'][0])['constraints'] == [current]


@pytest.mark.parametrize('owner', ['Gene', 'anatomical_structure', 'disease'])
def test_explicit_node_owner_is_not_silently_reassigned(owner):
    assert compile_endpoint(field('end_id', DISEASE[1], owner))[1] == f'invalid_property_owner:s1:{owner}.end_id'


@pytest.mark.parametrize('change', ['catalog_incomplete', 'identity_incomplete', 'ambiguous', 'unresolved', 'wrong_release', 'unavailable'])
def test_endpoint_context_mapping_requires_complete_same_release_grounding(change):
    data = grounding()
    if change == 'catalog_incomplete':
        data['catalog_complete'] = False
    elif change == 'identity_incomplete':
        data['mentions'][0]['identity_complete'] = False
    elif change == 'ambiguous':
        data['mentions'][0]['candidates'].append({'entity_type': 'disease', 'id': 'other', 'name': 'T1D'})
    elif change == 'unresolved':
        data['mentions'][0]['state'] = 'ambiguous'
    elif change == 'wrong_release':
        data['identity']['graph_release'] = 'other'
    else:
        data['status'] = 'unavailable'
    result, _ = compile_endpoint(data=data)
    assert result['steps'][0]['constraints'][0]['property'] == 'end_id'


@pytest.mark.parametrize('relations', [['PART_OF_GWAS_SIGNAL'], ['T1D_DEG_IN', 'PART_OF_GWAS_SIGNAL']])
def test_other_or_multiple_relationships_do_not_gain_t1d_context_mapping(relations):
    result, _ = compile_endpoint(relations=relations)
    assert result['steps'][0]['constraints'][0]['property'] == 'end_id'


def test_saved_region_proposal_full_gateway_and_actual_candidates_preserve_correct_scope():
    async def check():
        raw = deepcopy(SAVED['proposal'])
        data = region_grounding()
        gateway, calls = gateway_for(lambda _: deepcopy(raw))
        result = await gateway.plan(SAVED['question'], [], grounding=data)
        assert len(calls) == 1 and not result.get('proposal_issue') and len(result['steps']) == 2
        first, second = result['steps']
        assert first['depends_on'] == [] and second['depends_on'] == ['s1']
        assert second['relation_types'] == ['T1D_DEG_IN']
        assert not any(c['property'] == 'end_id' or c.get('entity_type') == 'disease' for c in second['constraints'])
        assert any(c['property'] == 'id' and c['value'] == 'CL_0000171' for c in second['constraints'])
        assert any(binding['requested']['property'] == 'end_id' for binding in second['constraint_compilation'])
        assert any(binding['to'] == 'required relationship T1D_DEG_IN' for binding in second['schema_bindings'])
        assert await gateway.plan(SAVED['question'], [], grounding=data) == result
        assert len(calls) == 1 and raw == SAVED['proposal']
        # Saved Cypher text is exact; dependency IDs here are a controlled
        # validation fixture. This performs no graph read or model call.
        step = {**second, 'graph_version': data['identity']['graph_release']}
        parameters = {'dep_0': ['ENSG00000186868']}
        assert validate_cypher(SAVED['candidate_queries'][0], step, parameters) == []
        assert 'unrequested_identity_filter:end_id' in validate_cypher(SAVED['candidate_queries'][1], step, parameters)
    asyncio.run(check())
