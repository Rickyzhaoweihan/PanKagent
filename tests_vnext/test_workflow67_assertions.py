"""Acceptance assertions must not confuse missing evidence with empty targets."""
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('workflow67_eval', ROOT / 'scripts/acceptance/workflow57.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def reference(expected=None, **overrides):
    return {'id': 'Q58', 'core': {'nodes': [], 'edges': []},
            'extra': {'nodes': [], 'edges': []},
            'reference_validation': {'status': 'verified'},
            'assertions': [{'id': 'types', 'kind': 'distinct_values',
                'reference_status': 'verified', 'selector': {'row_fields': ['types']},
                'expected_values': ['RNA', 'ATAC'] if expected is None else expected,
                **overrides}]}


def evidence(values, **overrides):
    return {'types': {'step_id': 'types', 'rows': [{'types': values}], 'nodes': [],
                      'edges': [], 'status': 'complete', 'truncated': False, **overrides}}


def test_complete_distinct_values_and_cardinality_are_typed_and_deduplicated():
    ref = reference()
    ref['assertions'].append({**ref['assertions'][0], 'id': 'count',
                              'kind': 'cardinality', 'expected_count': 2})
    scored = module.coverage(ref, evidence(['RNA', 'ATAC', 'RNA']))
    assert scored['core_covered']
    assert [check['actual_count'] for check in scored['assertions']] == [2, 2]
    assert not module.coverage(reference([1]), evidence([True]))['core_covered']


def test_missing_or_extra_category_is_a_failure_and_answer_prose_is_not_evidence():
    assert not module.coverage(reference(), evidence(['RNA']))['core_covered']
    assert not module.coverage(reference(), evidence(['RNA', 'ATAC', 'OTHER']))['core_covered']
    run = {'graph_answer': 'RNA and ATAC', 'evidence': {'steps': []}}
    assert not module.evaluate(reference(), run)['verified_core_covered']


def test_unverified_reference_and_failed_or_truncated_results_do_not_pass():
    assert not module.coverage(reference(reference_status='pending_live_verification'),
                               evidence(['RNA', 'ATAC']))['core_covered']
    for fields in ({'truncated': True}, {'status': 'partial'}, {'status': 'failed'},
                   {'error': {'category': 'timeout'}}):
        assert not module.coverage(reference(), evidence(['RNA', 'ATAC'], **fields))['core_covered']


def test_empty_expected_set_requires_executed_complete_typed_evidence():
    ref = reference([])
    assert not module.coverage(ref, {})['core_covered']
    assert module.coverage(ref, evidence([], status='empty'))['core_covered']
    assert not module.coverage(ref, {'s': {'status': 'empty', 'truncated': False,
                                          'rows': [], 'nodes': [], 'edges': []}})['core_covered']


def test_node_properties_keep_donor_and_sample_sources_separate():
    ref = reference(['study'], selector={'node_property': {'label': 'donor', 'property': 'data_source'}})
    nodes = [{'id': 's', 'labels': ['Sample_node'], 'properties': {'data_source': 'study'}}]
    assert not module.coverage(ref, evidence([], nodes=nodes))['core_covered']
    nodes.append({'id': 'd', 'labels': ['donor'], 'properties': {'data_source': 'study'}})
    assert module.coverage(ref, evidence([], nodes=nodes))['core_covered']


def test_generic_scalar_alias_requires_typed_projection_provenance():
    ref = reference(['study'], selector={'node_property': {'label': 'donor', 'property': 'data_source'}})
    actual = evidence([], rows=[{'value': 'study'}], queries=[
        {'cypher': 'MATCH (d:donor) RETURN DISTINCT d.data_source AS value'}])
    assert module.coverage(ref, actual)['core_covered']
    actual['types']['queries'][0]['cypher'] = 'MATCH (s:Sample_node) RETURN s.data_source AS value'
    assert not module.coverage(ref, actual)['core_covered']
    actual['types']['queries'][0]['cypher'] = 'MATCH (d:donor) RETURN collect(DISTINCT d.data_source) AS value'
    actual['types']['rows'] = [{'value': ['study']}]
    assert module.coverage(ref, actual)['core_covered']
    actual['types']['queries'][0]['cypher'] = 'MATCH (d:donor) WHERE d.data_source=$source RETURN d.id'
    actual['types']['rows'] = []
    assert not module.coverage(reference([], selector=ref['assertions'][0]['selector']), actual)['core_covered']


def test_modality_identities_are_an_alternative_to_scalar_projections():
    ref = reference(['RNA'], selector={'node_identity': {'label': 'data_modality', 'field': 'id'}})
    assert module.coverage(ref, evidence([], nodes=[{'id': 'RNA', 'labels': ['data_modality']}]))['core_covered']


def tissue_reference(expected):
    return reference(expected, selector={'row_fields': ['name'], 'node_property': {
        'label': 'anatomical_structure', 'property': 'name', 'where': {'category': 'tissue'}}})


def tissue_rows(rows, query=None):
    return evidence([], rows=rows, queries=[{'cypher': query or (
        'MATCH (a:anatomical_structure)-[:HAS_SAMPLE]->(s:Sample_node) '
        'RETURN DISTINCT a.name AS name, a.category AS category')}])


def test_filtered_scalar_values_use_same_owner_category_projections():
    rows = [{'name': 'spleen', 'category': 'tissue'}, {'name': 'beta cell', 'category': 'cell_type'}]
    score = module.coverage(tissue_reference(['spleen']), tissue_rows(rows))
    assert score['core_covered']
    assert score['assertions'][0]['actual_values'] == ['spleen']
    bare = tissue_rows([{'a.name': 'spleen', 'a.category': 'tissue'}],
                       'MATCH (a:anatomical_structure) RETURN DISTINCT a.name, a.category')
    assert module.coverage(tissue_reference(['spleen']), bare)['core_covered']


def test_category_alias_or_different_typed_owner_cannot_prove_row_filter():
    rows = [{'name': 'spleen', 'category': 'tissue'}]
    for query in (
        "MATCH (a:anatomical_structure) RETURN a.name AS name, 'tissue' AS category",
        'MATCH (a:anatomical_structure), (b:anatomical_structure) RETURN a.name AS name, b.category AS category',
        'MATCH (a:anatomical_structure) RETURN collect(a.name) AS name, collect(a.category) AS category',
    ):
        assert not module.coverage(tissue_reference(['spleen']), tissue_rows(rows, query))['core_covered']


def test_missing_filter_value_cannot_be_hidden_by_other_complete_rows():
    for row in ({'name': 'unknown'}, {'category': 'tissue'},
                {'name': 'unknown', 'category': ['tissue']}):
        rows = [{'name': 'spleen', 'category': 'tissue'}, row]
        score = module.coverage(tissue_reference(['spleen']), tissue_rows(rows))
        assert not score['core_covered']
        assert not score['assertions'][0]['complete']


def test_complete_empty_filtered_subset_requires_executed_projection_proof():
    ref = tissue_reference([])
    assert module.coverage(ref, tissue_rows([]))['core_covered']
    assert module.coverage(ref, tissue_rows([{'name': 'beta cell', 'category': 'cell_type'}]))['core_covered']
    assert not module.coverage(ref, {})['core_covered']
    assert not module.coverage(ref, evidence([], rows=[], queries=[]))['core_covered']
    assert not module.coverage(ref, tissue_rows([{'name': 'unknown'}]))['core_covered']


def test_schema_explanation_requires_manual_grounding_review():
    ref = reference(kind='schema_evidence', expected_concepts=['assays', 'genes'])
    score = module.coverage(ref, evidence(['RNA', 'ATAC']))
    assert score['manual_assertion_review']
    assert score['assertions'][0]['pass'] is None
    assert not score['core_covered']


def test_source_slices_do_not_prove_an_exhaustive_graph_source_answer():
    fixture = json.loads((ROOT / 'tests_vnext/fixtures/acceptance/workflow57.json').read_text())
    ref = next(case for case in fixture['cases'] if case['id'] == 'Q62')
    nodes = []
    for check in ref['assertions']:
        if check['kind'] != 'distinct_values':
            continue
        owner = check['selector']['node_property']['label']
        nodes.extend({'id': f'{owner}-{i}', 'labels': [owner],
                      'properties': {'data_source': value}}
                     for i, value in enumerate(check['expected_values']))
    score = module.coverage(ref, evidence([], nodes=nodes))
    assert all(check['pass'] for check in score['assertions'][:2])
    assert score['assertions'][-1]['pass'] is None
    assert score['manual_assertion_review']
    assert not score['core_covered']


def test_grouped_report_cannot_hide_old_regressions_in_new_gains():
    cases = [{'id': 'Q01'}, {'id': 'Q02'}, {'id': 'Q58'}]
    rows = [{'case': 'Q01', 'evaluation': {'verified_core_covered': False}},
            {'case': 'Q58', 'evaluation': {'verified_core_covered': True}}]
    summary = module.group_summary(rows, cases)
    assert summary['original_57']['selected_cases'] == 2
    assert summary['original_57']['attempts'] == 1
    assert summary['original_57']['verified_core_covered'] == 0
    assert summary['introductions_10']['verified_core_covered'] == 1


def test_fixture_has_all_67_cases_and_never_vacuously_scores_unfrozen_additions():
    fixture = json.loads((ROOT / 'tests_vnext/fixtures/acceptance/workflow57.json').read_text())
    assert [case['id'] for case in fixture['cases']] == [f'Q{number:02}' for number in range(1, 68)]
    for case in fixture['cases'][57:]:
        assert not module.coverage(case, {})['core_covered']
