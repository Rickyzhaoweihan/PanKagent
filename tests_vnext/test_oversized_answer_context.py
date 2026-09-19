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


def test_final_serialized_body_overflow_also_uses_node_only_fallback(monkeypatch, tmp_path):
    async def scenario():
        gateway, _, _ = gateway_with_mock(monkeypatch, tmp_path, [])
        import pankagent_vnext.llm as llm
        normal = compact_evidence(detection_evidence())
        normal[0]['large_post_compaction_field'] = 'PRIVATE_ENVELOPE' * 8000
        monkeypatch.setattr(llm, 'compact_evidence', lambda _: normal)
        try:
            prepared = gateway.prepare_answer('多' * 6000, detection_evidence())
            assert len(prepared.body.encode()) <= MAX_BYTES
            assert 'PRIVATE_ENVELOPE' not in prepared.body
            assert prepared.profile['model_context']['mode'] == 'node_identity_only'
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
