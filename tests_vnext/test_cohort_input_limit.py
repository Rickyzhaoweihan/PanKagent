from copy import deepcopy
from pankagent_vnext.evidence_context import compact_evidence, node_only_evidence
from test_evidence_context import node, edge, evidence


def test_cohort_examples_max_100_with_full_counts_and_short_parallel_measurement():
    samples = [node(str(i), 'Sample_node', annotation='x' * 1000) for i in range(200)]
    large = evidence([node('d', 'donor'), *samples],
        [edge('d', str(i), 'HAS_SAMPLE') for i in reversed(range(200))], step_id='large')
    small = evidence([node('g'), node('c', 'anatomical_structure')],
        [edge('g', 'c', 'GENE_ENRICHED_IN', log2FoldChange=3.2)], step_id='small')
    before = deepcopy(large)
    result = compact_evidence([large, small], max_bytes=150_000)
    first = result[0]
    assert len(first['nodes']) <= 100
    assert first['evidence_totals']['distinct_nodes_by_label']['Sample_node'] == 200
    assert first['status'] == 'complete' and first['record_selection']['additional_records_omitted'] > 0
    visible = {n['id'] for n in first['nodes']}
    assert all(e['start_id'] in visible and e['end_id'] in visible for e in first['edges'])
    assert result[1]['edges'][0]['properties']['log2FoldChange'] == 3.2
    assert large == before
    identities = node_only_evidence([large])[0]
    assert len(identities['nodes']) <= 100
    assert identities['context_dropped']['nodes'] >= 101


def test_100_fallback_is_not_applied_to_small_results_and_metadata_reaches_llm():
    from pankagent_vnext.evidence_context import scientific_excerpt
    small = evidence([node(str(i), 'Sample_node') for i in range(150)])
    original = deepcopy(small)
    result = node_only_evidence([small])[0]
    assert len(result['nodes']) == 150
    assert 'result_size_fallback' not in result
    assert small == original
    big = deepcopy(small)
    for n in big['nodes']:
        n['properties']['description'] = 'detail ' * 200
    result = node_only_evidence([big])[0]
    assert len(result['nodes']) <= 100
    metadata = scientific_excerpt([result])[0]['result_size_fallback']
    assert metadata['observed_result_bytes'] > metadata['formatter_budget_bytes']
    assert metadata['backend_results_preserved']
    assert 'Do not count' in metadata['llm_instruction']
    assert result['context_dropped']['nodes'] == 150 - len(result['nodes'])


def test_oversized_incomplete_result_does_not_gain_complete_counts():
    big = evidence([node(str(i), 'Sample_node', description='x' * 2000) for i in range(150)],
                   status='partial', truncated=True)
    result = node_only_evidence([big])[0]
    assert result['status'] == 'partial' and result['truncated']
    assert result['result_size_fallback']['example_limit'] == 100
    assert 'answer_facts' not in result


def test_chain_cap_is_conditional_and_keeps_whole_verified_paths():
    from pankagent_vnext.format_input_modes import add_chain_identities
    path = {'nodes': [{'id': 'a', 'role': 'donor', 'labels': ['donor']},
                      {'id': 'b', 'role': 'sample', 'labels': ['Sample_node']}],
            'edges': [{'role': 'link', 'type': 'HAS_SAMPLE', 'start_id': 'a',
                       'end_id': 'b', 'fingerprint': 'record1'}]}
    small = {'path_records': [deepcopy(path) for _ in range(150)], 'status': 'complete'}
    result = add_chain_identities({}, small, 150_000)
    assert len(result['identity_path_records']) == 150
    assert 'result_size_fallback' not in result
    big = deepcopy(small)
    for p in big['path_records']:
        p['properties'] = {'bulky_annotation': 'x' * 2000}
    result = add_chain_identities({}, big, 150_000)
    assert len(result['identity_path_records']) == 100
    assert result['identity_path_records'][0] == path
    assert result['path_input_scope']['omitted_path_count'] == 50
    assert result['path_input_scope']['retrieval_complete'] is True
    assert result['result_size_fallback']['backend_results_preserved'] is True
