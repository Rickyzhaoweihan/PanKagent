"""Grounded cell aliases retain literal-field and endpoint ownership boundaries."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from pankagent_vnext import planning_compile
from pankagent_vnext.genomic_scope import coordinate_metadata, has_verified_region_scope
from pankagent_vnext.planning_compile import compile_property_owners
from tests_vnext.test_planning_compile import context, field, plan
from tests_vnext.test_planning_compiler_gateway import gateway_for


ALPHA = ('anatomical_structure', 'CL_0000171', 'alpha cell')
BETA = ('anatomical_structure', 'CL_0000169', 'beta cell')
TISSUE = ('anatomical_structure', 'UBERON_0001264', 'Pancreas')
QUESTION = 'Find T1D differentially expressed genes in alpha cells.'
SAVED = json.loads(Path(__file__).with_name('fixtures').joinpath('region_cell_owner_proposals.json').read_text())


def grounded(*candidates):
    return context(*(candidates or (ALPHA,)), catalog_complete=True)


def compile_cell(constraint=None, data=None, question=QUESTION, relations=None):
    raw = plan(constraint or field('cell_type', 'alpha cell'), relation='T1D_DEG_IN')
    if relations is not None:
        raw['steps'][0]['relation_types'] = relations
    return compile_property_owners(raw, data or grounded(), question=question)


@pytest.mark.parametrize('prop,operator,value,expected', [
    ('cell_type', '=', 'alpha cell', ALPHA[1]),
    ('cell_type_name', '=', 'alpha cell', ALPHA[1]),
    ('cell_type_id', '=', ALPHA[1], ALPHA[1]),
    ('cell_type', 'IN', ['alpha cell', 'beta cell'], [ALPHA[1], BETA[1]]),
    ('cell_type', 'IN', '["alpha cell", "beta cell"]', [ALPHA[1], BETA[1]]),
])
def test_verified_cell_alias_preserves_operator_values_and_provenance(prop, operator, value, expected):
    raw = plan(field(prop, value, operator=operator), relation='T1D_DEG_IN')
    before = deepcopy(raw)
    question = 'Find T1D DEGs in alpha cells and beta cells.'
    compiled, issue = compile_property_owners(raw, grounded(ALPHA, BETA), question=question)
    assert issue is None and raw == before
    result = compiled['steps'][0]['constraints'][0]
    assert result == field('id', expected, 'anatomical_structure', operator, owner_kind='node')
    assert compiled['steps'][0]['constraint_compilation'][0]['requested'] == before['steps'][0]['constraints'][0]
    assert compile_property_owners(compiled, grounded(ALPHA, BETA), question=question) == (compiled, None)


@pytest.mark.parametrize('constraint', [
    field('cell_type', 'alpha cell', 'Gene'),
    field('cell_type', 'alpha cell', 'anatomical_structure'),
    field('cell_type', 'alpha cell', relationship_type='T1D_DEG_IN'),
    field('cell_type', 'alpha cell', owner_kind='node'),
    field('cell_type', 'alpha cell', owner_kind='relationship'),
    field('Gene.cell_type', 'alpha cell'),
    field('T1D_DEG_IN.cell_type', 'alpha cell'),
    field('cell_type_id', 'alpha cell'),
    field('cell_type_name', ALPHA[1]),
    field('cell_type', 'alpha cell', operator='CONTAINS'),
    field('arbitrary_cell_label', 'alpha cell'),
])
def test_explicit_owners_unknown_fields_and_mismatched_identity_semantics_are_not_reinterpreted(constraint):
    assert compile_cell(constraint)[1] is not None


@pytest.mark.parametrize('relations', [
    [], ['HAS_CELL_TYPE'], ['HAS_STATE'], ['PART_OF'], ['ADJACENT_TO'],
    ['PART_OF_QTL_SIGNAL'], ['HAS_SAMPLE'], ['UNKNOWN_RELATION'],
    ['T1D_DEG_IN', 'HAS_CELL_TYPE'], ['T1D_DEG_IN', 'GENE_ENRICHED_IN'],
    ['T1D_DEG_IN', 'UNKNOWN_RELATION'],
])
def test_missing_multiple_or_ambiguous_anatomical_endpoints_cannot_authorize_alias(relations):
    assert compile_cell(relations=relations)[1] is not None


@pytest.mark.parametrize('mutation', ['incomplete_catalog', 'incomplete_identity', 'ambiguous', 'unresolved', 'wrong_release'])
def test_alias_requires_complete_unique_release_grounding(mutation):
    data = grounded()
    if mutation == 'incomplete_catalog':
        data['catalog_complete'] = False
    elif mutation == 'incomplete_identity':
        data['mentions'][0]['identity_complete'] = False
    elif mutation == 'ambiguous':
        data['mentions'][0]['candidates'].append({'entity_type': ALPHA[0], 'id': BETA[1], 'name': ALPHA[2]})
    elif mutation == 'unresolved':
        data['mentions'][0]['state'] = 'ambiguous'
    else:
        data['identity']['graph_release'] = 'other_release'
    compiled, issue = compile_cell(data=data)
    assert compiled['steps'][0]['constraints'][0]['property'] == 'cell_type'
    assert issue or mutation == 'wrong_release'  # The whole compiler deliberately skips unsupported releases.


@pytest.mark.parametrize('question', [
    'Find T1D DEGs in beta cells.',
    'Previously alpha cells were studied. Find T1D DEGs.',
    'For example alpha cells. Find T1D DEGs.',
    'Find T1D DEGs using the raw cell_type field for alpha cells.',
    'Find T1D DEGs using cell_type_id for alpha cells.',
    'Find T1D DEGs using cell_type_name for alpha cells.',
])
def test_raw_field_requests_and_noncurrent_mentions_cannot_authorize_alias(question):
    assert compile_cell(question=question)[1] is not None


def test_verified_tissue_is_not_a_cell_even_with_an_ontology_id():
    assert compile_cell(field('cell_type', 'Pancreas'), grounded(TISSUE),
                        'Find T1D DEGs in Pancreas.')[1] is not None


@pytest.mark.parametrize('relation,prop', [('GENE_DETECTED_IN', 'cell_type'), ('MARKER_GENE_OF', 'cell_type_name')])
def test_real_relationship_cell_field_keeps_storage_semantics(relation, prop):
    result, issue = compile_cell(field(prop, 'alpha cell'), relations=[relation])
    assert issue is None
    assert result['steps'][0]['constraints'][0] == field(
        prop, 'alpha cell', owner_kind='relationship', relationship_type=relation)


def test_future_real_node_field_cannot_be_replaced_by_cell_identity(monkeypatch):
    registry = deepcopy(planning_compile.REGISTRY)
    registry['nodes']['Gene'].append('cell_type')
    monkeypatch.setattr(planning_compile, 'REGISTRY', registry)
    result, issue = compile_cell()
    assert issue is None
    assert result['steps'][0]['constraints'][0] == field('cell_type', 'alpha cell', 'Gene', owner_kind='node')


def region_grounding():
    """Controlled metadata fixture, separate from the saved model proposals."""
    data = grounded(ALPHA, ('disease', 'MONDO_0005147', 'T1D'))
    for symbol, identifier in [('MAPT', 'ENSG00000186868'), ('PLEKHM1', 'ENSG00000134504')]:
        data['mentions'].append({'requested': symbol, 'state': 'resolved', 'identity_complete': False,
            'context_role': {'role': 'genomic_locus_label'},
            'candidates': [{'id': identifier, 'name': symbol, 'entity_type': 'Gene'}]})
    data['genomic_coordinate_metadata'] = coordinate_metadata([{
        'total': 10, 'coordinate_count': 10, 'chromosomes': ['17'],
        'assembly_count': 10, 'assembly_values': ['GRCh38.p14'],
        'genome_assembly_count': 10, 'genome_assembly_values': ['GRCh38.p14'],
    }], data['identity'])
    return data


@pytest.mark.parametrize('index', [0, 1])
def test_saved_region_proposals_pass_full_gateway_scope_and_repair_pipeline(index):
    async def check():
        proposal = deepcopy(SAVED['proposals'][index])
        before = deepcopy(proposal)
        data = region_grounding()
        gateway, calls = gateway_for(lambda _: deepcopy(proposal))
        result = await gateway.plan(SAVED['question'], [], grounding=data)
        assert len(calls) == 1 and not result.get('proposal_issue') and not result.get('clarification')
        assert len(result['steps']) == 2 and proposal == before
        first, second = result['steps']
        assert first['depends_on'] == [] and second['depends_on'] == ['s1']
        assert second['relation_types'] == ['T1D_DEG_IN']
        for step in result['steps']:
            assert has_verified_region_scope({**step, 'graph_version': data['identity']['graph_release']})
            coordinates = {(c['property'], c['operator'], c['value']) for c in step['constraints'] if c.get('entity_type') == 'Gene'}
            assert coordinates == {('chr', '=', '17'), ('genome_assembly', '=', 'GRCh38.p14'),
                                   ('start_loc', '<=', 46000000), ('end_loc', '>=', 43000000)}
            assert not any(c.get('entity_type') == 'disease' for c in step['constraints'])
        cells = [c for c in second['constraints'] if c.get('entity_type') == 'anatomical_structure']
        assert cells and all(c['property'] == 'id' and c['operator'] == '=' and c['value'] == ALPHA[1] for c in cells)
        assert any(c['requested']['property'] == 'cell_type' for c in second['constraint_compilation'])
        assert await gateway.plan(SAVED['question'], [], grounding=data) == result
        assert len(calls) == 1
    asyncio.run(check())


@pytest.mark.parametrize('other', [
    field('cell_type', 'beta cell'),
    field('id', BETA[1], 'anatomical_structure'),
    field('name', BETA[2], 'anatomical_structure'),
    field('cell_type', ['beta cell'], operator='IN'),
    field('id', [ALPHA[1], BETA[1]], 'anatomical_structure', operator='IN'),
])
def test_distinct_positive_cells_on_one_endpoint_need_separate_checks(other):
    raw = plan(field('cell_type', 'alpha cell'), other, relation='T1D_DEG_IN')
    result, issue = compile_property_owners(raw, grounded(ALPHA, BETA),
        question='Which genes are differentially expressed in alpha cells and beta cells in T1D?')
    assert issue == 'conflicting_cell_identity:s1'
    assert [c['operator'] for c in result['steps'][0]['constraints']] == ['=', other['operator']]
    assert compile_property_owners(result, grounded(ALPHA, BETA),
        question='Which genes are differentially expressed in alpha cells and beta cells in T1D?')[1] == issue


@pytest.mark.parametrize('operator', ['!=', '<>', 'NOT IN'])
def test_negative_alias_cannot_satisfy_requested_positive_cell_scope(operator):
    async def check():
        raw = deepcopy(SAVED['proposals'][1])
        cell = raw['steps'][1]['constraints'][0]
        cell['operator'] = operator
        if operator == 'NOT IN':
            cell['value'] = [cell['value']]
        gateway, calls = gateway_for(lambda _: deepcopy(raw))
        result = await gateway.plan(SAVED['question'], [], grounding=region_grounding())
        assert len(calls) == 2
        assert result['proposal_issue'] == 'unknown_property_owner:s2:cell_type:'
        assert result['steps'] == [] and result['recovery']['category'] == 'planning_failure'
    asyncio.run(check())


@pytest.mark.parametrize('prop,value', [('id', BETA[1]), ('name', BETA[2])])
def test_additional_identity_absent_from_current_grounding_cannot_bypass_alias_scope(prop, value):
    raw = plan(field('cell_type', 'alpha cell'), field(prop, value, 'anatomical_structure'), relation='T1D_DEG_IN')
    assert compile_property_owners(raw, grounded(), question=QUESTION)[1] == 'unverified_cell_identity:s1'
