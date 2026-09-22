import copy
from collections import Counter

import pytest

from pankagent_vnext.anatomy_membership import (
    DIGEST, REGISTRY, membership_from_snapshot, resolve_cell_membership,
)

RELEASE = 'PanKgraph_08_04'


def test_complete_pancreas_membership_has_all_five_verified_paths():
    result = resolve_cell_membership('UBERON_0001264', RELEASE)
    assert result['cell_count'] == 30
    assert result['maximum_path_length'] == 3
    assert Counter(tuple(e['relation'] for e in path) for paths in result['cell_paths'].values() for path in paths) == {
        ('HAS_CELL_TYPE',): 3,
        ('HAS_CELL_TYPE', 'PART_OF'): 5,
        ('SUBCLASS_OF', 'HAS_CELL_TYPE'): 8,
        ('SUBCLASS_OF', 'HAS_CELL_TYPE', 'PART_OF'): 11,
        ('SUBCLASS_OF', 'SUBCLASS_OF', 'HAS_CELL_TYPE'): 3,
    }
    assert {'CL_0000115', 'CL_0000738'} <= set(result['cell_ids'])  # No marker annotation, still valid groups.
    assert {'CL_0000169', 'CL_0000236', 'CL_0000057'} <= set(result['cell_ids'])
    assert len(result['states']) == 4
    assert not {s['id'] for s in result['states']} & set(result['cell_ids'])


def test_islet_is_its_own_supported_scope_not_pancreas_substitution():
    islet = resolve_cell_membership('UBERON_0000006', RELEASE)
    assert 'CL_0000169' in islet['cell_ids']
    assert 'CL_0002079' not in islet['cell_ids']
    assert 'CL_0000236' not in islet['cell_ids']
    assert islet['requested_root']['id'] == 'UBERON_0000006'
    assert all(path[-1]['target'] == 'UBERON_0000006' for paths in islet['cell_paths'].values() for path in paths)


def test_unknown_empty_and_wrong_root_roles_are_distinct():
    with pytest.raises(ValueError, match='unregistered_'):
        resolve_cell_membership('unknown', RELEASE)
    with pytest.raises(ValueError, match='requires_tissue_or_region'):
        resolve_cell_membership('CL_0000169', RELEASE)
    with pytest.raises(ValueError, match='graph_release_mismatch'):
        resolve_cell_membership('UBERON_0001264', 'other-release')
    empty = resolve_cell_membership('PANKREGION:001', RELEASE)
    assert empty['state'] == 'no_registered_cell_membership'
    assert empty['cell_ids'] == []


def test_pure_generic_resolution_uses_categories_not_id_prefixes():
    snapshot = {
        'version': 'test', 'graph_release': 'test', 'complete_inventory': True,
        'source_sha256': 'source', 'category_source_sha256': 'categories',
        'categories': {'CL_tissue': 'tissue', 'organ': 'tissue', 'base': 'cell_type', 'subtype': 'cell_type', 'state': 'cell_type'},
        'edges': [
            {'source': 'base', 'relation': 'HAS_CELL_TYPE', 'target': 'CL_tissue'},
            {'source': 'CL_tissue', 'relation': 'PART_OF', 'target': 'organ'},
            {'source': 'subtype', 'relation': 'SUBCLASS_OF', 'target': 'base'},
            {'source': 'base', 'relation': 'HAS_STATE', 'target': 'state'},
        ],
    }
    before = copy.deepcopy(snapshot)
    result = membership_from_snapshot('organ', 'test', snapshot, 'test-digest')
    assert result['cell_ids'] == ['base', 'subtype']
    assert result['states'] == [{'id': 'state', 'base_cell_ids': ['base'], 'relation': 'HAS_STATE'}]
    assert snapshot == before


def test_cache_result_mutation_cannot_change_future_membership():
    result = resolve_cell_membership('UBERON_0001264', RELEASE)
    result['cell_ids'].clear()
    result['cell_paths'].clear()
    assert resolve_cell_membership('UBERON_0001264', RELEASE)['cell_count'] == 30
    assert len(resolve_cell_membership('UBERON_0001264', RELEASE)['cell_ids']) == 30


def test_incomplete_unknown_endpoint_and_cycle_fail_without_partial_membership():
    snapshot = copy.deepcopy(REGISTRY)
    snapshot['complete_inventory'] = False
    with pytest.raises(ValueError, match='inventory_incomplete'):
        membership_from_snapshot('UBERON_0001264', RELEASE, snapshot, DIGEST)
    snapshot = copy.deepcopy(REGISTRY)
    snapshot['edges'].append({'source': 'unknown', 'relation': 'HAS_CELL_TYPE', 'target': 'UBERON_0001264'})
    with pytest.raises(ValueError, match='endpoint_role'):
        membership_from_snapshot('UBERON_0001264', RELEASE, snapshot, DIGEST)
    snapshot = copy.deepcopy(REGISTRY)
    snapshot['edges'].append({'source': 'UBERON_0001264', 'relation': 'PART_OF', 'target': 'UBERON_0001264'})
    with pytest.raises(ValueError, match='cyclic_'):
        membership_from_snapshot('UBERON_0001264', RELEASE, snapshot, DIGEST)


def test_snapshot_provenance_and_no_private_record_fields():
    assert len(REGISTRY['edges']) == 54
    assert len(REGISTRY['categories']) == 58
    assert REGISTRY['source_sha256'] and REGISTRY['category_source_sha256']
    assert all(set(edge) == {'source', 'relation', 'target'} for edge in REGISTRY['edges'])
    assert all(not identity.startswith('HPAP-') for identity in REGISTRY['categories'])
