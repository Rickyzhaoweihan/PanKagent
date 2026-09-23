"""Synthetic identity, path and lifecycle contracts; never live donor data."""
from copy import deepcopy
import ast
import json
from pathlib import Path
import subprocess

import pytest
from pankagent_vnext.composable_planning import normalize, combine, selected_nodes, answer_results, snapshot
from pankagent_vnext.evidence_status import checked_query_result
from pankagent_vnext.app import aggregate_evidence
from pankagent_vnext.evidence_context import compact_evidence, scientific_excerpt
from pankagent_vnext.bounded_paths import compile_query, plan_issue
from pankagent_vnext.graph import validate_cypher, _public_edge_fingerprint
from pankagent_vnext.revision_interpreter import reuse_step_ids
from test_bounded_paths import four_node_step, prepared, STORED


def evidence(key, ids, label='donor', status='complete'):
    return {'step_id': key, 'graph_version': 'PanKgraph_08_04', 'status': status,
        'nodes': [{'id': i, 'labels': [label], 'properties': {'id': i}} for i in ids],
        'edges': [], 'rows': [], 'queries': [{'cypher': 'MATCH (n) RETURN n'}],
        'validation': [{'valid': True, 'reasons': []}], 'truncated': status == 'partial',
        'retrieval_execution': {'completed': True, 'cursor_exhausted': status != 'partial'}}


def operation(kind, role='', label='donor'):
    spec = {'id': 'combined', 'question': 'Combine the requested records.', 'operator': kind,
        'inputs': [{'step_id': k, 'entity_type': label, 'role': role} for k in ('a', 'b')]}
    return {'id': 'combined', 'question': spec['question'], 'operation': spec, 'depends_on': ['a', 'b']}


@pytest.mark.parametrize('kind,expected', [('union', {'one', 'two', 'three'}),
    ('intersection', {'two'}), ('filter', {'two'}), ('difference', {'one'})])
def test_exact_id_algebra_and_verified_derivation(kind, expected):
    parents = {'a': evidence('a', ['one', 'two']), 'b': evidence('b', ['two', 'three'])}
    step = operation(kind)
    result = combine(step, parents)
    assert {n['id'] for n in result['nodes']} == expected
    assert checked_query_result(step, result, parents)
    altered = deepcopy(result); altered['nodes'].append({'id': 'invented', 'labels': ['donor']})
    assert not checked_query_result(step, altered, parents)
    assert len(parents['a']['nodes']) == 2


def test_partial_exclusion_blocked_and_intersection_not_complete():
    parents = {'a': evidence('a', ['one']), 'b': evidence('b', [], status='partial')}
    assert combine(operation('difference'), parents)['status'] == 'blocked'
    result = combine(operation('intersection'), parents)
    assert result['status'] == 'partial' and result['truncated']
    assert not checked_query_result(operation('intersection'), result, parents)


def test_release_failure_and_formatter_samples_cannot_supply_ids():
    parents = {'a': evidence('a', ['one']), 'b': evidence('b', ['one'])}
    parents['b']['graph_version'] = 'different'
    with pytest.raises(ValueError, match='release_mismatch'):
        combine(operation('intersection'), parents)
    sampled = {**parents['a'], 'context_compaction': 'node_identity_only'}
    with pytest.raises(ValueError, match='formatter_projection'):
        selected_nodes(sampled, 'donor')
    parents['b']['status'] = 'failed'
    assert combine(operation('intersection'), parents)['status'] == 'blocked'


def path_result(key, identifiers, roles):
    result = evidence(key, identifiers, 'Gene')
    result['edges'] = [{'start_id': a, 'end_id': b, 'type': 'PHYSICAL_INTERACTION', 'properties': {}}
                        for a,b in zip(identifiers, identifiers[1:])]
    result['path_records'] = [{'nodes': [{'id': i, 'role': r, 'labels': ['Gene']} for i,r in zip(identifiers, roles)],
        'edges': [{**{k:e[k] for k in ('start_id','end_id','type')}, 'role': key+str(n),
                   'fingerprint': _public_edge_fingerprint(e)} for n,e in enumerate(result['edges'])]}]
    return result


