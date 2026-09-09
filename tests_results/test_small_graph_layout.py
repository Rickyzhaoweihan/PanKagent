import asyncio
import copy
import pytest
from pankgraph_results.layout import LayoutService, _initial_coords, _worker
from test_layout import fixture, overlaps


@pytest.mark.parametrize('count,engine', [(1, 'pygraphviz'), (9, 'pygraphviz'), (10, 'optimized_v1')])
def test_threshold(count, engine):
    asyncio.run(check_threshold(count, engine))


async def check_threshold(count, engine):
    service = LayoutService()
    seen = []
    async def worker(payload):
        seen.append(payload['engine'])
        return {'status': 'fallback', 'reason': 'test_probe'}
    service._run_worker = worker
    try:
        await service.layout(fixture(count))
        assert seen == [engine]
    finally:
        await service.close()


def test_real_coloc_parallel_records_and_node_geometry():
    pytest.importorskip('pygraphviz')
    graph = {'nodes': [{'~id': nid, 'display_label': label} for nid, label in
                      [('v', 'rs13393590'), ('g', 'ADCY3'), ('d', 'type 1 diabetes')]],
             'edges': [{'~id': str(i), '~start': start, '~end': end, '~type': kind}
                       for i, (start, end, kind) in enumerate([
                           ('v', 'g', 'PART_OF_QTL_SIGNAL'),
                           ('v', 'd', 'PART_OF_GWAS_SIGNAL'),
                           ('g', 'd', 'SIGNAL_COLOC_WITH'),
                           ('g', 'd', 'SIGNAL_COLOC_WITH')])]}
    original = copy.deepcopy(graph)
    payload = {'graph': graph, 'coords': _initial_coords(graph, ['g']), 'engine': 'pygraphviz'}
    result = _worker(payload)
    assert result['engine'] == 'pygraphviz'
    assert set(result['xy_json']) == {'v', 'g', 'd'}
    assert not overlaps(result['xy_json'])
    assert result['edge_routes'] == {}  # Native frontend curves, no custom ports.
    assert graph == original and len(graph['edges']) == 4
    assert _worker(payload)['xy_json'] == result['xy_json']
