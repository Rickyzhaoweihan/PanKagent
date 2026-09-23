"""Viewer selection never changes formatter evidence or result membership."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pankagent_vnext.app import public_run, aggregate_evidence
from pankagent_vnext.viewer_evidence import select_evidence, prepare_evidence
from pankagent_vnext.composable_planning import combine
from pankgraph_results.projection import project_evidence
from test_composable_planning import evidence, operation


def run(steps, targets=None):
    plan = {'steps': [{'id': s['step_id']} for s in steps], 'execution_mode': 'parallel'}
    if targets is not None:
        plan['answer_step_ids'] = targets
    value = aggregate_evidence({s['step_id']: deepcopy(s) for s in steps}, plan)
    return {'question': 'How many donors?', 'plan': plan, 'evidence': value,
            'preview': {'evidence': deepcopy(value)}, 'graph_answer': 'Two donors [G1].'}


def test_count_keeps_annotations_for_viewer_and_summary_unchanged():
    r = run([evidence('a', ['one', 'two'])])
    before, original = public_run(r), deepcopy(r)
    viewed = select_evidence(r, 'final')
    assert len(project_evidence(viewed)['combined_query_result']['nodes']) == 2
    assert not before['evidence']['nodes']
    assert viewed['steps'][0]['evidence_id'] == 'G1'
    assert public_run(r) == before and r == original


@pytest.mark.parametrize('kind,ids', [('intersection', {'two'}), ('filter', {'two'}),
    ('difference', {'one'}), ('union', {'one', 'two', 'three'})])
def test_no_reintroduction_from_intermediate_steps(kind, ids):
    parents = {'a': evidence('a', ['one', 'two']), 'b': evidence('b', ['two', 'three'])}
    combined = combine(operation(kind), parents)
    r = run([*parents.values(), combined], ['combined'])
    viewed = select_evidence(r, 'final')
    rendered = project_evidence(viewed)
    assert {n['~id'] for n in rendered['combined_query_result']['nodes']} == ids
    assert len(viewed['steps']) == 1
    assert len(r['evidence']['steps']) == 3
    assert viewed['steps'][0]['evidence_id'] == 'G1'  # canonical aggregate assigns selected citation


def test_widening_replaces_old_answer_membership():
    r = run([evidence('old', ['one']), evidence('new', ['one', 'two'])], ['new'])
    assert {n['id'] for n in select_evidence(r, 'final')['nodes']} == {'one', 'two'}


def test_failed_branch_does_not_erase_successful_sibling():
    r = run([evidence('good', ['one']), evidence('bad', ['bad'], status='failed')])
    viewed = select_evidence(r, 'final')
    assert [n['id'] for n in viewed['nodes']] == ['one']
    assert viewed['completeness'] == 'partial'


def test_samples_and_release_mismatch_rejected():
    r = run([evidence('a', ['one'])])
    r['evidence']['steps'][0]['context_compaction'] = 'identity_only'
    with pytest.raises(ValueError, match='formatter_projection'):
        select_evidence(r, 'final')
    del r['evidence']['steps'][0]['context_compaction']
    r['evidence']['steps'][0]['graph_version'] = 'other'
    with pytest.raises(ValueError, match='release_mismatch'):
        select_evidence(r, 'final')


def test_preview_uses_its_own_records():
    r = run([evidence('a', ['one'])])
    r['preview']['evidence']['steps'][0]['nodes'] = []
    assert not select_evidence(r, 'preview')['nodes']
    assert select_evidence(r, 'final')['nodes']


def test_id_hydration_is_exact_and_does_not_change_saved_evidence():
    s = evidence('a', [])
    s['graph_identity_membership'] = {'graph_version': s['graph_version'], 'sampled': False,
        'complete': True, 'typed_ids': [{'id': 'one', 'entity_type': 'donor'}]}
    r = run([s]); original = deepcopy(r)
    class Graph:
        async def hydrate_viewer_ids(self, release, ids):
            assert ids == [('one', 'donor')]
            return {'nodes': evidence('x', ['one', 'outside'])['nodes'], 'truncated': False}
    viewed = asyncio.run(prepare_evidence(r, 'final', Graph()))
    assert [n['id'] for n in viewed['nodes']] == ['one']
    assert r == original
    assert viewed['steps'][0]['annotation_lookup']['status'] == 'complete'


def test_failed_annotation_is_partial_not_complete_empty():
    s = evidence('a', [])
    s['graph_identity_membership'] = {'graph_version': s['graph_version'], 'sampled': False,
        'complete': True, 'typed_ids': [{'id': 'one', 'entity_type': 'donor'}]}
    class Graph:
        async def hydrate_viewer_ids(self, *args):
            raise TimeoutError()
    viewed = asyncio.run(prepare_evidence(run([s]), 'final', Graph()))
    assert viewed['completeness'] == 'partial'
    assert viewed['steps'][0]['annotation_lookup']['status'] == 'unavailable'


def test_verified_chain_and_short_parallel_measurements_remain_rich():
    from test_composable_planning import path_result
    parents = {'a': path_result('a', list('ABCD'), ['a','b','c','left']),
               'b': path_result('b', list('DEF'), ['right','e','f'])}
    spec = operation('join', label='Gene')
    spec['operation']['inputs'][0]['role'] = 'left'
    spec['operation']['inputs'][1]['role'] = 'right'
    joined = combine(spec, parents)
    short = path_result('short', ['X','Y'], ['x','y'])
    short['edges'][0]['properties'] = {'measurement': 17}
    r = run([*parents.values(), joined, short], ['combined', 'short'])
    r['plan']['steps'][0]['context_mode'] = 'identity_only'
    viewed = select_evidence(r, 'final')
    assert [n['id'] for n in viewed['steps'][0]['path_records'][0]['nodes']] == list('ABCDEF')
    assert len(viewed['nodes']) == 8 and len(viewed['edges']) == 6
    assert viewed['steps'][1]['edges'][0]['properties']['measurement'] == 17


def test_formatting_source_files_and_methods_unchanged():
    import subprocess, ast
    from pathlib import Path
    protected = ['pankagent_vnext/llm.py', 'pankagent_vnext/output_scope.py',
        'pankagent_vnext/evidence_context.py', 'pankagent_vnext/format_input_modes.py']
    for path in protected:
        assert Path(path).read_bytes() == subprocess.check_output(['git','show','8c8a771:'+path])
    for path, names in [('pankagent_vnext/app.py', {'public_run','public_payload','execution'}),
                        ('pankgraph_results/app.py', {'answer'})]:
        old = ast.parse(subprocess.check_output(['git','show','8c8a771:'+path]))
        new = ast.parse(Path(path).read_text())
        def methods(tree):
            return {n.name: ast.dump(n) for n in ast.walk(tree)
                    if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names}
        assert methods(old) == methods(new)


def test_scalar_only_count_cannot_become_arbitrary_donors_or_empty_match():
    from pankgraph_results.assembly import assemble
    s = evidence('a', []); s['rows'] = [{'count': 44}]
    v = select_evidence(run([s]), 'final')
    assert v['viewer']['status'] == 'unavailable'
    assert v['steps'][0]['annotation_lookup']['reason'] == 'membership_not_recorded'


def test_graph_endpoint_leaves_normal_run_unchanged(tmp_path):
    from tests_vnext.test_runtime import service
    async def scenario():
        async with service(tmp_path) as (client, runtime, *_):
            record = runtime.store.create('How many donors?', include_context=False)
            r = run([evidence('a', ['one', 'two'])])
            runtime.store.update(record['run_id'], plan=r['plan'], evidence=r['evidence'],
                preview=r['preview'], graph_answer=r['graph_answer'], status='completed')
            url = '/v2/runs/' + record['run_id']
            before = (await client.get(url)).json()
            response = await client.get(url + '/graph')
            assert response.status_code == 200
            assert len(response.json()['evidence']['nodes']) == 2
            assert response.json()['graph_answer'] == before['graph_answer']
            assert (await client.get(url)).json() == before
            preview = (await client.get(url + '/graph?phase=preview')).json()
            assert preview['graph_answer'] == '' and preview['evidence'] is None
            assert (await client.get(url + '/graph?phase=bad')).status_code == 422
    asyncio.run(scenario())


def test_results_overlay_matches_tracked_source(tmp_path):
    import subprocess
    from pathlib import Path
    from deploy_viewer.stage import results_overlay
    (tmp_path / 'pankgraph_results').mkdir()
    for name in ['app.py', 'assembly.py']:
        (tmp_path/'pankgraph_results'/name).write_bytes(subprocess.check_output(
            ['git','show','8c8a771:pankgraph_results/'+name]))
    results_overlay(tmp_path)
    for name in ['app.py', 'assembly.py']:
        assert (tmp_path/'pankgraph_results'/name).read_bytes() == Path('pankgraph_results',name).read_bytes()
