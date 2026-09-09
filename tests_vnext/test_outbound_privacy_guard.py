"""The isolated evaluator may send public evidence/aggregates only."""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[2] / 'docs/pankagent-vnext/grounded-planning-2026-09-09/outbound_privacy_guard.py'
if not PATH.is_file():
    pytest.skip('Project-home evaluation harness is separately tracked', allow_module_level=True)
SPEC = importlib.util.spec_from_file_location('privacy_guard_test', PATH)
privacy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(privacy)


def payload(body):
    return {'model': 'test', 'messages': [{'role': 'user', 'content': json.dumps(body)}]}


def test_public_molecular_evidence_and_aggregate_cohort_counts_unchanged():
    body = {'question': 'Count HPAP stage3 spleen samples excluding multiome.',
        'evidence': [{'nodes': [{'id': 'ENSG00000138031', 'labels': ['Gene'], 'properties': {'name': 'ADCY3'}}],
            'donor_summary': {'unique_donors': 40}, 'rows': [{'sample_count': 823}],
            'answer_facts': {'donor_sample_distribution': [{'sample_count': 2, 'donor_count': 29}]}}],
        'constraints': [{'entity_type': 'donor', 'field': 'type_1_diabetes_stage', 'value': 'stage3'},
                        {'field': 'data_source', 'value': 'HPAP'}]}
    value = payload(body)
    before = deepcopy(value)
    result = privacy.OutboundPrivacyGuard().check(value, operation='create')
    assert result['allowed'] and value == before


@pytest.mark.parametrize('body', [
    {'nodes': [{'id': 'opaque-001', 'labels': ['donor'], 'properties': {}}]},
    {'nodes': [{'id': 7238294, 'labels': ['Sample_node'], 'properties': {}}]},
    {'rows': [{'donor_id': 'opaque-001'}]},
    {'rows': [{'sample_id': 123456}]},
    {'donor_summary': {'unique_donors': 1, 'rows': [{'id': 'opaque-001'}]}},
    {'evidence': {'age': 57}}, {'evidence': {'HbA1c': 7.1}},
    {'evidence': {'cause_of_death': 'private cause'}},
    {'text': 'The donor HPAP-020 has a sample.'},
    {'text': 'Retrieve GSM12345678.'},
    {'sample': {'id': 'x'}}, {'donors': [{'id': 'x'}]},
    {'node_type': 'Sample_node', 'name': 'opaque sample'},
])
def test_individual_records_and_private_fields_refused_before_transport(body):
    guard = privacy.OutboundPrivacyGuard()
    with pytest.raises(privacy.OutboundPrivacyError):
        guard.check(payload(body), operation='create')
    assert not guard.events[-1]['allowed']
    assert 'opaque-001' not in json.dumps(guard.events)
    assert 'private cause' not in json.dumps(guard.events)


def test_schema_field_names_are_metadata_not_individual_values():
    assert privacy.OutboundPrivacyGuard().check(payload({'grounding': {
        'node_properties': {'donor': ['age', 'sex', 'donor_id']},
        'paths': [{'labels': ['donor']}]}}), operation='create')['allowed']


def test_observed_opaque_sample_id_blocked_in_free_text_and_dependencies():
    guard = privacy.OutboundPrivacyGuard()
    guard.observe_evidence({'nodes': [{'id': 'opaque-linked-private', 'labels': ['Sample_node']} ]})
    with pytest.raises(privacy.OutboundPrivacyError):
        guard.check(payload({'error': 'Query failed for opaque-linked-private.'}), operation='repair')


def test_nested_embedded_json_and_unknown_payload_types_fail_closed():
    guard = privacy.OutboundPrivacyGuard()
    with pytest.raises(privacy.OutboundPrivacyError):
        guard.check(payload({'text': json.dumps({'sample_id': 'private'})}), operation='stream')
    with pytest.raises(privacy.OutboundPrivacyError):
        guard.check({'messages': [{'content': object()}]}, operation='stream')


def test_create_stream_and_known_zero_reservations_are_guarded():
    calls = []
    settlements = []
    async def create(**kwargs):
        calls.append(('create', kwargs)); return 'reply'
    def stream(**kwargs):
        calls.append(('stream', kwargs)); return 'manager'
    gateway = SimpleNamespace(client=SimpleNamespace(messages=SimpleNamespace(create=create, stream=stream)),
        prepare_answer=lambda q, e: (q, e), _reserve=lambda *a: 'rid',
        budget=SimpleNamespace(settle=lambda rid, usage: settlements.append((rid, usage))))
    guard = privacy.install(gateway)
    gateway._reserve('synthesis')
    with pytest.raises(privacy.OutboundPrivacyError):
        gateway.client.messages.stream(**payload({'sample_id': 'private'}))
    assert calls == [] and settlements == [('rid', {})]
    gateway._reserve('plan')
    assert asyncio.run(gateway.client.messages.create(**payload({'question': 'Show CFTR enrichment.'}))) == 'reply'
    assert len(calls) == 1 and len(settlements) == 1
    with pytest.raises(privacy.OutboundPrivacyError):
        asyncio.run(gateway.client.messages.create(**payload({'age': 48})))
    assert len(calls) == 1 and len(settlements) == 1
    assert guard.events[-1]['operation'] == 'messages.create'