def test_chain_join_beyond_four_nodes_keeps_actual_witnesses():
    parents = {'a': path_result('a', ['A','B','C','D'], ['start','middle','next','join_left']),
               'b': path_result('b', ['D','E','F'], ['join_right','later','end'])}
    step = operation('join', label='Gene')
    step['operation']['inputs'][0]['role'] = 'join_left'
    step['operation']['inputs'][1]['role'] = 'join_right'
    result = combine(step, parents)
    assert [n['id'] for n in result['path_records'][0]['nodes']] == list('ABCDEF')
    assert len(result['edges']) == 5
    assert {n['id'] for n in result['nodes']} == set('ABCDEF')
    assert checked_query_result(step, result, parents)
    parents['b'] = path_result('b', ['X','E','F'], ['join_right','later','end'])
    assert combine(step, parents)['status'] == 'empty'


def test_final_projection_does_not_union_rejected_intermediates():
    parents = {'a': evidence('a', ['excluded','kept']), 'b': evidence('b', ['kept'])}
    parents['combined'] = combine(operation('intersection'), parents)
    plan = {'answer_step_ids': ['combined']}
    aggregate = aggregate_evidence(parents, plan)
    assert {n['id'] for n in aggregate['nodes']} == {'kept'}
    assert len(aggregate['steps'][0]['nodes']) == 2
    assert list(answer_results(plan, parents)) == ['combined']


def test_typed_fragment_anchor_and_exact_compiler_validation():
    step = prepared(four_node_step(), STORED)
    step['constraints'] = []
    step['resolved_entities'] = []
    step['depends_on'] = ['prior']
    step['input_bindings'] = [{'step_id': 'prior', 'entity_type': 'Gene', 'source_role': 'last', 'target_role': 'focus'}]
    assert plan_issue(step) is None
    step['_path_dependency_parameters'] = {'dep_0': {'ids': ['A'], 'graph_version': 'PanKgraph_08_04'}}
    query = compile_query(step)
    assert 'n0.`id` IN $dep_0' in query['cypher']
    assert query['parameters']['dep_0'] == ['A']
    errors = validate_cypher(query['cypher'], step, query['parameters'],
        dependency_bindings={'dep_0': {'graph_version': 'PanKgraph_08_04', 'id_labels': {'A': ['Gene']}}})
    assert errors == []
    assert validate_cypher(query['cypher'].replace('n0.`id` IN', 'n1.`id` IN'), step, query['parameters'])


def test_modes_operations_and_bad_references():
    steps = [{'id': k, 'question': k, 'depends_on': [], 'constraints': [], 'relation_types': []} for k in ['a','b']]
    plan = normalize({'steps': steps, 'combine_operations': [operation('intersection')['operation']]})
    assert plan['answer_step_ids'] == ['combined']
    assert plan['steps'][-1]['depends_on'] == ['a','b']
    with pytest.raises(ValueError, match='invalid_answer'):
        normalize({'steps': steps, 'answer_step_ids': ['missing']})
    with pytest.raises(ValueError, match='invalid_plan_dependencies'):
        normalize({'steps': [{**steps[0], 'depends_on': ['a']}]})


def test_per_query_identity_paths_and_short_full_measurements():
    a = path_result('a', ['A','B','C','D'], ['a','b','c','d'])
    a['context_mode'] = 'identity_only'
    b = evidence('b', ['S','T'], 'Gene')
    b['edges'] = [{'start_id':'S','end_id':'T','type':'PHYSICAL_INTERACTION', 'properties': {'measurement':17}}]
    before = deepcopy(a)
    views = scientific_excerpt(compact_evidence({'a':a,'b':b}))
    assert views[0]['identity_path_records'][0]['nodes'][0]['id'] == 'A'
    assert views[0]['path_input_scope']['measurements_available'] is False
    assert not views[0].get('edges')
    assert views[1]['edges'][0]['properties']['measurement'] == 17
    assert a == before


