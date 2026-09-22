from copy import deepcopy
from types import SimpleNamespace
import asyncio

import pytest

from pankagent_vnext.query_templates import compile_query
from pankagent_vnext.graph import GraphAdapter
from pankagent_vnext.semantic_registry import (
    STAGES, _positive_request_phrase, _property_owner_is_unambiguous,
    _raw_constraint_authorized, resolve)


RELEASE = 'PanKgraph_08_04'
QUESTION = ('How many spleen scRNA-seq (or snMultiomics, which includes an RNA component) '
            'samples are available from HPAP donors who are recorded as ND/healthy '
            '(not type 1 diabetes)?')
VOCAB = {
    'stages': list(STAGES.values()),
    'sources': ['HPAP'], 'donor_sources': ['HPAP'], 'sample_sources': ['HPAP'],
    'modalities': ['scRNA-seq', 'snMultiomics'],
    'tissues': [{'id': 'UBERON_0002106', 'name': 'spleen'}],
    'modality_links_verified': True,
    'assay_donor_sources': {'snMultiomics': ['HPAP']},
    'donor_categories_complete': True,
    'donor_categorical_values': {
        'gender': ['Female', 'Male'], 'sex_at_birth': ['F', 'M'],
        'race': ['White'], 'donation_type': ['Organ Donor'],
        'aab_state': ['AAB-'], 'hla_status': ['not recorded'],
        'diabetes_type': ['Control Without Diabetes', 'Diabetes (Type I)', 'Diabetes (Type II)'],
        'derived_diabetes_status': ['Normal', 'Prediabetes', 'Diabetes'],
        'data_source': ['HPAP'],
    },
    # Deliberately non-production IDs prove that resolution uses this snapshot.
    'donor_diseases': [
        {'id': 'CURRENT_T1D', 'name': 'type 1 diabetes', 'synonyms': ['T1D']},
        {'id': 'CURRENT_T2D', 'name': 'type 2 diabetes', 'synonyms': ['T2D']},
    ],
    'inventory_sha256': 'current-runtime-fixture',
}


def _resolved_step(assay):
    step = {
        'id': assay, 'question': f'Count spleen samples labeled with the {assay} assay.',
        'relation_types': ['HAS_DONOR', 'HAS_SAMPLE'],
        'constraints': [{'entity_type': 'Sample_node', 'property': 'data_modality',
                         'operator': '=', 'value': assay}],
        'depends_on': [], 'complete': True,
        'semantic_request': {'source': 'user_request', 'question': QUESTION},
    }
    result = resolve(step, deepcopy(VOCAB), RELEASE)
    result['graph_version'] = RELEASE
    result['resolved_entities'] = []
    for index, constraint in enumerate(result['constraints']):
        if constraint.get('entity_type') == 'anatomical_structure':
            result['resolved_entities'].append({
                'constraint_index': index, 'requested': deepcopy(constraint),
                'state': 'resolved', 'graph_version': RELEASE,
                'entity_type': 'anatomical_structure', 'labels': ['anatomical_structure'],
                'id': constraint['value'], 'name': 'spleen',
            })
    return result


@pytest.mark.parametrize('assay', ['scRNA-seq', 'snMultiomics'])
def test_exact_nd_question_uses_live_control_category_and_local_template(assay):
    step = _resolved_step(assay)
    assert step['semantic_issues'] == []
    assert step['relation_types'] == ['HAS_SAMPLE']
    assert {'entity_type': 'donor', 'property': 'data_source', 'operator': '=', 'value': 'HPAP'} in step['constraints']
    assert {'entity_type': 'donor', 'property': 'diabetes_type', 'operator': '=',
            'value': 'Control Without Diabetes'} in step['constraints']
    assert not any(c.get('entity_type') == 'disease' for c in step['constraints'])
    compiled = compile_query(step)
    assert compiled['template_id'] == 'donor_tissue_same_sample_records'
    assert set(compiled['parameters'].values()) >= {'HPAP', 'Control Without Diabetes', assay}
    assert all(value not in compiled['cypher'] for value in compiled['parameters'].values())


@pytest.mark.parametrize('question', [
    'Find HPAP donors who are not T1D.',
    'Find HPAP donors excluding type 2 diabetes.',
])
def test_negative_disease_only_never_becomes_positive_or_nd(question):
    result = resolve({'question': question, 'relation_types': ['HAS_DONOR'],
                      'constraints': [
                          {'entity_type': 'disease', 'property': 'id', 'operator': '=', 'value': 'CURRENT_T1D'},
                          {'entity_type': 'donor', 'property': 'diabetes_type', 'operator': '=',
                           'value': 'Diabetes (Type I)'}]}, deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('entity_type') == 'disease' for c in result['constraints'])
    assert not any(c.get('property') == 'diabetes_type' for c in result['constraints'])


