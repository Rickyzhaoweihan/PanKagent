"""Verified interval enumeration and its query/resolution safety boundary."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pankagent_vnext.genomic_scope import compile_genomic_scope, coordinate_metadata
from pankagent_vnext.graph import GraphAdapter, validate_cypher
from pankagent_vnext.query_templates import compile_query


RELEASE = 'PanKgraph_08_04'
QUESTION = 'List all Gene Ensembl IDs overlapping chr17 43–46 Mb in GRCh38.p14.'


def region_step(kinds=()):
    identity = {'graph_release': RELEASE}
    metadata = coordinate_metadata([{'total': 3, 'coordinate_count': 3, 'chromosomes': ['chr17'],
                                     'assembly_count': 3, 'assembly_values': ['GRCh38.p14'],
                                     'genome_assembly_count': 3, 'genome_assembly_values': ['GRCh38.p14']}], identity)
    grounding = {'status': 'ready', 'identity': identity, 'genomic_coordinate_metadata': metadata}
    plan = {'steps': [{'id': 's1', 'question': QUESTION, 'complete': True,
                       'relation_types': list(kinds), 'depends_on': [], 'constraints': []}]}
    plan, issue = compile_genomic_scope(QUESTION, grounding, plan)
    assert issue is None
    return {**plan['steps'][0], 'graph_version': RELEASE}


@pytest.mark.parametrize('kinds', [(), ('T1D_DEG_IN',), ('PART_OF_QTL_SIGNAL',)])
def test_complete_verified_region_can_enumerate_without_a_named_gene_anchor(kinds):
    step = region_step(kinds)
    original = deepcopy(step)
    result = compile_query(step)
    assert result is not None
    assert result['parameters'] == {'template_0': 'chr17', 'template_1': 'GRCh38.p14',
                                    'template_2': 46000000, 'template_3': 43000000}
    assert isinstance(result['parameters']['template_2'], int)
    assert 'LIMIT' not in result['cypher']
    assert 'toFloat' not in result['cypher']
    assert validate_cypher(result['cypher'], step, result['parameters']) == []
    assert step == original
    if not kinds:
        assert result['template_id'] == 'verified_gene_region_records'
        assert '[] AS edges' in result['cypher']


def test_verified_region_resolution_is_literal_and_performs_no_identity_queries():
    async def check():
        graph = object.__new__(GraphAdapter)
        graph.settings = SimpleNamespace(graph_version=RELEASE)
        graph._small_query = AsyncMock(side_effect=AssertionError('no graph query expected'))
        step = region_step()
        for index, constraint in enumerate(step['constraints']):
            record = await graph._resolve_constraint(constraint, index, step)
            assert record['state'] == 'literal_predicate'
            assert record['requested'] == constraint
        graph._small_query.assert_not_called()
        step['genomic_scope_contract']['metadata']['state'] = 'unavailable'
        for index, constraint in enumerate(step['constraints'][:2]):
            assert (await graph._resolve_constraint(constraint, index, step))['state'] == 'unsupported'
    asyncio.run(check())


@pytest.mark.parametrize('kinds', [(), ('T1D_DEG_IN',)])
@pytest.mark.parametrize('mutation', [
    'missing_contract', 'stale_release', 'missing_chr', 'missing_assembly', 'missing_start',
    'missing_end', 'wrong_owner', 'edge_owner', 'modified_bounds', 'unknown_numeric_storage',
    'incomplete_metadata', 'named_anchor', 'extra_narrow_bound', 'arbitrary_numeric',
])
def test_unverified_or_narrowed_interval_never_gets_the_region_template(kinds, mutation):
    step = region_step(kinds)
    if mutation == 'missing_contract':
        step.pop('genomic_scope_contract')
    elif mutation == 'stale_release':
        step['graph_version'] = 'other-release'
    elif mutation.startswith('missing_'):
        step['constraints'].pop({'missing_chr': 0, 'missing_assembly': 1,
                                 'missing_start': 2, 'missing_end': 3}[mutation])
    elif mutation == 'wrong_owner':
        step['constraints'][0]['entity_type'] = 'variants'
    elif mutation == 'edge_owner':
        step['constraints'][0]['owner_kind'] = 'relationship'
        step['constraints'][0]['relationship_type'] = 'PART_OF_QTL_SIGNAL'
    elif mutation == 'modified_bounds':
        step['constraints'][2]['value'] = 45000000
    elif mutation == 'unknown_numeric_storage':
        step['genomic_scope_contract']['metadata']['coordinate_storage'] = 'unknown'
    elif mutation == 'incomplete_metadata':
        step['genomic_scope_contract']['metadata']['coverage']['coordinate_count'] = 2
    elif mutation == 'named_anchor':
        step['constraints'].append({'entity_type': 'Gene', 'property': 'name', 'operator': '=', 'value': 'MAPT'})
    elif mutation == 'extra_narrow_bound':
        step['constraints'].append({'entity_type': 'Gene', 'owner_kind': 'node',
                                    'property': 'start_loc', 'operator': '>', 'value': 45000000})
    else:
        step['constraints'].append({'entity_type': 'Gene', 'owner_kind': 'node',
                                    'property': 'hgnc_symbol', 'operator': '>', 'value': '1'})
    assert compile_query(step) is None


def test_region_query_still_requires_all_coordinates_on_gene_nodes():
    step = region_step(('T1D_DEG_IN',))
    result = compile_query(step)
    query = result['cypher']
    assert validate_cypher(query.replace('a.`start_loc`', 'r.`start_loc`'), step, result['parameters'])
    assert validate_cypher(query + ' LIMIT 2', step, result['parameters'])
    parameters = {**result['parameters'], 'template_2': 45000000}
    assert validate_cypher(query, step, parameters)


@pytest.mark.parametrize('pattern,predicates', [
    ("MATCH (a:Gene)-[r:T1D_DEG_IN]->(c:anatomical_structure), (b:Gene)",
     "a.chr='chr17' AND a.genome_assembly='GRCh38.p14' AND b.start_loc<=46000000 AND b.end_loc>=43000000"),
    ("MATCH (a:Gene)-[r:T1D_DEG_IN]->(c:anatomical_structure), (b:Gene)",
     "b.chr='chr17' AND b.genome_assembly='GRCh38.p14' AND b.start_loc<=46000000 AND b.end_loc>=43000000"),
    ("MATCH (a:Gene)-[r:T1D_DEG_IN]->(c:anatomical_structure) OPTIONAL MATCH (b:Gene)",
     "b.chr='chr17' AND b.genome_assembly='GRCh38.p14' AND b.start_loc<=46000000 AND b.end_loc>=43000000"),
])
def test_gpu_query_cannot_distribute_region_across_genes_or_an_unrelated_optional_gene(pattern, predicates):
    step = region_step(('T1D_DEG_IN',))
    query = pattern + ' WHERE ' + predicates + ' RETURN a,b,r,c'
    assert 'region_scope_not_same_gene' in validate_cypher(query, step)


def test_region_guard_applies_to_every_union_arm():
    step = region_step(('T1D_DEG_IN',))
    good = compile_query(step)
    bad = good['cypher'].replace('a.`start_loc` <= $template_2', 'a.`start_loc` <= 45000000')
    assert 'region_scope_not_same_gene' in validate_cypher(
        good['cypher'] + ' UNION ' + bad, step, good['parameters'])
