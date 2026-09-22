from copy import deepcopy
import time

import pytest

from pankagent_vnext.signal_membership import summarize_signal_membership
from pankagent_vnext.release_schema import REGISTRY

RELEASE = REGISTRY['release']


def source(*edges):
    labels = {edge['start_id']: ['sequence_variant', 'variants'] for edge in edges}
    labels.update({edge['end_id']: ['disease'] if edge['type'] == 'PART_OF_GWAS_SIGNAL' else ['Gene'] for edge in edges})
    return {'step_id': 's1', 'graph_version': RELEASE, 'status': 'complete', 'truncated': False,
        'nodes': [{'id': key, 'labels': value} for key, value in labels.items()], 'edges': list(edges), 'rows': []}


def qtl(gene='ENSG00000001084', count=29, **extra):
    return {'id': gene + '-e1', 'type': 'PART_OF_QTL_SIGNAL', 'start_id': 'rs587937', 'end_id': gene,
        'properties': {'credible_set': 'credibleSet1', 'data_source': 'GTEx; SusieR', 'data_version': 'v1.0',
            'tissue_id': 'UBERON_0001264', 'tissue_name': 'Pancreas', 'n_snp': count, **extra}}


def test_one_indexed_edge_does_not_become_a_single_variant_credible_set():
    item = source(qtl())
    original = deepcopy(item)
    result = summarize_signal_membership(item)
    assert item == original
    assert result['by_relation']['PART_OF_QTL_SIGNAL']['retrieved_record_count'] == 1
    assert result['by_relation']['PART_OF_QTL_SIGNAL']['unique_indexed_variant_ids'] == 1
    assert result['records'][0]['source_reported_count'] == 29
    assert result['records'][0]['source_count_property'] == 'n_snp'
    assert result['complete_credible_set_membership_verified'] is False
    assert 'One returned edge does not mean a single-variant credible set' in result['interpretation']


def test_source_counts_remain_per_gene_and_tissue_never_summed_or_merged():
    a = qtl(); b = qtl('ENSG00000001167', 78)
    b['start_id'] = 'rs139649738'
    c = qtl(count=10, tissue_id='UBERON_0000006', tissue_name='Islet', data_source='exon; INSPIRE')
    c['id'] = 'islet'; c['start_id'] = 'rs84933'
    result = summarize_signal_membership(source(a, b, c))
    assert [r['source_reported_count'] for r in result['records']] == [29, 78, 10]
    assert [r['target_id'] for r in result['records']] == [a['end_id'], b['end_id'], c['end_id']]
    assert [r['recorded_signal_context']['tissue_id'] for r in result['records']] == ['UBERON_0001264', 'UBERON_0001264', 'UBERON_0000006']
    assert result['by_relation']['PART_OF_QTL_SIGNAL']['source_counts_are_per_record_not_summed']
    assert all(r['globally_unique_signal_identity_verified'] is False for r in result['records'])


def test_gwas_uses_recorded_credible_set_size_and_preserves_nonlead_role():
    edge = {'id': 'gwas', 'type': 'PART_OF_GWAS_SIGNAL', 'start_id': 'rs13393590', 'end_id': 'MONDO_0005147',
        'properties': {'credible_set_id': 'ADCY3__credibleSet1', 'credible_set_size': 102,
            'data_source': 'HIRN', 'data_version': 'v1', 'method': 'fine-mapping', 'lead_status': 'nonlead'}}
    record = summarize_signal_membership(source(edge))['records'][0]
    assert record['source_count_property'] == 'credible_set_size'
    assert record['source_reported_count'] == 102
    assert record['recorded_lead_status'] == 'nonlead'
    assert record['complete_credible_set_membership_verified'] is False


@pytest.mark.parametrize('value', [None, -1, 1.2, True, 'many', '²', float('inf')])
def test_invalid_or_missing_counts_never_become_zero_or_row_count(value):
    record = summarize_signal_membership(source(qtl(count=value)))['records'][0]
    assert record['source_reported_count'] is None
    assert record['source_count_state'] == 'missing_or_invalid'


@pytest.mark.parametrize('value, expected', [('29', 29), (29.0, 29), (0, 0), (1, 1)])
def test_recorded_count_is_preserved_without_inventing_membership_completeness(value, expected):
    record = summarize_signal_membership(source(qtl(count=value)))['records'][0]
    assert record['source_reported_count'] == expected
    assert record['source_reported_count_raw'] == value
    assert record['complete_credible_set_membership_verified'] is False


def test_missing_context_or_wrong_endpoints_remains_unknown():
    item = source(qtl(data_source=None))
    result = summarize_signal_membership(item)
    assert result['records'][0]['recorded_context_state'] == 'incomplete_or_unverified'
    item['nodes'][0]['labels'] = ['Gene']
    result = summarize_signal_membership(item)
    assert result['by_relation']['PART_OF_QTL_SIGNAL']['unique_indexed_variant_ids'] is None
    assert result['records'][0]['indexed_variant_id'] is None


def test_duplicate_inputs_and_excerpt_cap_do_not_change_full_retrieved_totals():
    edges = [qtl('GENE' + str(index), index + 2) for index in range(10)]
    result = summarize_signal_membership(source(*edges, deepcopy(edges[0])), max_records=2)
    assert result['input_record_count'] == 11
    assert result['retrieved_record_count'] == 10
    assert result['duplicate_input_records'] == 1
    assert len(result['records']) == 2 and result['summary_omitted_record_count'] == 8
    assert result['by_relation']['PART_OF_QTL_SIGNAL']['unique_indexed_variant_ids'] == 1


def test_unknown_release_and_non_signal_evidence_do_not_acquire_metadata():
    item = source(qtl()); item['graph_version'] = 'unknown'
    assert summarize_signal_membership(item) is None
    item = source(qtl()); item['edges'][0]['type'] = 'PHYSICAL_INTERACTION'
    assert summarize_signal_membership(item) is None


def test_summary_reaches_model_excerpt_and_preserves_full_evidence():
    from pankagent_vnext.evidence_context import compact_evidence, scientific_excerpt
    item = source(qtl())
    original = deepcopy(item)
    result = scientific_excerpt(compact_evidence({'s1': item}))
    assert result[0]['signal_membership_summary']['records'][0]['source_reported_count'] == 29
    assert result[0]['signal_membership_summary']['complete_credible_set_membership_verified'] is False
    assert item == original


def test_small_summary_adds_no_inference_and_stays_fast():
    item = source(*[qtl('GENE' + str(index), index + 2) for index in range(100)])
    started = time.monotonic()
    for _ in range(10):
        summarize_signal_membership(item)
    assert (time.monotonic() - started) / 10 < .05
