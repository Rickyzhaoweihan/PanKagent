import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pankagent_vnext.measurement_scope import measurement_scope_recovery, RELEASE


def request():
    return {'id': 's1', 'question': 'Compare beta-cell gene expression by donor age',
            'relation_types': ['GENE_DETECTED_IN', 'HAS_DONOR'],
            'constraints': [{'entity_type': 'Gene', 'property': 'name', 'value': 'CHRNA3'},
                            {'entity_type': 'donor', 'property': 'age', 'operator': '>', 'value': '55'}]}


def test_unsupported_stratification_preserves_filters_and_only_proposes_a_change():
    step = request(); original = deepcopy(step)
    issue = measurement_scope_recovery(step, RELEASE)
    assert step == original
    assert issue['category'] == 'unsupported_expression_stratification'
    assert issue['retryable'] is False
    assert issue['evidence']['preserved_donor_filters'] == [step['constraints'][1]]
    assert 'does not answer the original' in issue['suggestions'][0]['instruction']


@pytest.mark.parametrize('field', ['age', 'sex_at_birth', 'hla_typing', 't1d_stage', 'hba1c_percentage', 'id'])
def test_other_donor_dimensions_have_same_grain_limit(field):
    step = request(); step['constraints'][1]['property'] = field
    assert measurement_scope_recovery(step, RELEASE)


def test_supported_metadata_lookup_and_existing_disease_comparison_are_unchanged():
    step = request(); step['relation_types'] = ['HAS_DONOR', 'HAS_SAMPLE']
    assert measurement_scope_recovery(step, RELEASE) is None
    step = request(); step['relation_types'] = ['T1D_DEG_IN']
    step['constraints'][1] = {'entity_type': 'disease', 'property': 'id', 'value': 'MONDO_0005147'}
    assert measurement_scope_recovery(step, RELEASE) is None
    assert measurement_scope_recovery(request(), 'unverified-release') is None


def test_preparation_stops_before_gpu_or_donor_join_and_revision_can_clear_it():
    from pankagent_vnext.graph import GraphAdapter
    adapter = object.__new__(GraphAdapter)
    adapter.settings = SimpleNamespace(graph_version=RELEASE)
    adapter.semantic_vocabulary = AsyncMock(side_effect=AssertionError('No metadata join needed'))
    adapter._resolve_constraint = AsyncMock(side_effect=AssertionError('No lookup needed'))
    adapter._resolution_signature = lambda step: 'fixture-signature'
    prepared = asyncio.run(adapter._prepare_step(request(), AsyncMock()))
    assert prepared['entity_resolution']['state'] == 'needs_clarification'
    assert prepared['constraints'] == request()['constraints']
    adapter.semantic_vocabulary.assert_not_called()
    adapter._resolve_constraint.assert_not_called()
    revised = deepcopy(prepared)
    revised['constraints'] = [revised['constraints'][0]]
    assert measurement_scope_recovery(revised, RELEASE) is None