def test_formatter_output_function_is_byte_identical_to_baseline():
    path = Path(__file__).parents[1] / 'pankagent_vnext/llm.py'
    old = subprocess.check_output(['git','show','4f2d0d3:pankagent_vnext/llm.py'], text=True)
    def output(source):
        node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'synthesize')
        return ast.get_source_segment(source, node)
    assert output(path.read_text()) == output(old)


def test_overretrieved_entities_filtered_before_counts():
    parents = {'a': evidence('a', ['outside','inside']), 'b': evidence('b', ['inside'])}
    result = combine(operation('filter'), parents)
    assert result['combination_summary']['selected_entity_count'] == 1
    assert [n['id'] for n in result['nodes']] == ['inside']
    assert len(parents['a']['nodes']) == 2


def test_parallel_widening_discovers_previously_absent_ids():
    parents = {'a': evidence('a', ['old']), 'b': evidence('b', ['old','new'])}
    assert {n['id'] for n in combine(operation('union'), parents)['nodes']} == {'old','new'}
    assert list(answer_results({'answer_step_ids':['b']}, parents)) == ['b']


def test_cross_type_id_collision_is_not_a_valid_join():
    parents = {'a': evidence('a', ['same']), 'b': evidence('b', ['same'])}
    parents['b']['nodes'].append({'id': 'same','labels':['Gene']})
    with pytest.raises(ValueError, match='cross_type'):
        combine(operation('intersection'), parents)


def test_revision_reuses_semantically_identical_query_id_not_replaced_population():
    def step(key, value):
        return {'id':key,'question':'question','depends_on':[], 'relation_types':['HAS_DONOR'],
                'constraints':[{'property':'t1d_stage','operator':'=','value':value,'entity_type':'donor'}]}
    parent={'steps':[step('old_stage','Stage 3')]}
    plan={'steps':[step('s1','Stage 3'),step('s2','Stage 2')], 'answer_step_ids':['s2']}
    rewritten = reuse_step_ids(plan,parent)
    assert rewritten['steps'][0]['id']=='old_stage'
    assert rewritten['steps'][1]['id']!='old_stage'
    assert rewritten['answer_step_ids']==['s2']


def test_aggregate_profile_schema_names_are_not_edge_records():
    from pankagent_vnext.output_scope import project
    payload = {'nodes':[{'id':'private-donor','labels':['donor']}],
        'edges':[{'start_id':'private-donor','end_id':'sample','type':'HAS_SAMPLE'}],
        'answer_profile':{'unknown_schema':{'edges':['HAS_DONOR']}}}
    result = project(payload)
    assert result['edges'] == []
    assert result['answer_profile']['unknown_schema']['edges'] == ['HAS_DONOR']
    assert 'private-donor' not in json.dumps(result)


def test_connected_request_cannot_silently_become_independent_lookup():
    from pankagent_vnext.llm import plan_structure_issue
    assert plan_structure_issue({'interpreted_question': 'Find connected five-node chains from a gene.',
        'steps': [{'id': 's1', 'question': 'Find partners', 'depends_on': [], 'constraints': [],
                   'relation_types': ['PHYSICAL_INTERACTION']}], 'clarification': None}) == 'connected_chain_requires_path_fragments_and_final_join'


def test_disabled_literature_wording_preserves_graph_request():
    from pankagent_vnext.planning_fastpath import literature_request_plan
    assert literature_request_plan('Show INS detection with literature evidence disabled.') is None