def test_positive_and_negative_disease_polarities_do_not_collapse():
    result = resolve({'question': 'Find HPAP T2D donors but not T1D.',
                      'relation_types': ['HAS_DONOR'], 'constraints': []},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues'] == []
    disease = [c for c in result['constraints'] if c.get('entity_type') == 'disease']
    assert disease == [{'property': 'id', 'entity_type': 'disease', 'operator': '=',
                        'value': 'CURRENT_T2D'}]


def test_missing_or_changed_runtime_control_category_fails_closed():
    for values in ([], ['Healthy-ish'],
                   ['Control Without Diabetes', 'Healthy Control']):
        vocabulary = deepcopy(VOCAB)
        vocabulary['donor_categorical_values']['diabetes_type'] = values
        result = resolve({'question': QUESTION, 'relation_types': ['HAS_SAMPLE'],
                          'constraints': []}, vocabulary, RELEASE)
        assert result['semantic_issues']
        assert not any(c.get('property') == 'diabetes_type' for c in result['constraints'])


def test_derived_normal_is_not_substituted_for_recorded_nd_cohort():
    explicit = QUESTION + ' Also require derived diabetes status Normal.'
    result = resolve({'question': explicit, 'relation_types': ['HAS_SAMPLE'],
                      'constraints': [{'entity_type': 'donor',
                          'property': 'derived_diabetes_status', 'operator': '=', 'value': 'Normal'}]},
                     deepcopy(VOCAB), RELEASE)
    fields = {(c.get('property'), c.get('value')) for c in result['constraints']}
    assert ('diabetes_type', 'Control Without Diabetes') in fields
    assert ('derived_diabetes_status', 'Normal') in fields


def test_generated_derived_status_cannot_narrow_an_nd_request():
    result = resolve({'question': QUESTION, 'relation_types': ['HAS_SAMPLE'],
                      'constraints': [{'entity_type': 'donor',
                          'property': 'derived_diabetes_status', 'operator': '=', 'value': 'Normal'}]},
                     deepcopy(VOCAB), RELEASE)
    assert not any(c.get('property') == 'derived_diabetes_status'
                   for c in result['constraints'])


def test_split_step_inherits_one_positive_source_but_not_multi_source_request():
    split = _resolved_step('scRNA-seq')
    assert any(c.get('property') == 'data_source' and c.get('value') == 'HPAP'
               for c in split['constraints'])
    raw = QUESTION.replace('HPAP donors', 'HPAP versus StudyA donors')
    vocabulary = deepcopy(VOCAB)
    vocabulary['sources'] = vocabulary['donor_sources'] = ['HPAP', 'StudyA']
    step = {'question': 'Count spleen samples labeled with scRNA-seq.',
            'relation_types': ['HAS_SAMPLE'], 'constraints': [],
            'semantic_request': {'source': 'user_request', 'question': raw}}
    result = resolve(step, vocabulary, RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('property') == 'data_source' for c in result['constraints'])


def test_split_step_inherits_one_unambiguous_recorded_stage():
    raw = QUESTION.replace('ND/healthy (not type 1 diabetes)', 'ND/healthy at recorded stage 3 (not type 1 diabetes)')
    step = {'question': 'Count spleen samples labeled with scRNA-seq.',
            'relation_types': ['HAS_SAMPLE'], 'constraints': [],
            'semantic_request': {'source': 'user_request', 'question': raw}}
    result = resolve(step, deepcopy(VOCAB), RELEASE)
    assert any(c.get('property') == 't1d_stage' and c.get('value') == STAGES['3']
               for c in result['constraints'])


@pytest.mark.parametrize('phrase', [
    'excluding non-diabetic donors',
    'not healthy controls',
    'other than ND/healthy donors',
])
def test_negated_control_phrase_never_becomes_positive_control(phrase):
    result = resolve({'question': f'Find HPAP spleen samples from {phrase}.',
                      'relation_types': ['HAS_SAMPLE'],
                      'constraints': [{'entity_type': 'donor', 'property': 'diabetes_type',
                                       'operator': '=', 'value': 'Control Without Diabetes'}]},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('property') == 'diabetes_type' for c in result['constraints'])
    assert result['semantic_registry']['clinical_intent']['control_cohort'] is False
    assert result['semantic_registry']['clinical_intent']['excluded_control_cohort'] is True


def test_healthy_tissue_is_not_a_healthy_donor_classification():
    result = resolve({'question': 'Find spleen samples from HPAP donors with healthy spleen tissue.',
                      'relation_types': ['HAS_SAMPLE'],
                      'constraints': [{'entity_type': 'donor', 'property': 'diabetes_type',
                                       'operator': '=', 'value': 'Control Without Diabetes'}]},
                     deepcopy(VOCAB), RELEASE)
    assert not any(c.get('property') == 'diabetes_type' for c in result['constraints'])
    assert result['semantic_registry']['clinical_intent']['control_cohort'] is False


@pytest.mark.parametrize('phrase', ['not type I diabetes', 'not T1DM', 'excluding T2DM'])
def test_roman_and_dm_disease_exclusions_never_become_positive(phrase):
    result = resolve({'question': f'Find HPAP donors {phrase}.',
                      'relation_types': ['HAS_DONOR'],
                      'constraints': [{'entity_type': 'disease', 'property': 'id',
                                       'operator': '=', 'value': 'CURRENT_T1D'}]},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('entity_type') == 'disease' for c in result['constraints'])


def test_negation_stops_at_contrast_conjunction_for_disease_and_source():
    disease = resolve({'question': 'Find HPAP donors not T1D but T2D.',
                       'relation_types': ['HAS_DONOR'], 'constraints': []},
                      deepcopy(VOCAB), RELEASE)
    assert [c.get('value') for c in disease['constraints']
            if c.get('entity_type') == 'disease'] == ['CURRENT_T2D']
    vocabulary = deepcopy(VOCAB)
    vocabulary['sources'] = vocabulary['donor_sources'] = ['HPAP', 'StudyA']
    source = resolve({'question': 'Find donors not from StudyA but from HPAP donors.',
                      'relation_types': ['HAS_DONOR'], 'constraints': []},
                     vocabulary, RELEASE)
    assert {'entity_type': 'donor', 'property': 'data_source', 'operator': '=',
            'value': 'HPAP'} in source['constraints']


def test_generated_source_and_clinical_filters_require_raw_request_authority():
    unrequested_source = resolve({
        'question': 'Find spleen samples.', 'relation_types': ['HAS_SAMPLE'],
        'constraints': [{'entity_type': 'donor', 'property': 'data_source',
                         'operator': '=', 'value': 'HPAP'}],
        'semantic_request': {'source': 'user_request', 'question': 'Find spleen samples.'}},
        deepcopy(VOCAB), RELEASE)
    assert not any(c.get('property') == 'data_source' for c in unrequested_source['constraints'])
    assert unrequested_source['semantic_issues']

    wrong_owner_and_clinical = resolve({
        'question': 'Count spleen samples.', 'relation_types': ['HAS_SAMPLE'],
        'constraints': [
            {'entity_type': 'Sample_node', 'property': 'data_source', 'operator': '=', 'value': 'HPAP'},
            {'entity_type': 'donor', 'property': 'diabetes_type', 'operator': '=',
             'value': 'Diabetes (Type I)'},
        ],
        'semantic_request': {'source': 'user_request', 'question':
            'Count spleen samples from HPAP donors.'}}, deepcopy(VOCAB), RELEASE)
    sources = [c for c in wrong_owner_and_clinical['constraints']
               if c.get('property') == 'data_source']
    assert sources == [{'property': 'data_source', 'entity_type': 'donor',
                        'operator': '=', 'value': 'HPAP'}]
    assert not any(c.get('property') == 'diabetes_type'
                   for c in wrong_owner_and_clinical['constraints'])


@pytest.mark.parametrize('split', [False, True])
def test_negative_stage_keeps_negative_polarity_with_runtime_proof(split):
    raw = 'Find HPAP donors excluding stage 3.'
    step = {'question': 'Count HPAP donors.' if split else raw,
            'relation_types': ['HAS_DONOR'],
            'constraints': [{'entity_type': 'donor', 'property': 't1d_stage',
                             'operator': '=', 'value': STAGES['3']}]}
    if split:
        step['semantic_request'] = {'source': 'user_request', 'question': raw}
    result = resolve(step, deepcopy(VOCAB), RELEASE)
    constraint = next(c for c in result['constraints'] if c.get('property') == 't1d_stage')
    assert constraint['operator'] == '!=' and constraint['value'] == STAGES['3']
    assert any(m.get('canonical_binding') == constraint
               and m.get('match_kind') == 'verified_runtime_stage'
               and m.get('inventory_sha256') == VOCAB['inventory_sha256']
               for m in result['resolved_constraints'])


def test_split_step_inherits_one_tissue_and_preserves_explicit_disease_link_relation():
    raw = 'Show disease records linked to HPAP donors with spleen samples.'
    result = resolve({'question': 'Count matching samples.',
                      'relation_types': ['HAS_DONOR', 'HAS_SAMPLE'], 'constraints': [],
                      'semantic_request': {'source': 'user_request', 'question': raw}},
                     deepcopy(VOCAB), RELEASE)
    assert any(c.get('entity_type') == 'anatomical_structure'
               and c.get('value') == 'UBERON_0002106' for c in result['constraints'])
    assert result['relation_types'] == ['HAS_DONOR', 'HAS_SAMPLE']


def test_unrequested_generic_runtime_category_is_removed_before_proof():
    result = resolve({'question': 'Find HPAP donors.', 'relation_types': ['HAS_DONOR'],
                      'constraints': [{'entity_type': 'donor', 'property': 'gender',
                                       'operator': '=', 'value': 'Female'}]},
                     deepcopy(VOCAB), RELEASE)
    assert not any(c.get('property') == 'gender' for c in result['constraints'])


def test_runtime_vocabulary_separates_source_owners_and_hashes_snapshot():
    adapter = object.__new__(GraphAdapter)
    adapter.settings = SimpleNamespace(graph_version=RELEASE)

    async def query(cypher, parameters=None):
        if 'MATCH (d:donor) RETURN' in cypher:
            row = {'stages': list(STAGES.values()), 'donor_sources': ['HPAP']}
            for field, values in VOCAB['donor_categorical_values'].items():
                row['category_' + field] = values
            return [row]
        if 'MATCH (s:Sample_node) RETURN' in cypher:
            return [{'modalities': ['scRNA-seq', 'snMultiomics'],
                     'sample_sources': ['HPAP assay archive']}]
        if 'MATCH (m:data_modality)' in cypher:
            return [{'mismatches': 0, 'links': 2}]
        if 'MATCH (a:anatomical_structure)' in cypher:
            return VOCAB['tissues']
        if 'MATCH (x:disease)' in cypher:
            return VOCAB['donor_diseases']
        if 'RETURN s.data_modality AS modality' in cypher:
            return [{'modality': 'snMultiomics', 'sources': ['HPAP']}]
        raise AssertionError(cypher)

    adapter._small_query = query
    value = asyncio.run(adapter.semantic_vocabulary())
    assert value['donor_sources'] == ['HPAP']
    assert value['sample_sources'] == ['HPAP assay archive']
    assert value['sources'] == ['HPAP', 'HPAP assay archive']
    assert value['donor_categorical_values']['diabetes_type'] == VOCAB['donor_categorical_values']['diabetes_type']
    assert len(value['inventory_sha256']) == 64


@pytest.mark.parametrize('wording', [
    'Find HPAP donors not recorded as healthy.',
    'Find HPAP donors not classified as ND.',
    'Find HPAP donors never labeled as healthy.',
    'Find HPAP donors not categorized as non-diabetic.',
])
def test_internal_control_classification_negation_never_becomes_positive(wording):
    result = resolve({'question': wording, 'relation_types': ['HAS_DONOR'],
                      'constraints': []}, deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('property') == 'diabetes_type' for c in result['constraints'])
    assert result['semantic_registry']['clinical_intent']['control_cohort'] is False
    assert result['semantic_registry']['clinical_intent']['excluded_control_cohort'] is True


@pytest.mark.parametrize('wording', [
    'Find HPAP donors who never had T1D.',
    'Find HPAP donors lacking T1D.',
    'Find HPAP donors free of type 1 diabetes.',
    'Find HPAP donors with neither T1D nor T2D.',
])
def test_broad_negative_disease_wording_never_selects_a_positive_disease(wording):
    result = resolve({'question': wording, 'relation_types': ['HAS_DONOR'],
                      'constraints': []}, deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('entity_type') == 'disease' for c in result['constraints'])


def test_never_stage_and_source_preserve_negative_operator():
    stage = resolve({'question': 'Find HPAP donors never at stage 3.',
                     'relation_types': ['HAS_DONOR'], 'constraints': []},
                    deepcopy(VOCAB), RELEASE)
    assert next(c for c in stage['constraints'] if c.get('property') == 't1d_stage')['operator'] == '!='
    vocabulary = deepcopy(VOCAB)
    vocabulary['sources'] = vocabulary['donor_sources'] = ['HPAP', 'StudyA']
    source = resolve({'question': 'Find donors never from StudyA cohort.',
                      'relation_types': ['HAS_DONOR'], 'constraints': []},
                     vocabulary, RELEASE)
    assert next(c for c in source['constraints'] if c.get('property') == 'data_source')['operator'] == '!='


def test_generated_stage_tissue_and_assay_cannot_replace_immutable_request_scope():
    vocabulary = deepcopy(VOCAB)
    vocabulary['tissues'].append({'id': 'CURRENT_PANCREAS', 'name': 'pancreas'})
    vocabulary['modalities'].append('scATAC-seq')
    result = resolve({
        'question':'Count pancreas stage 3 samples labeled with scATAC-seq.',
        'relation_types':['HAS_SAMPLE'],
        'constraints':[
            {'entity_type':'anatomical_structure', 'property':'id',
             'operator':'=', 'value':'CURRENT_PANCREAS'},
            {'entity_type':'donor', 'property':'t1d_stage',
             'operator':'=', 'value':STAGES['3']},
            {'entity_type':'Sample_node', 'property':'data_modality',
             'operator':'=', 'value':'scATAC-seq'},
        ],
        'semantic_request':{'source':'user_request', 'question':QUESTION},
    }, vocabulary, RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('value') in {'CURRENT_PANCREAS', STAGES['3'], 'scATAC-seq'}
                   for c in result['constraints'])


@pytest.mark.parametrize(('prop', 'operator', 'value'), [
    ('age', '>', '42'), ('bmi', '>=', '27'),
    ('hba1c_percentage', '>', '8.1'), ('c_peptide_ng_ml', '<', '0.5'),
    ('other_disease_records', 'CONTAINS', 'thyroid'),
])
def test_unrequested_donor_clinical_predicates_are_removed(prop, operator, value):
    result = resolve({'question':'Count HPAP donors.', 'relation_types':['HAS_DONOR'],
                      'constraints':[{'entity_type':'donor', 'property':prop,
                                      'operator':operator, 'value':value}],
                      'semantic_request':{'source':'user_request',
                                          'question':'Count HPAP donors.'}},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('property') == prop for c in result['constraints'])


@pytest.mark.parametrize(('raw', 'constraints'), [
    ('Find donors with age 30 and BMI 25.', [
        {'entity_type': 'donor', 'property': 'age', 'operator': '=', 'value': '25'},
        {'entity_type': 'donor', 'property': 'bmi', 'operator': '=', 'value': '30'},
    ]),
    ('Find HPAP donors age >= 18 with recorded HbA1c.', [
        {'entity_type': 'donor', 'property': 'hba1c_percentage',
         'operator': '>=', 'value': '18'},
    ]),
    ('Find donors with HbA1c >= 6 and C-peptide recorded.', [
        {'entity_type': 'donor', 'property': 'c_peptide_ng_ml',
         'operator': '>=', 'value': '6'},
    ]),
    ('Find donors age 18 to 65 with recorded HbA1c.', [
        {'entity_type': 'donor', 'property': 'hba1c_percentage',
         'operator': '>=', 'value': '18'},
    ]),
])
def test_request_authorization_cannot_transpose_values_between_fields(raw, constraints):
    result = resolve({'question': 'Count matching donors.',
                      'relation_types': ['HAS_DONOR'],
                      'constraints': constraints,
                      'semantic_request': {'source': 'user_request', 'question': raw}},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c in result['constraints'] for c in constraints)
    assert not any(match.get('match_kind') == 'verified_request_filter'
                   and match.get('canonical_binding') in constraints
                   for match in result['resolved_constraints'])


@pytest.mark.parametrize(('raw', 'constraint'), [
    ('Find donors with age >= 18.',
     {'entity_type': 'donor', 'property': 'age', 'operator': '>=', 'value': '18'}),
    ('Find donors with BMI <= 25.',
     {'entity_type': 'donor', 'property': 'bmi', 'operator': '<=', 'value': '25'}),
    ('Find donors whose other disease records contain thyroid.',
     {'entity_type': 'donor', 'property': 'other_disease_records',
      'operator': 'CONTAINS', 'value': 'thyroid'}),
])
def test_request_authorization_binds_field_operator_and_value_locally(raw, constraint):
    result = resolve({'question': 'Count matching donors.',
                      'relation_types': ['HAS_DONOR'],
                      'constraints': [constraint],
                      'semantic_request': {'source': 'user_request', 'question': raw}},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues'] == []
    assert constraint in result['constraints']
    assert any(match.get('match_kind') == 'verified_request_filter'
               and match.get('canonical_binding') == constraint
               for match in result['resolved_constraints'])


@pytest.mark.parametrize(('raw', 'prop', 'operator', 'value'), [
    ('Find HPAP donors with BMI not more than 27.', 'bmi', '>', '27'),
    ('Find HPAP donors with BMI no more than 27.', 'bmi', '>', '27'),
    ('Find HPAP donors not older than age 42.', 'age', '>', '42'),
    ('Find HPAP donors with HbA1c not less than 8.1.', 'hba1c_percentage', '<', '8.1'),
    ('Exclude HPAP donors with BMI 27.', 'bmi', '=', '27'),
])
def test_opposite_or_negated_donor_comparator_is_not_authorized(raw, prop, operator, value):
    result = resolve({'question':raw, 'relation_types':['HAS_DONOR'],
                      'constraints':[{'entity_type':'donor', 'property':prop,
                                      'operator':operator, 'value':value}],
                      'semantic_request':{'source':'user_request', 'question':raw}},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('property') == prop for c in result['constraints'])


def test_category_value_in_unrelated_phrase_does_not_authorize_donor_category():
    raw = 'Find HPAP donors with white blood cell measurements.'
    result = resolve({'question':raw, 'relation_types':['HAS_DONOR'],
                      'constraints':[{'entity_type':'donor', 'property':'race',
                                      'operator':'=', 'value':'White'}],
                      'semantic_request':{'source':'user_request', 'question':raw}},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('property') == 'race' for c in result['constraints'])


@pytest.mark.parametrize('raw', [
    'Find samples from HPAP or StudyA donors.',
    'Compare HPAP versus StudyA donors with samples.',
    'Find donors excluding StudyA and StudyB.',
])
def test_multiple_source_scope_never_falls_through_to_unrestricted_query(raw):
    vocabulary = deepcopy(VOCAB)
    vocabulary['sources'] = vocabulary['donor_sources'] = ['HPAP', 'StudyA', 'StudyB']
    result = resolve({'question':raw, 'relation_types':['HAS_SAMPLE'],
                      'constraints':[]}, vocabulary, RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('property') == 'data_source' for c in result['constraints'])


@pytest.mark.parametrize('raw', [
    'Find samples whose sample source is HPAP and whose donor source is HPAP.',
    'Find samples with sample data source HPAP from HPAP donors.',
])
def test_same_source_can_be_bound_independently_to_sample_and_donor(raw):
    result = resolve({'question': raw, 'relation_types': ['HAS_SAMPLE'],
                      'constraints': []}, deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues'] == []
    assert [c for c in result['constraints'] if c.get('property') == 'data_source'] == [
        {'property': 'data_source', 'entity_type': 'Sample_node',
         'operator': '=', 'value': 'HPAP'},
        {'property': 'data_source', 'entity_type': 'donor',
         'operator': '=', 'value': 'HPAP'},
    ]


def test_conflicting_source_polarity_fails_closed():
    raw = 'Find HPAP donors but not HPAP donors.'
    result = resolve({'question': raw, 'relation_types': ['HAS_DONOR'],
                      'constraints': []}, deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('property') == 'data_source' for c in result['constraints'])


@pytest.mark.parametrize('raw', [
    'Find T1D donors but not T1D donors.',
    'Find samples from healthy controls but not healthy controls.',
])
def test_conflicting_clinical_polarity_fails_closed(raw):
    result = resolve({'question': raw, 'relation_types': ['HAS_SAMPLE'],
                      'constraints': []}, deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('entity_type') == 'disease'
                   or c.get('property') == 'diabetes_type'
                   for c in result['constraints'])


def test_control_only_wording_still_enters_runtime_semantic_resolution():
    raw = 'Count healthy controls.'
    result = resolve({'question': raw, 'relation_types': ['HAS_DONOR'],
                      'constraints': [],
                      'semantic_request': {'source': 'user_request', 'question': raw}},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues'] == []
    assert {'property': 'diabetes_type', 'entity_type': 'donor', 'operator': '=',
            'value': 'Control Without Diabetes'} in result['constraints']
    assert result['request_filter_bindings']


@pytest.mark.parametrize(('raw', 'constraint'), [
    ('Find white blood cell samples and report donor race.',
     {'entity_type': 'donor', 'property': 'race', 'operator': '=', 'value': 'White'}),
    ('Find samples mentioning diabetes in note and report donor other disease records.',
     {'entity_type': 'donor', 'property': 'other_disease_records',
      'operator': '=', 'value': 'diabetes'}),
])
def test_output_field_mentions_do_not_authorize_equality_filters(raw, constraint):
    result = resolve({'question': 'Count matching samples.',
                      'relation_types': ['HAS_SAMPLE'], 'constraints': [constraint],
                      'semantic_request': {'source': 'user_request', 'question': raw}},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert constraint not in result['constraints']


def test_unrelated_count_near_field_does_not_authorize_equality_filter():
    raw = 'Find donors with BMI data available for 25 samples.'
    constraint = {'entity_type': 'donor', 'property': 'bmi',
                  'operator': '=', 'value': '25'}
    result = resolve({'question': 'Count matching donors.',
                      'relation_types': ['HAS_DONOR'], 'constraints': [constraint],
                      'semantic_request': {'source': 'user_request', 'question': raw}},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert constraint not in result['constraints']


@pytest.mark.parametrize(('raw', 'constraint'), [
    ('Find donors with recorded gender Female or Male.',
     {'entity_type': 'donor', 'property': 'gender', 'operator': '=', 'value': 'Female'}),
    ('Find donors with BMI 25 or 30.',
     {'entity_type': 'donor', 'property': 'bmi', 'operator': '=', 'value': '25'}),
    ('Find donors whose other disease records contain thyroid or lupus.',
     {'entity_type': 'donor', 'property': 'other_disease_records',
      'operator': 'CONTAINS', 'value': 'thyroid'}),
])
def test_same_field_alternatives_cannot_be_silently_narrowed(raw, constraint):
    result = resolve({'question': 'Count matching donors.',
                      'relation_types': ['HAS_DONOR'], 'constraints': [constraint],
                      'semantic_request': {'source': 'user_request', 'question': raw}},
                     deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert constraint not in result['constraints']


@pytest.mark.parametrize(('raw', 'constraints', 'missing_property'), [
    ('Find HPAP donors with BMI <= 25.', [], 'bmi'),
    ('Find donors with HbA1c above 8.1.', [], 'hba1c_percentage'),
    ('Find donors whose other disease records contain thyroid.', [], 'other_disease_records'),
    ('Find Female donors by recorded gender.', [], 'gender'),
    ('Find donors age 18 to 65.', [
        {'entity_type': 'donor', 'property': 'age', 'operator': '>=', 'value': '18'}], 'age'),
    ('Find donors with recorded gender Female and race White.', [
        {'entity_type': 'donor', 'property': 'gender', 'operator': '=', 'value': 'Female'}], 'race'),
])
def test_omitted_or_partial_raw_donor_filter_fails_closed(raw, constraints, missing_property):
    result = resolve({'question': 'Count matching donors.',
                      'relation_types': ['HAS_DONOR'], 'constraints': constraints,
                      'semantic_request': {'source': 'user_request', 'question': raw}},
                     deepcopy(VOCAB), RELEASE)
    assert any('donor.' + missing_property in issue for issue in result['semantic_issues'])


@pytest.mark.parametrize(('raw', 'prop'), [
    ('Find donors whose HLA typing contains DR4.', 'hla_typing'),
    ('Find donors whose cause of death contains trauma.', 'cause_of_death'),
    ('Find donors with family history of diabetes = true.', 'family_history_of_diabetes'),
])
def test_unparsed_explicit_donor_filter_fails_closed_when_omitted(raw, prop):
    result = resolve({'question': 'Count matching donors.',
                      'relation_types': ['HAS_DONOR'], 'constraints': [],
                      'semantic_request': {'source': 'user_request', 'question': raw}},
                     deepcopy(VOCAB), RELEASE)
    assert any('donor.' + prop in issue for issue in result['semantic_issues'])


def test_donor_identifier_prefix_is_not_misread_as_dataset_source():
    raw = 'Find donor ID HPAP-041.'
    constraint = {'entity_type': 'donor', 'property': 'id',
                  'operator': '=', 'value': 'HPAP-041'}
    result = resolve({'question': raw, 'relation_types': ['HAS_DONOR'],
                      'constraints': [constraint],
                      'semantic_request': {'source': 'user_request', 'question': raw}},
                     deepcopy(VOCAB), RELEASE)
    assert not any(c.get('property') == 'data_source' for c in result['constraints'])
    omitted = resolve({'question': 'Count matching donors.',
                       'relation_types': ['HAS_DONOR'], 'constraints': [],
                       'semantic_request': {'source': 'user_request', 'question': raw}},
                      deepcopy(VOCAB), RELEASE)
    assert any('donor.id' in issue for issue in omitted['semantic_issues'])


@pytest.mark.parametrize('raw', [
    'Find donors with samples and report HLA typing.',
    'Find donors with samples and list cause of death.',
])
def test_projection_only_free_text_fields_are_not_inferred_as_filters(raw):
    result = resolve({'question': 'Count matching donors.',
                      'relation_types': ['HAS_DONOR'], 'constraints': [],
                      'semantic_request': {'source': 'user_request', 'question': raw}},
                     deepcopy(VOCAB), RELEASE)
    assert not any('donor.hla_typing' in issue or 'donor.cause_of_death' in issue
                   for issue in result['semantic_issues'])


@pytest.mark.parametrize('raw', [
    'Find HPAP-only donors.',
    'Find HPAP-derived donor samples.',
])
def test_hyphenated_source_qualifiers_still_resolve_as_source(raw):
    result = resolve({'question': raw, 'relation_types': ['HAS_SAMPLE'],
                      'constraints': []}, deepcopy(VOCAB), RELEASE)
    assert {'property': 'data_source', 'entity_type': 'donor', 'operator': '=',
            'value': 'HPAP'} in result['constraints']


@pytest.mark.parametrize('raw', [
    'Find donors; stage 3 is excluded.',
    'Find samples from spleen excluded from the analysis.',
    'Find scRNA-seq excluded samples.',
    'Find T1D donors excluded from the cohort.',
    'Find HPAP excluded donors.',
    'Find T1D-free donors.',
    'Find T1D negative donors.',
    'Find stage-3-negative donors.',
])
def test_postfix_exclusion_never_becomes_a_positive_filter(raw):
    result = resolve({'question': raw,
                      'relation_types': ['HAS_SAMPLE'] if 'sample' in raw else ['HAS_DONOR'],
                      'constraints': []}, deepcopy(VOCAB), RELEASE)
    assert not any(str(c.get('operator', '=')).upper() in {'=', 'IN'}
                   and (c.get('entity_type') in {'disease', 'anatomical_structure'}
                        or c.get('property') in {'t1d_stage', 'data_source', 'data_modality'})
                   for c in result['constraints'])


@pytest.mark.parametrize('raw', [
    'Find scRNA-seq negative control samples.',
    'Find HPAP free-access samples.',
    'Find HPAP free samples.',
    'Find stage 3 negative control donors.',
])
def test_negative_control_and_free_access_suffixes_are_not_generic_exclusions(raw):
    result = resolve({'question': raw,
                      'relation_types': ['HAS_SAMPLE'] if 'sample' in raw else ['HAS_DONOR'],
                      'constraints': []}, deepcopy(VOCAB), RELEASE)
    assert not any(str(c.get('operator', '=')).upper() in {'!=', '<>', 'NOT IN'}
                   for c in result.get('constraints', []))


@pytest.mark.parametrize('raw', [
    'Find HPAP samples not from spleen.',
    'Find HPAP samples excluding spleen.',
    'Find HPAP samples from tissues other than spleen.',
    'Find HPAP samples never from spleen.',
])
def test_negative_tissue_scope_never_becomes_positive_anatomy(raw):
    result = resolve({'question':raw, 'relation_types':['HAS_SAMPLE'],
                      'constraints':[]}, deepcopy(VOCAB), RELEASE)
    assert result['semantic_issues']
    assert not any(c.get('entity_type') == 'anatomical_structure'
                   for c in result['constraints'])


def test_identity_phrase_authority_allows_only_simple_trailing_plural():
    assert _positive_request_phrase('Find ductal cells.', 'ductal cell')
    assert _positive_request_phrase('Find ductal-cells.', 'ductal cell')
    assert not _positive_request_phrase('Find ductal cellular states.', 'ductal cell')
    assert not _positive_request_phrase('Find records excluding ductal cells.',
                                        'ductal cell')


@pytest.mark.parametrize(('owner', 'prop', 'raw', 'values'), [
    ('Gene', 'name', 'Find genes where Gene.name IN [CFTR, INS].', ['CFTR', 'INS']),
    ('anatomical_structure', 'name',
     'Find anatomy where anatomy.name IN [spleen, pancreas].', ['spleen', 'pancreas']),
    ('disease', 'name',
     'Find diseases where disease.name IN [T1D, T2D].', ['T1D', 'T2D']),
    ('variants', 'id',
     'Find variants where variant.id IN [rs1, rs2].', ['rs1', 'rs2']),
])
def test_explicit_qualified_identity_list_has_one_unambiguous_owner(
        owner, prop, raw, values):
    constraint = {'entity_type': owner, 'property': prop,
                  'operator': 'IN', 'value': values}
    assert _property_owner_is_unambiguous(owner, prop, raw)
    assert _raw_constraint_authorized(constraint, raw, ['GENE_ENRICHED_IN'])
    other_owner = 'disease' if owner != 'disease' else 'Gene'
    assert not _raw_constraint_authorized(
        {**constraint, 'entity_type': other_owner}, raw, ['GENE_ENRICHED_IN'])


@pytest.mark.parametrize('raw', [
    'Find HPAP donors with islet samples.',
    'Find HPAP samples from islets.',
])
def test_resolved_islet_alias_is_not_reflagged_as_unknown_tissue(raw):
    vocabulary = deepcopy(VOCAB)
    vocabulary['tissues'].append({
        'id': 'UBERON_0000006',
        'name': 'pancreatic islet (islet of Langerhans)'})
    result = resolve({'question': raw, 'relation_types': ['HAS_SAMPLE'],
                      'constraints': []}, vocabulary, RELEASE)
    assert result['semantic_issues'] == []
    assert {'property': 'id', 'entity_type': 'anatomical_structure',
            'operator': '=', 'value': 'UBERON_0000006'} in result['constraints']