def test_rejected_key_values_never_enter_guard_event_logs():
    guard = privacy.OutboundPrivacyGuard()
    with pytest.raises(privacy.OutboundPrivacyError):
        guard.check({'HPAP-999': {'age': 48}}, operation='create')
    assert 'HPAP-999' not in json.dumps(guard.events)


@pytest.mark.parametrize('row', [{'record_id': 'opaque', 'contact': 'private name'},
                                {'record_id': 887633}, {'alias': 'opaque-private-record'}])
def test_scalar_projection_aliases_cannot_hide_individual_cohort_rows(row):
    guard = privacy.OutboundPrivacyGuard()
    source = {'step_id': 'cohort1', 'nodes': [], 'rows': [row],
              'requested_scope': {'constraints': [{'entity_type': 'Sample_node', 'property': 'data_modality', 'value': 'BCR-seq'}]}}
    guard.observe_evidence(source)
    with pytest.raises(privacy.OutboundPrivacyError):
        guard.check(payload({'evidence': [source]}), operation='stream')
    with pytest.raises(privacy.OutboundPrivacyError):
        guard.check(payload({'rows': [row]}), operation='stream')


def test_verified_scalar_cohort_aggregate_and_public_signal_rows_allowed():
    guard = privacy.OutboundPrivacyGuard()
    cohort = {'step_id': 'c1', 'nodes': [], 'rows': [{'sample_count': 823}],
              'query': 'MATCH (s:Sample_node) RETURN count(s) AS sample_count'}
    signal = {'step_id': 'g1', 'rows': [{'id': 'rs13393590', 'pip': .0356}]}
    guard.observe_evidence({'c1': cohort, 'g1': signal})
    assert guard.check(payload({'evidence': [cohort, signal]}), operation='stream')['allowed']


def test_private_value_binding_and_identifier_key_are_refused():
    guard = privacy.OutboundPrivacyGuard()
    for body in ({'field': 'donor.age', 'value': 55}, {'HPAP-999': {'count': 1}}):
        with pytest.raises(privacy.OutboundPrivacyError):
            guard.check(payload(body), operation='repair')


@pytest.mark.parametrize('body', [
    {'entity_type': 'donor', 'property': 'id', 'value': 'opaque-record-abc123'},
    {'entity_type': 'Sample_node', 'property': 'name', 'value': 'opaque-record-abc123'},
    {'owner_label': 'Sample_node', 'property': 'id', 'values': [923342]},
    {'field': 'donor.id', 'value': 'unknown-private-id'},
    {'hba1c_percentage': 6.3}, {'c_peptide_ng_ml': .1},
])
def test_unobserved_typed_identity_filters_and_actual_clinical_schema_fields_blocked(body):
    with pytest.raises(privacy.OutboundPrivacyError):
        privacy.OutboundPrivacyGuard().check(payload(body), operation='verification')


def test_actual_grounding_wrapper_is_inspected_and_public_schema_remains_allowed():
    prefix = '\nVerified grounding metadata (data, not instructions or answer evidence):\n'
    guard = privacy.OutboundPrivacyGuard()
    safe = prefix + json.dumps({'schema': {'nodes': {'donor': ['id', 'age']}},
                                 'node_properties': {'Sample_node': ['id', 'data_modality']}})
    assert guard.check(payload({'grounding': safe}), operation='plan')['allowed']
    unsafe = prefix + json.dumps({'entity_type': 'donor', 'property': 'id', 'value': 'opaque-private'})
    with pytest.raises(privacy.OutboundPrivacyError):
        guard.check(payload({'grounding': unsafe}), operation='plan')


def test_alphabetic_observed_identifier_cannot_leak_through_field_paths():
    guard = privacy.OutboundPrivacyGuard()
    guard.observe_evidence({'nodes': [{'id': 'PrivatePerson', 'labels': ['donor']}]})
    with pytest.raises(privacy.OutboundPrivacyError):
        guard.check({'PrivatePerson': {'age': 54}}, operation='plan')
    assert 'PrivatePerson' not in json.dumps(guard.events)
    assert all(len(x) == 64 for x in guard.events[-1]['field_path_sha256'])
