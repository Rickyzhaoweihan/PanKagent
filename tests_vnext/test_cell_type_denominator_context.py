from copy import deepcopy

import pytest

from pankagent_vnext.evidence_context import compact_evidence, scientific_excerpt
from test_evidence_context import node, edge, evidence


def result(count=12, kind='GENE_DETECTED_IN'):
    cells = [node('cell-' + str(i), 'anatomical_structure') for i in range(count)]
    return evidence([node('gene')] + cells,
        [edge('gene', cell['id'], kind, total_cells=500, expression_call='Expressed') for cell in cells],
        requested_scope={'complete': True, 'relation_types': [kind],
                         'constraints': [{'entity_type': 'Gene', 'property': 'id', 'operator': '=', 'value': 'gene'}]})


@pytest.mark.parametrize('kind', ['GENE_DETECTED_IN', 'GENE_ENRICHED_IN', 'MARKER_GENE_OF', 'T1D_DEG_IN'])
def test_returned_cell_type_count_is_never_a_source_study_denominator(kind):
    source = result(kind=kind)
    snapshot = deepcopy(source)
    compact = compact_evidence([source])
    value = scientific_excerpt(compact)[0]['evidence_totals']['relationships'][kind]
    assert value['matched_cell_type_count'] == 12
    assert value['source_study_total_cell_types'] is None
    assert value['source_study_denominator_state'] == 'not_established_by_this_query'
    assert 'all profiled cell types' in value['denominator_rule']
    assert source == snapshot


def test_duplicate_records_and_excerpt_selection_do_not_change_matched_cell_count():
    source = result(125)
    source['edges'].append(deepcopy(source['edges'][0]))
    actual = compact_evidence([source])[0]
    value = actual['evidence_totals']['relationships']['GENE_DETECTED_IN']
    assert actual['context_sampled'] is True and len(actual['edges']) == 100
    assert value['records'] == 126 and value['matched_cell_type_count'] == 125
    assert value['source_study_total_cell_types'] is None


def test_unknown_endpoint_label_is_not_silently_counted_as_cell_type():
    source = result()
    source['nodes'][1]['labels'] = []
    actual = compact_evidence([source])[0]['evidence_totals']['relationships']['GENE_DETECTED_IN']
    assert actual['matched_cell_type_count'] is None
    assert actual['matched_cell_type_count_state'] == 'endpoint_types_unverified'


def test_prior_step_same_release_endpoint_types_can_verify_returned_cell_count():
    source = result(1)
    prior = evidence([source['nodes'].pop()])
    actual = compact_evidence([prior, source])[1]['evidence_totals']['relationships']['GENE_DETECTED_IN']
    assert actual['matched_cell_type_count'] == 1


def test_partial_retrieval_keeps_count_scope_and_never_claims_study_coverage():
    source = result(2); source['status'] = 'partial'; source['truncated'] = True
    compact = compact_evidence([source])[0]
    assert compact['status'] == 'partial' and compact['truncated'] is True
    actual = compact['evidence_totals']['relationships']['GENE_DETECTED_IN']
    assert actual['matched_cell_type_count'] == 2
    assert actual['matched_cell_type_count_scope'] == 'all_returned_records_before_excerpt_selection'
    assert actual['source_study_total_cell_types'] is None
