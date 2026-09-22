import asyncio
import copy
import json
import time
from types import SimpleNamespace

import pytest

from pankagent_vnext.anatomy_scope import DIGEST, _request_surfaces, normalize_plan
from pankagent_vnext.graph import GraphAdapter, validate_cypher
from pankagent_vnext.query_templates import runtime_binding_errors
from pankagent_vnext.semantic_registry import attach_request_authorizations

RELEASE = 'PanKgraph_08_04'
ROOT = {'entity_type': 'anatomical_structure', 'property': 'name', 'operator': '=', 'value': 'pancreas'}


async def resolve(constraint, index, step):
    if constraint.get('operator') == 'IN':
        return {'constraint_index': index, 'requested': copy.deepcopy(constraint), 'state': 'literal_predicate', 'graph_version': RELEASE}
    lookup = {
        'pancreas': ('UBERON_0001264', 'pancreas'),
        'UBERON_0001264': ('UBERON_0001264', 'pancreas'),
        'islet': ('UBERON_0000006', 'pancreatic islet (islet of Langerhans)'),
        'UBERON_0000006': ('UBERON_0000006', 'pancreatic islet (islet of Langerhans)'),
        'beta cell': ('CL_0000169', 'beta cell'),
    }
    if constraint.get('value') in lookup:
        identifier, name = lookup[constraint['value']]
        return {'constraint_index': index, 'requested': copy.deepcopy(constraint), 'state': 'resolved', 'graph_version': RELEASE,
                'id': identifier, 'name': name, 'entity_type': 'anatomical_structure', 'labels': ['anatomical_structure']}
    return {'constraint_index': index, 'requested': copy.deepcopy(constraint), 'state': 'not_found'}


def plan():
    question = 'What are the major pancreatic cell types and their key genetic markers?'
    return {'original_question': question,
            'steps': [{'id': 's1', 'question': question,
                       'relation_types': ['HAS_CELL_TYPE', 'MARKER_GENE_OF'], 'depends_on': [], 'complete': True,
                       'constraints': [copy.deepcopy(ROOT)]}], 'clarification': None,
            'display_groups': [{'id': 'g1', 'step_ids': ['s1']}], 'include_context': False}


def normalized(source):
    return asyncio.run(normalize_plan(source, RELEASE, resolve))


def test_one_combined_check_becomes_cell_lookup_and_markers_with_original_provenance():
    source = plan(); before = copy.deepcopy(source)
    result = normalized(source)
    assert source == before
    cells, markers = result['steps']
    assert cells['relation_types'] == []
    assert markers['relation_types'] == ['MARKER_GENE_OF']
    assert markers['id'] == 's1'
    assert markers['depends_on'] == [cells['id']]
    assert len(cells['constraints'][0]['value']) == 30
    assert 'CL_0000115' in cells['constraints'][0]['value']  # Markerless group remains retrievable.
    assert markers['constraints'] == cells['constraints']
    assert markers['anatomy_scope']['original_root_constraint'] == ROOT
    assert markers['anatomy_scope']['membership']['requested_root']['id'] == 'UBERON_0001264'
    assert result['display_groups'][0]['step_ids'] == [cells['id'], 's1']


@pytest.mark.parametrize(('question', 'terms', 'surface'), [
    ('Show the major cell types in islets.', ['islet'], 'islets'),
    ('Show pancreatic cell types.', ['pancreatic'], 'pancreatic'),
    ('Show cell types in PLN.', ['PLN'], 'PLN'),
])
def test_reviewed_anatomy_aliases_preserve_exact_request_surfaces(question, terms, surface):
    assert _request_surfaces(question, terms) == [surface]


def test_canonical_islet_root_keeps_plural_request_authority_for_generated_cell_set():
    question = 'What are the major islets cell types and their key genetic markers?'
    source = plan()
    source['original_question'] = question
    source['steps'][0]['question'] = question
    source['steps'][0]['constraints'][0] = {
        'entity_type': 'anatomical_structure', 'property': 'id',
        'operator': '=', 'value': 'UBERON_0000006'}
    result = normalized(source)
    assert len(result['steps']) == 2
    for generated in result['steps']:
        assert generated['anatomy_scope']['request_terms'] == ['islets']
        prepared = attach_request_authorizations({
            **generated, 'graph_version': RELEASE, 'resolved_entities': [],
            'semantic_request': {'source': 'user_request', 'question': question},
        })
        assert prepared['semantic_issues'] == []
        assert [binding['authorization_kind']
                for binding in prepared['request_filter_bindings']] == [
                    'verified_request_anatomy_scope']


def test_existing_cell_check_is_reused_with_marker_dependency_and_gene_filter_preserved():
    source = plan()
    source['steps'][0]['relation_types'] = ['HAS_CELL_TYPE']
    source['steps'].append({'id': 's2', 'question': 'Find the recorded marker genes for those cell groups',
                            'relation_types': ['MARKER_GENE_OF'], 'depends_on': ['s1'], 'complete': True,
                            'constraints': [{'entity_type': 'Gene', 'property': 'id', 'operator': '=', 'value': 'ENSG00000001626'}]})
    result = normalized(source)
    assert len(result['steps']) == 2
    assert result['steps'][0]['id'] == 's1'
    assert result['steps'][1]['depends_on'] == ['s1']
    assert result['steps'][1]['constraints'][0] == source['steps'][1]['constraints'][0]
    assert len(result['steps'][1]['constraints'][1]['value']) == 30


