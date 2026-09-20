"""Oversized result regression fixtures are synthetic, with no live inference."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path

from pankagent_vnext.evidence_context import MAX_BYTES, compact_evidence, node_only_evidence, scientific_excerpt
from tests_vnext.test_answer_synthesis import detection_evidence, gateway_with_mock


def large_evidence():
    result = {}
    for i in range(12):
        step = copy.deepcopy(detection_evidence()['s1'])
        step['step_id'] = f's{i}'
        step['nodes'] = [
            {'id': f'gene-{i}-{n}', 'labels': ['Gene'], 'properties': {
                'description': 'Synthetic source description. ' * 100,
                'data_source': 'Synthetic fixture', 'data_version': 'fixture-v1',
                'data_source_url': 'https://example.org/fixture',
                'hidden_measurement': 'PRIVATE_NODE_MEASUREMENT',
                **{f'field-{k}': 'x' * 1024 for k in range(20)},
            }} for n in range(70)
        ]
        step['edges'] = [{'start_id': step['nodes'][0]['id'], 'end_id': step['nodes'][1]['id'],
                          'type': 'PHYSICAL_INTERACTION', 'properties': {'measurement': 'PRIVATE_EDGE_MEASUREMENT'}}] * 101
        step['rows'] = [{'measurement': 'PRIVATE_ROW_MEASUREMENT'}] * 40
        step['donor_summary'] = {'unique_donors': 500, 'private': 'PRIVATE_DONOR_SUMMARY'}
        result[step['step_id']] = step
    return result


def test_large_preparation_is_bounded_and_discloses_only_four_node_fields(monkeypatch, tmp_path):
    async def scenario():
        gateway, fake, _ = gateway_with_mock(monkeypatch, tmp_path, [])
        evidence = large_evidence()
        before = copy.deepcopy(evidence)
        try:
            prepared = gateway.prepare_answer('Tell me about this gene.', evidence)
            body = json.loads(prepared.body)
            assert len(prepared.body.encode()) <= MAX_BYTES
            assert prepared.profile['model_context']['mode'] == 'node_identity_only'
            assert prepared.profile['model_context']['query_too_broad'] is True
            assert prepared.profile['model_context']['exposed_node_fields'] == ['id', 'type', 'description', 'source']
            assert len(body['evidence']) == 12
            for step in body['evidence']:
                assert not {'edges', 'rows', 'answer_facts', 'donor_summary', 'evidence_totals'} & step.keys()
                for node in step['nodes']:
                    assert set(node) == {'id', 'type', 'description', 'source'}
                    assert node['source']['data_source'] == 'Synthetic fixture'
                    assert node['source']['data_version'] == 'fixture-v1'
            assert 'PRIVATE_' not in prepared.body
            contract = prepared.system[-1]['text']
            assert 'query is too broad' in contract and 'more specific query' in contract
            assert 'Do not infer associations' in contract
            assert not any('Matched interpretation guidance' in block['text'] for block in prepared.system)
            assert fake.stream_calls == fake.create_calls == []
            assert evidence == before
        finally:
            await gateway.close()
    asyncio.run(scenario())


def test_normal_answer_retains_measurements_and_has_no_oversize_instruction(monkeypatch, tmp_path):
    async def scenario():
        gateway, _, _ = gateway_with_mock(monkeypatch, tmp_path, [])
        evidence = detection_evidence()
        try:
            prepared = gateway.prepare_answer('Where is INS detected?', evidence)
            result = json.loads(prepared.body)['evidence'][0]
            assert result['edges'][0]['properties'] == evidence['s1']['edges'][0]['properties']
            assert prepared.profile['model_context']['query_too_broad'] is False
            assert not any('Oversized-result contract' in b['text'] for b in prepared.system)
        finally:
            await gateway.close()
    asyncio.run(scenario())


def test_final_serialized_body_overflow_recompacts_without_global_downgrade(monkeypatch, tmp_path):
    async def scenario():
        gateway, _, _ = gateway_with_mock(monkeypatch, tmp_path, [])
        import pankagent_vnext.llm as llm
        normal = compact_evidence(detection_evidence())
        normal[0]['large_post_compaction_field'] = 'PRIVATE_ENVELOPE' * 8000
        monkeypatch.setattr(llm, 'compact_evidence', lambda value, **kw: compact_evidence(value, **kw) if kw else normal)
        try:
            prepared = gateway.prepare_answer('多' * 6000, detection_evidence())
            assert len(prepared.body.encode()) <= MAX_BYTES
            assert 'PRIVATE_ENVELOPE' not in prepared.body
            assert prepared.profile['model_context']['mode'] == 'standard'
        finally:
            await gateway.close()
    asyncio.run(scenario())


def test_oversized_identifiers_are_omitted_whole_and_rare_types_survive():
    evidence = {'s1': {'status': 'partial', 'truncated': True, 'nodes': [
        {'id': 'g' * (MAX_BYTES + 1), 'labels': ['Gene'], 'properties': {}},
        *[{'id': f'g-{i}', 'labels': ['Gene'], 'properties': {'description': '多' * 5000}} for i in range(250)],
        {'id': 'rare', 'labels': ['GO_term'], 'properties': {'data_source': 'fixture'}},
    ], 'edges': [], 'rows': []}, 's2': {'status': 'failed', 'nodes': [], 'edges': [], 'rows': []}}
    first = node_only_evidence(evidence, max_bytes=10000)
    assert first == node_only_evidence(evidence, max_bytes=10000)
    assert len(json.dumps(first, ensure_ascii=False, separators=(',', ':')).encode()) <= 10000
    assert any(n['id'] == 'rare' for n in first[0]['nodes'])
    assert all(len(n['id']) < MAX_BYTES for n in first[0]['nodes'])
    assert first[0]['context_dropped']['nodes'] > 0
    view = scientific_excerpt(first)
    assert view[0]['truncated'] is True and view[1]['status'] == 'failed'
    assert view[0]['answer_evidence_scope']['relationship_and_measurement_evidence_available'] is False


def test_fallback_prompt_is_pinned_in_application_bundle():
    root = Path(__file__).resolve().parents[1] / 'pankagent_vnext' / 'answer_skills'
    manifest = json.loads((root / 'manifest.json').read_text())
    path = manifest['oversized_result_contract']['path']
    assert manifest['sha256'][path] == hashlib.sha256((root / path).read_bytes()).hexdigest()
    assert manifest['oversized_result_contract']['scope'] == 'application_local_node_only_fallback_not_shared_kg_standard'


def test_node_only_stream_never_uses_full_evidence_text_rewrites(monkeypatch, tmp_path):
    async def scenario():
        text = 'This query is too broad for a detailed answer; try a more specific query. [G1]'
        gateway, fake, _ = gateway_with_mock(monkeypatch, tmp_path, [text])
        def forbidden(*_):
            raise AssertionError('Full-evidence text rewriting is forbidden in node-only mode')
        monkeypatch.setattr('pankagent_vnext.llm.ScopeTextFilter', forbidden)
        evidence = large_evidence()
        try:
            prepared = gateway.prepare_answer('Tell me about this gene.', evidence)
            answer = ''.join([part async for part in gateway.synthesize('Tell me about this gene.', evidence, prepared=prepared)])
            assert answer == text
            assert len(fake.stream_calls) == 1 and not fake.create_calls
        finally:
            await gateway.close()
    asyncio.run(scenario())


def test_empty_and_failed_check_categories_remain_distinguishable():
    source = [{'step_id': 's1', 'status': 'failed', 'title': 'Check pancreatic QTLs',
               'requested_scope': {'relation_types': ['PART_OF_QTL_SIGNAL']}, 'nodes': []},
              {'step_id': 's2', 'status': 'empty', 'title': 'Check T1D GWAS',
               'requested_scope': {'relation_types': ['PART_OF_GWAS_SIGNAL']}, 'nodes': []}]
    result = scientific_excerpt(node_only_evidence(source))
    assert result[0]['check']['relation_types'] == ['PART_OF_QTL_SIGNAL']
    assert result[1]['check']['relation_types'] == ['PART_OF_GWAS_SIGNAL']
    assert [s['status'] for s in result] == ['failed', 'empty']
    assert all(not s['answer_evidence_scope']['relationship_and_measurement_evidence_available'] for s in result)


def test_runtime_failure_retains_requested_category_for_limited_view():
    from pankagent_vnext.app import Runtime
    step = {'id': 's1', 'question': 'Check pancreatic QTLs', 'relation_types': ['PART_OF_QTL_SIGNAL']}
    failed = Runtime.failed_step(step, {'category': 'graph_unavailable'})
    step['relation_types'].clear()
    result = scientific_excerpt(node_only_evidence([failed]))
    assert result[0]['status'] == 'failed'
    assert result[0]['check']['relation_types'] == ['PART_OF_QTL_SIGNAL']


def test_oversized_source_identifiers_are_omitted_not_fabricated():
    value = {'nodes': [{'id': 'g', 'labels': ['Gene'], 'properties': {
        'description': {'private_measurement': 100},
        'data_source': 'fixture', 'data_source_url': 'https://example.org/' + 'x' * 1000,
        'data_version': 'v' * 1000}}]}
    result = node_only_evidence([value])[0]
    node = result['nodes'][0]
    assert node['description'] is None and node['source'] == {'data_source': 'fixture'}
    assert result['context_content_omissions']['omitted_oversized_source_identities'] == 2


def test_source_identity_lists_omit_long_members_without_clipping_valid_members():
    properties = {'data_source_url': ['https://example.org/' + 'x' * 1000, 'https://example.org/valid'],
                  'data_version': ('v' * 1000, 'v1')}
    result = node_only_evidence([{'nodes': [{'id': 'g', 'labels': ['Gene'], 'properties': properties}]}])[0]
    assert result['nodes'][0]['source'] == {'data_source_url': ['https://example.org/valid'], 'data_version': ['v1']}
    assert result['context_content_omissions']['omitted_oversized_source_identities'] == 2
