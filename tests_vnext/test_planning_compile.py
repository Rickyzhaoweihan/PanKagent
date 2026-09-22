from copy import deepcopy
import json

import pytest

from pankagent_vnext.planning_compile import compile_property_owners


def context(*candidates, **extra):
    return {'status': 'ready', 'identity': {'graph_release': 'PanKgraph_08_04'},
            'sample_terminology': {'modalities': ['scRNA-seq', 'snMultiomics', 'BCR-seq', 'TCR-seq']},
            'mentions': [{'requested': name, 'state': 'resolved', 'candidates': [
                {'entity_type': kind, 'id': identifier, 'name': name}]} for kind, identifier, name in candidates], **extra}


def plan(*constraints, relation='HAS_SAMPLE'):
    return {'steps': [{'id': 's1', 'question': 'Find recorded sample evidence.', 'relation_types': [relation],
                       'constraints': list(constraints)}]}


def field(prop, value, owner=None, operator='=', **extra):
    return {'property': prop, 'entity_type': owner, 'operator': operator, 'value': value, **extra}


TISSUE = ('anatomical_structure', 'UBERON_0002106', 'Spleen')


def test_raw_sample_fields_compile_before_requested_scope_validation():
    raw = plan(field('anatomical_structure', TISSUE[1]), field('data_modality', 'BCR-seq'),
               field('t1d_stage', 'Stage 3'))
    original = deepcopy(raw)
    compiled, issue = compile_property_owners(raw, context(TISSUE))
    assert issue is None and raw == original
    constraints = compiled['steps'][0]['constraints']
    assert [(c['entity_type'], c['property']) for c in constraints] == [
        ('anatomical_structure', 'id'), ('Sample_node', 'data_modality'), ('donor', 't1d_stage')]
    assert all(c['owner_kind'] == 'node' for c in constraints)
    assert compiled['steps'][0]['constraint_compilation'][0]['requested'] == raw['steps'][0]['constraints'][0]


@pytest.mark.parametrize('operator,value,expected', [
    ('=', 'scrnaseq', 'scRNA-seq'), ('!=', 'multiome', 'snMultiomics'),
    ('<>', 'snMultiomics', 'snMultiomics'),
    ('IN', ['scrnaseq', 'BCR-seq'], ['scRNA-seq', 'BCR-seq']),
    ('NOT IN', '["multiome", "BCR-seq"]', ['snMultiomics', 'BCR-seq']),
])
def test_sample_assay_aliases_preserve_operator_and_all_values(operator, value, expected):
    compiled, issue = compile_property_owners(plan(field('data_modality', value, operator=operator)), context())
    assert issue is None
    result = compiled['steps'][0]['constraints'][0]
    assert result['entity_type'] == 'Sample_node'
    assert result['operator'] == operator and result['value'] == expected


def test_unknown_assay_cannot_silently_choose_edge_owner():
    _, issue = compile_property_owners(plan(field('data_modality', 'unrecognized assay')), context())
    assert issue.startswith('ambiguous_property_owner:s1:data_modality:')


@pytest.mark.parametrize('fieldname', ['data_source', 'data_version', 'id'])
def test_shared_property_needs_an_explicit_owner(fieldname):
    _, issue = compile_property_owners(plan(field(fieldname, 'HPAP')), context())
    assert issue.startswith('ambiguous_property_owner:s1:' + fieldname + ':')


def test_explicit_schema_qualification_is_honored_without_new_scope():
    compiled, issue = compile_property_owners(plan(field('donor.data_source', 'HPAP')), context())
    assert issue is None
    result = compiled['steps'][0]['constraints'][0]
    assert result['entity_type'] == 'donor' and result['property'] == 'data_source'
    assert result['value'] == 'HPAP'


def test_explicit_wrong_owner_is_not_overwritten_with_the_unique_correct_owner():
    _, issue = compile_property_owners(plan(field('t1d_stage', 'Stage 3', 'disease')), context())
    assert issue == 'invalid_property_owner:s1:disease.t1d_stage'


def test_conflicting_node_and_edge_owners_fail():
    _, issue = compile_property_owners(plan(field('data_source', 'HPAP', 'donor', relationship_type='HAS_SAMPLE')), context())
    assert issue.startswith('conflicting_property_owners:')


def test_qtl_tissue_is_owned_by_the_selected_relationship():
    compiled, issue = compile_property_owners(plan(field('tissue_name', 'Pancreas'), relation='PART_OF_QTL_SIGNAL'), context())
    assert issue is None
    assert compiled['steps'][0]['constraints'][0]['relationship_type'] == 'PART_OF_QTL_SIGNAL'