def test_other_gene_and_evidence_filters_and_dependencies_are_not_dropped():
    source = plan()
    constraints = [
        {'entity_type': 'Gene', 'property': 'id', 'operator': '=', 'value': 'ENSG00000001626'},
        {'owner_kind': 'relationship', 'relationship_type': 'MARKER_GENE_OF', 'property': 'data_source', 'operator': '=', 'value': 'recorded-source'},
    ]
    source['steps'][0]['constraints'].extend(copy.deepcopy(constraints))
    source['steps'][0]['depends_on'] = ['gene_check']
    source['steps'].insert(0, {'id': 'gene_check', 'question': 'Resolve the requested gene', 'relation_types': [], 'constraints': [constraints[0]], 'complete': True})
    result = normalized(source)
    assert len(result['steps']) == 3
    marker = next(s for s in result['steps'] if s['id'] == 's1')
    assert marker['constraints'][:2] == constraints
    assert marker['depends_on'][0] == 'gene_check'


def test_cap_or_extra_tissue_restrictions_produce_specific_recovery_without_loss():
    source = plan()
    source['steps'].extend([{'id': 'other1', 'question': 'Other lookup', 'relation_types': [], 'constraints': []},
                            {'id': 'other2', 'question': 'Other lookup', 'relation_types': [], 'constraints': []}])
    result = normalized(source)
    assert result['anatomy_scope_issue'] == 'step_cap'
    assert result['steps'] == source['steps']
    assert result['entity_resolution']['state'] == 'needs_clarification'
    assert result['recovery']['suggestions']
    source = plan(); source['steps'][0]['constraints'].append({**ROOT, 'value': 'beta cell'})
    assert normalized(source)['anatomy_scope_issue'] == 'multiple_roots'


def test_nonorgan_measurements_and_sample_scopes_are_unchanged():
    for relations, question in [(['GENE_ENRICHED_IN'], 'Find measured enrichment in pancreas'),
                                (['HAS_SAMPLE'], 'Find pancreas samples'),
                                (['MARKER_GENE_OF'], 'Find beta cell markers')]:
        source = plan(); source['steps'][0].update(relation_types=relations, question=question)
        if 'beta cell' in question:
            source['steps'][0]['constraints'][0]['value'] = 'beta cell'
        assert normalized(source) == source


def test_stale_or_modified_generated_cell_ids_are_rebuilt_from_original_plan():
    first = normalized(plan())
    first['steps'][0]['constraints'][0]['value'] = ['wrong']
    first['steps'][1]['anatomy_scope']['membership']['cell_ids'] = ['wrong']
    first['anatomy_scope_normalization']['digest'] = 'old'
    replay = normalized(first)
    assert replay['anatomy_scope_normalization']['digest'] == DIGEST
    assert len(replay['steps'][0]['constraints'][0]['value']) == 30
    assert len(replay['steps']) == 2


def test_generated_queries_still_require_exact_typed_cell_set_and_marker_role():
    cells, marker = normalized(plan())['steps']
    cell_ids = cells['constraints'][0]['value']
    literal = json.dumps(cell_ids)
    cell_query = f'MATCH (c:anatomical_structure) WHERE c.id IN {literal} RETURN collect(c) AS nodes, [] AS edges'
    assert validate_cypher(cell_query, cells) == []
    assert validate_cypher(cell_query.replace(literal, json.dumps(cell_ids[:1])), cells)
    marker_query = f'MATCH (g:Gene)-[r:MARKER_GENE_OF]->(c:anatomical_structure) WHERE c.id IN {literal} AND c.id IN $dep_0 RETURN g,r,c'
    assert validate_cypher(marker_query, marker, {'dep_0': cell_ids}) == []
    assert validate_cypher(marker_query.replace('(g:Gene)-[r:MARKER_GENE_OF]->(c:anatomical_structure)', '(g:Gene)<-[r:MARKER_GENE_OF]-(c:anatomical_structure)'), marker, {'dep_0': cell_ids})
    assert validate_cypher(marker_query.replace(literal, "['UBERON_0001264']"), marker, {'dep_0': cell_ids})


class OfflineAdapter(GraphAdapter):
    def __init__(self):
        self.settings = SimpleNamespace(graph_version=RELEASE, graph_identity_file='/nonexistent-test')
        self.identity_verified = True
        self.identity_check_time = time.monotonic()
        self.release_relations = {'MARKER_GENE_OF', 'HAS_CELL_TYPE'}
    async def _ensure_identity(self):
        pass
    async def _resolve_constraint(self, constraint, index, step):
        return await resolve(constraint, index, step)


def test_prepare_hook_signs_membership_and_returns_specific_cap_recovery_offline():
    async def work():
        adapter = OfflineAdapter()
        async def emit(*args):
            pass
        prepared = await adapter.prepare_plan(plan(), emit)
        assert len(prepared['steps']) == 2
        assert prepared['entity_resolution']['state'] == 'resolved'
        assert all(runtime_binding_errors(step) == [] for step in prepared['steps'])
        assert adapter.preview_identity()['anatomy_scope'] == DIGEST
        assert all(adapter._resolution_verified(s) for s in prepared['steps'])
        prepared['steps'][0]['anatomy_scope']['membership']['cell_ids'].clear()
        assert runtime_binding_errors(prepared['steps'][0]) == [
            'unverified_identity_resolution:0:anatomical_structure.id']
        assert not adapter._resolution_verified(prepared['steps'][0])
    asyncio.run(work())