def test_join_refuses_nonpath_evidence_instead_of_claiming_empty():
    parents = {'a': evidence('a', ['A'], 'Gene'), 'b': evidence('b', ['A'], 'Gene')}
    assert combine(operation('join', label='Gene'), parents)['status'] == 'blocked'


def test_reverse_edge_spelling_is_losslessly_canonicalized():
    step = four_node_step()
    edge = step['path_spec']['edges'][1]
    edge['from'], edge['to'] = edge['to'], edge['from']
    edge['direction'] = {'out': 'in', 'in': 'out', 'either': 'either'}[edge['direction']]
    plan = normalize({'steps': [step]})
    assert plan['steps'][0]['path_spec'] == four_node_step()['path_spec']


def test_final_join_cannot_drop_ancestor_fragment():
    a = four_node_step(); a['id'] = 'a'
    b = deepcopy(a); b['id'] = 'b'; b['depends_on'] = ['a']
    c = deepcopy(a); c['id'] = 'c'; c['depends_on'] = ['b']
    op = operation('join', label='Gene')['operation']
    op['inputs'][0]['step_id'] = 'b'; op['inputs'][1]['step_id'] = 'c'
    with pytest.raises(ValueError, match='all_ancestor_path'):
        normalize({'steps': [a,b,c], 'combine_operations': [op], 'answer_step_ids': ['combined']})


def test_complete_path_draft_compiles_fragments_and_cumulative_joins():
    from pankagent_vnext.chain_drafting import expand
    nodes = [{'role': f'n{i}', 'entity_types': ['Gene']} for i in range(8)]
    edges = [{'role': f'e{i}', 'from': f'n{i}', 'to': f'n{i+1}', 'types_any': ['PHYSICAL_INTERACTION'], 'direction': 'either'} for i in range(7)]
    plan = normalize(expand({'interpreted_question': 'Connected path', 'clarification': None,
        'chain_spec': {'version': 'bounded-path-v1', 'nodes': nodes, 'edges': edges},
        'constraints': [{'owner_role': 'n0', 'entity_type': 'Gene', 'property': 'id', 'operator': '=', 'value': 'A'}]}))
    assert len([s for s in plan['steps'] if s.get('path_spec')]) == 3
    assert plan['combine_operations'][1]['inputs'][0]['step_id'] == 'join1'
    assert plan['answer_step_ids'] == ['join2']
    assert all(plan_issue(s) is None for s in plan['steps'])


def test_source_article_and_unrestricted_stage_wording():
    from pankagent_vnext.semantic_registry import dataset_source_owner, _unresolved_source_role, scope_intent_text
    q = 'Count donors from the HPAP data source regardless of recorded T1D stage.'
    assert dataset_source_owner(q, 'HPAP') == 'donor'
    assert not _unresolved_source_role(q, {'sources': ['HPAP']})
    assert 'T1D' not in scope_intent_text(q)


def test_stage_identity_does_not_conflict_with_separate_negative_clinical_filter():
    from pankagent_vnext.semantic_registry import resolve, STAGES
    q = 'Find HPAP donors with recorded T1D stage 3 but do not have diagnosed type 1 diabetes.'
    vocabulary = {'stages': list(STAGES.values()), 'sources': ['HPAP'], 'modalities': [],
        'donor_categories_complete': True, 'donor_categorical_values': {'diabetes_type': ['T1D', 'ND']},
        'inventory_sha256': 'verified-test-inventory'}
    step = {'id': 's1', 'question': q, 'relation_types': ['HAS_DONOR'], 'constraints': [],
        'semantic_request': {'source': 'user_request', 'question': q}}
    result = resolve(step, vocabulary, 'PanKgraph_08_04')
    assert not result['semantic_issues']
    filters = {(c['property'], c['operator'], c['value']) for c in result['constraints']}
    assert ('diabetes_type', '!=', 'T1D') in filters
    assert ('t1d_stage', '=', STAGES['3']) in filters
    assert ('data_source', '=', 'HPAP') in filters
