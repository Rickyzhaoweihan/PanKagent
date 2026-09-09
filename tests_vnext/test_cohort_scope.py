from copy import deepcopy

import pytest

from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.query_templates import compile_query
from test_sample_path_templates import sample_step


PARAMS = {'source': 'HPAP', 'tissue': 'UBERON_0015865', 'assay': 'snMultiomics'}
BASE = ('MATCH (d:donor)-[ds:HAS_SAMPLE]->(s:Sample_node), '
        '(t:anatomical_structure)-[ts:HAS_SAMPLE]->(s) '
        'WHERE d.data_source = $source AND t.id = $tissue AND s.data_modality = $assay ')
RETURN = 'RETURN d, ds, s, t, ts'


def donor_sample_step():
    step = sample_step(assay='snMultiomics')
    step['semantic_registry']['donor_required'] = True
    step['constraints'].append({'entity_type': 'donor', 'property': 'data_source', 'operator': '=', 'value': 'HPAP'})
    step['sample_requirements']['modality_groups'] = [['snMultiomics']]
    return step


def test_actual_unrequested_disease_sample_membership_is_rejected_even_when_count_matches():
    step = donor_sample_step()
    query = ('MATCH (disease:disease)-[disease_sample:HAS_SAMPLE]->(sample:Sample_node), '
             '(donor:donor)-[donor_sample:HAS_SAMPLE]->(sample), '
             '(tissue:anatomical_structure)-[tissue_sample:HAS_SAMPLE]->(sample) '
             "WHERE donor.data_source = 'HPAP' AND tissue.id = 'UBERON_0015865' "
             "AND sample.data_modality = 'snMultiomics' "
             'RETURN disease, sample, donor, tissue, disease_sample, donor_sample, tissue_sample')
    assert 'unrequested_mandatory_sample_source:disease' in validate_cypher(query, step)
    # The correct role constraints and same-sample witnesses are sufficient.
    assert validate_cypher(BASE + RETURN, step, PARAMS) == []


@pytest.mark.parametrize('extra', [
    'MATCH (extra:disease)-[:HAS_SAMPLE]->(s) ',
    'MATCH (:disease)-[:HAS_SAMPLE]->(s) ',
    'MATCH (extra:Gene) ',
    'MATCH (extra:anatomical_structure)-[:HAS_SAMPLE]->(s) ',
    'MATCH (extra:data_modality)-[:HAS_SAMPLE]->(s) ',
])
def test_additional_required_source_or_owner_cannot_narrow_an_approved_cohort(extra):
    errors = validate_cypher(BASE + extra + RETURN, donor_sample_step(), PARAMS)
    assert any(e.startswith(('unrequested_mandatory_cohort_', 'unrequested_mandatory_sample_source:')) for e in errors)


def test_optional_unfiltered_source_context_preserves_primary_rows():
    query = BASE + 'OPTIONAL MATCH (disease:disease)-[context:HAS_SAMPLE]->(s) ' + RETURN + ', disease, context'
    assert validate_cypher(query, donor_sample_step(), PARAMS) == []


@pytest.mark.parametrize('tail', [
    'WITH d,ds,s,t,ts,count(disease) AS context_count WHERE context_count > 0 ' + RETURN,
    'WITH d,ds,s,t,ts,collect(disease) AS context_nodes UNWIND context_nodes AS disease ' + RETURN,
    'WITH d,ds,s,t,ts,disease AS source MATCH (source)-[:HAS_DONOR]->(d) ' + RETURN,
    'WITH d,ds,s,t,ts,count(context) AS linked WHERE linked > 0 ' + RETURN,
])
def test_optional_context_cannot_be_promoted_to_a_primary_result_filter(tail):
    query = BASE + 'OPTIONAL MATCH (disease:disease)-[context:HAS_SAMPLE]->(s) ' + tail
    assert 'filtering_optional_cohort_context' in validate_cypher(query, donor_sample_step(), PARAMS)


def test_optional_context_projection_does_not_taint_unrelated_primary_alias():
    query = (BASE + 'OPTIONAL MATCH (disease:disease)-[context:HAS_SAMPLE]->(s) '
             'WITH disease AS source, d AS donor, ds,s,t,ts RETURN donor,ds,s,t,ts,source')
    assert validate_cypher(query, donor_sample_step(), PARAMS) == []


def test_optional_source_reused_directly_in_later_match_is_still_optional_derived():
    query = (BASE + 'OPTIONAL MATCH (other:donor)-[context:HAS_SAMPLE]->(s) '
             'MATCH (other)-[required:HAS_SAMPLE]->(s) WHERE other.data_source = $source '
             + RETURN + ',other,context,required')
    assert 'filtering_optional_cohort_context' in validate_cypher(query, donor_sample_step(), PARAMS)


def test_requested_primary_has_donor_path_retains_compatibility_without_diagnosis_filter():
    step = {'id': 'count', 'graph_version': 'PanKgraph_08_04', 'complete': True,
            'relation_types': ['HAS_DONOR'], 'semantic_registry': {'donor_required': True},
            'constraints': [{'entity_type': 'donor', 'property': 'data_source', 'operator': '=', 'value': 'HPAP'}]}
    assert validate_cypher('MATCH (disease:disease)-[r:HAS_DONOR]->(d:donor) '
                          'WHERE d.data_source = $source RETURN disease,r,d', step, PARAMS) == []


def test_explicit_disease_cohort_filter_keeps_its_required_donor_link():
    step = donor_sample_step()
    step['constraints'].append({'entity_type': 'disease', 'property': 'id', 'operator': '=', 'value': 'MONDO_0005147'})
    query = BASE + "MATCH (disease:disease)-[cd:HAS_DONOR]->(d) WHERE disease.id = 'MONDO_0005147' " + RETURN + ',disease,cd'
    assert validate_cypher(query, step, PARAMS) == []


def test_verified_modality_endpoint_remains_a_supported_same_sample_predicate():
    step = donor_sample_step()
    step['semantic_registry']['modality_links_verified'] = True
    query = BASE.replace('WHERE ', 'MATCH (m:data_modality)-[ms:HAS_SAMPLE]->(s) WHERE ').replace('s.data_modality =', 'm.id =') + RETURN + ',m,ms'
    assert validate_cypher(query, step, PARAMS) == []
    step['semantic_registry']['modality_links_verified'] = False
    assert any('data_modality' in error for error in validate_cypher(query, step, PARAMS))


@pytest.mark.parametrize('donor_required', [False, True])
def test_schema_verified_tissue_and_donor_templates_pass_the_same_guard(donor_required):
    step = donor_sample_step() if donor_required else sample_step(assay='snMultiomics')
    original = deepcopy(step)
    compiled = compile_query(step)
    assert compiled is not None
    assert validate_cypher(compiled['cypher'], step, compiled['parameters']) == []
    assert original == step
