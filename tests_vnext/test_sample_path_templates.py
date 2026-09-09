from copy import deepcopy
import pytest

from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.query_templates import compile_query
from pankagent_vnext.release_schema import REGISTRY
from pankagent_vnext.semantic_registry import resolve

RELEASE = REGISTRY['release']
VOCAB = {'modalities': ['scRNA-seq', 'scATAC-seq', 'snMultiomics', 'CITE-seq Protein'],
         'sources': ['HPAP'], 'assay_donor_sources': {'snMultiomics': ['HPAP']}}


@pytest.mark.parametrize('wording', ['data modality scRNA-seq', 'data_modality = scRNAseq',
    'data modality is "scRNA-seq"', 'data modality: scRNA-seq'])
def test_explicit_field_lookup_stays_separate_from_capability_check(wording):
    s = {'question': 'Count pancreatic lymph node samples with ' + wording,
         'relation_types': ['HAS_SAMPLE'], 'constraints': [
             {'entity_type': 'Sample_node', 'property': 'data_modality', 'operator': '=', 'value': 'scRNA-seq'}],
         'semantic_request': {'source': 'user_request', 'question':
             'Count PLN RNA samples including documented HPAP multiome RNA components.'}}
    out = resolve(s, VOCAB, RELEASE)
    assert out['sample_requirements']['modality_groups'] == [['scRNA-seq']]
    assert not out['semantic_issues']
    broad = deepcopy(s)
    broad['question'] = 'Count pancreatic lymph node RNA samples including HPAP multiome components.'
    assert resolve(broad, VOCAB, RELEASE)['sample_requirements']['modality_groups'] == [['scRNA-seq', 'snMultiomics']]


def test_explicit_capability_inclusion_survives_field_label_wording():
    q = 'Count spleen samples with data modality scRNA-seq, including documented RNA components of HPAP multiome assays.'
    s = {'question': q, 'relation_types': ['HAS_SAMPLE'], 'constraints': [
        {'entity_type': 'Sample_node', 'property': 'data_modality', 'operator': '=', 'value': 'scRNA-seq'}]}
    result = resolve(s, VOCAB, RELEASE)
    assert result['sample_requirements']['modality_groups'] == [['scRNA-seq', 'snMultiomics']]
    assert not result['semantic_issues']


@pytest.mark.parametrize('operator', ['!=', '<>', 'is not'])
def test_negative_field_label_never_becomes_positive_assay_requirement(operator):
    q = 'Count spleen samples with data_modality ' + operator + ' "scRNA-seq".'
    s = {'question': q, 'relation_types': ['HAS_SAMPLE'], 'constraints': [
        {'entity_type': 'Sample_node', 'property': 'data_modality', 'operator': '!=', 'value': 'scRNA-seq'}]}
    result = resolve(s, VOCAB, RELEASE)
    assert result['sample_requirements']['modality_groups'] == []
    assert result['sample_requirements']['excluded_modality_constraints'][0]['value'] == 'scRNA-seq'
    assert not result['semantic_issues']


def sample_step(tissue='UBERON_0015865', assay='scRNA-seq', operator='='):
    c = {'entity_type': 'anatomical_structure', 'property': 'id', 'operator': '=', 'value': tissue}
    return {'id': 's1', 'question': 'Count matching tissue samples', 'complete': True,
            'graph_version': RELEASE, 'relation_types': ['HAS_SAMPLE'],
            'constraints': [c, {'entity_type': 'Sample_node', 'property': 'data_modality', 'operator': operator, 'value': assay}],
            'resolved_entities': [{'constraint_index': 0, 'requested': deepcopy(c), 'state': 'resolved',
                'graph_version': RELEASE, 'entity_type': 'anatomical_structure',
                'labels': ['anatomical_structure'], 'id': tissue}],
            'semantic_registry': {'donor_required': False},
            'sample_requirements': {'paired': False, 'separate_bindings': False}}


@pytest.mark.parametrize('tissue', ['UBERON_0015865', 'UBERON_0002106'])
@pytest.mark.parametrize('assay,operator', [('scRNA-seq', '='), ('["scRNA-seq","snMultiomics"]', 'IN')])
def test_verified_tissue_sample_path_keeps_exact_filters_without_donor_join(tissue, assay, operator):
    s = sample_step(tissue, assay, operator)
    original = deepcopy(s)
    result = compile_query(s)
    assert result and result['cypher'].startswith('MATCH (a:`anatomical_structure`)-[r:`HAS_SAMPLE`]->(b:`Sample_node`)')
    assert 'donor' not in result['cypher'] and 'LIMIT' not in result['cypher']
    assert validate_cypher(result['cypher'], s, result['parameters']) == []
    assert result['endpoint_coverage']['all_requested_paths_covered']
    assert not result['endpoint_coverage']['all_paths_covered']
    assert result['parameters']['template_0'] == tissue
    assert s == original


@pytest.mark.parametrize('mutation', ['donor', 'no_registry', 'no_identity', 'ambiguous', 'paired', 'separate', 'wrong_owner', 'extra_donor'])
def test_complex_or_unproved_sample_scopes_keep_general_query_route(mutation):
    s = sample_step()
    if mutation == 'donor': s['semantic_registry']['donor_required'] = True
    if mutation == 'no_registry': s.pop('semantic_registry')
    if mutation == 'no_identity': s['resolved_entities'] = []
    if mutation == 'ambiguous': s['resolved_entities'][0]['state'] = 'ambiguous'
    if mutation == 'paired': s['sample_requirements']['paired'] = True
    if mutation == 'separate': s['sample_requirements']['separate_bindings'] = True
    if mutation == 'wrong_owner': s['constraints'][0]['entity_type'] = 'Sample_node'
    if mutation == 'extra_donor': s['constraints'].append({'entity_type': 'donor', 'property': 'data_source', 'operator': '=', 'value': 'HPAP'})
    assert compile_query(s) is None


@pytest.mark.parametrize('assay', ['scRNA-seq', 'snMultiomics', 'BCR-seq', 'TCR-seq'])
def test_donor_and_tissue_template_uses_one_filtered_sample(assay):
    s = sample_step(assay=assay)
    s['semantic_registry']['donor_required'] = True
    s['constraints'].extend([
        {'entity_type': 'donor', 'property': 'data_source', 'operator': '=', 'value': 'HPAP'},
        {'entity_type': 'donor', 'property': 't1d_stage', 'operator': '=', 'value': 'Stage 3: presence of clinical symptoms'},
        {'entity_type': 'Sample_node', 'property': 'data_modality', 'operator': '!=', 'value': 'CITE-seq Protein'}])
    out = compile_query(s)
    assert out and out['template_id'] == 'donor_tissue_same_sample_records'
    assert validate_cypher(out['cypher'], s, out['parameters']) == []
    assert 'disease' not in out['cypher'] and 'LIMIT' not in out['cypher']
    assert out['parameters']['template_1'] == assay
    assert 's.`data_modality` <> $template_4' in out['cypher']
    assert '!=' not in out['cypher']
    bad = deepcopy(s)
    bad['constraints'].append({'entity_type': 'disease', 'property': 'id', 'operator': '=', 'value': 'MONDO_0005147'})
    assert compile_query(bad) is None
    bad = deepcopy(s)
    bad['sample_requirements']['separate_bindings'] = True
    assert compile_query(bad) is None
    bad = deepcopy(s)
    bad['constraints'].append({'relationship_type': 'HAS_SAMPLE', 'owner_kind': 'relationship', 'property': 'data_source', 'operator': '=', 'value': 'HPAP'})
    assert compile_query(bad) is None