@pytest.mark.parametrize('operator,value,prop,expected', [
    ('=', 'pancreas', 'tissue_name', 'Pancreas'),
    ('=', 'UBERON_0001264', 'tissue_id', 'UBERON_0001264'),
    ('!=', 'pancreas', 'tissue_name', 'Pancreas'),
    ('<>', 'islet', 'tissue_name', 'Islet'),
    ('IN', ['pancreas', 'islet'], 'tissue_name', ['Pancreas', 'Islet']),
])
def test_generic_qtl_tissue_alias_uses_verified_category_and_preserves_operator(operator, value, prop, expected):
    raw = plan(field('tissue', value, operator=operator), relation='PART_OF_QTL_SIGNAL')
    result, issue = compile_property_owners(raw, context())
    assert issue is None
    compiled = result['steps'][0]['constraints'][0]
    assert compiled == {'property': prop, 'entity_type': None, 'operator': operator,
                        'value': expected, 'owner_kind': 'relationship', 'relationship_type': 'PART_OF_QTL_SIGNAL'}
    assert result['steps'][0]['constraint_compilation'][0]['requested'] == raw['steps'][0]['constraints'][0]


@pytest.mark.parametrize('value', ['unrecorded tissue', ['pancreas', 'UBERON_0001264']])
def test_unknown_or_mixed_name_and_id_qtl_tissue_alias_stays_unresolved(value):
    _, issue = compile_property_owners(plan(field('tissue', value, operator='IN' if isinstance(value, list) else '='),
                                          relation='PART_OF_QTL_SIGNAL'), context())
    assert issue.startswith('unknown_property_owner:')


def test_explicit_owner_kind_disambiguates_shared_property_without_node_preference():
    raw = plan(field('data_source', 'GTEx; SusieR', owner_kind='relationship'), relation='PART_OF_QTL_SIGNAL')
    result, issue = compile_property_owners(raw, context())
    assert issue is None
    assert result['steps'][0]['constraints'][0]['relationship_type'] == 'PART_OF_QTL_SIGNAL'
    assert result['steps'][0]['constraints'][0]['entity_type'] is None


@pytest.mark.parametrize('constraint', [field('nominal_p', '.05', owner_kind='node'),
    field('tissue', 'pancreas', owner_kind='node'),
    field('id', 'ENSG00000134460', 'Gene', owner_kind='relationship'),
    field('tissue_name', 'Pancreas', owner_kind='node', relationship_type='PART_OF_QTL_SIGNAL')])
def test_explicit_wrong_owner_kind_is_never_replaced_by_another_owner(constraint):
    result, issue = compile_property_owners(plan(constraint, relation='PART_OF_QTL_SIGNAL'), context())
    assert issue and ('property_owner' in issue)


def test_pre_normalized_three_relation_coloc_keeps_all_three_explicit_identities():
    identities = [('Gene', 'ENSG00000138031', 'ADCY3'), ('variants', 'rs13393590', 'rs13393590'),
                  ('disease', 'MONDO_0005147', 'type 1 diabetes')]
    raw = plan(*(field('id', identifier, kind) for kind, identifier, _ in identities))
    raw['steps'][0]['relation_types'] = ['SIGNAL_COLOC_WITH', 'PART_OF_GWAS_SIGNAL', 'PART_OF_QTL_SIGNAL']
    result, issue = compile_property_owners(raw, context(*identities))
    assert issue is None
    assert [constraint['entity_type'] for constraint in result['steps'][0]['constraints']] == ['Gene', 'variants', 'disease']


def test_generic_unique_go_property_is_resolved_without_question_specific_rules():
    compiled, issue = compile_property_owners(plan(field('go_domain', 'molecular_function'), relation='ASSOCIATED_WITH_GO'), context())
    assert issue is None
    assert compiled['steps'][0]['constraints'][0]['entity_type'] == 'GO_term'


def test_literal_identity_must_be_verified_and_same_type_for_all_values():
    for candidate in [context(), context(TISSUE, ('Gene', TISSUE[1], 'Colliding record'))]:
        _, issue = compile_property_owners(plan(field('id', TISSUE[1])), candidate)
        assert issue.startswith('ambiguous_property_owner:')
    raw = plan(field('anatomical_structure', TISSUE[1]))
    assert compile_property_owners(raw, context())[1].startswith('ambiguous_property_owner:')


def test_multiple_grounded_anatomy_ids_retain_in_operator():
    other = ('anatomical_structure', 'UBERON_0001264', 'Pancreas')
    raw = plan(field('anatomical_structure', json.dumps([TISSUE[1], other[1]]), operator='IN'))
    result, issue = compile_property_owners(raw, context(TISSUE, other))
    assert issue is None
    c = result['steps'][0]['constraints'][0]
    assert c['operator'] == 'IN' and c['value'] == [TISSUE[1], other[1]]
    assert c['property'] == 'id'


@pytest.mark.parametrize('state', ['unavailable', 'stale'])
def test_unavailable_or_different_release_grounding_does_not_guess(state):
    data = context()
    if state == 'unavailable':
        data['status'] = state
    else:
        data['identity']['graph_release'] = 'other'
    raw = plan(field('data_modality', 'snMultiomics'))
    result, issue = compile_property_owners(raw, data)
    assert result == raw and issue is None


def test_compilation_is_idempotent_and_keeps_original_provenance():
    first, issue = compile_property_owners(plan(field('data_modality', 'scRNAseq')), context())
    second, issue2 = compile_property_owners(first, context())
    assert issue is issue2 is None and first == second
