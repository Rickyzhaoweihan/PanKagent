from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

import pankagent_vnext.query_templates as templates
from pankagent_vnext.donor_categories import CATEGORICAL_FIELDS
from pankagent_vnext.query_templates import compile_query, runtime_binding_errors
from test_sample_path_templates import add_request_proof, add_runtime_proof, sample_step


def runtime_step(owner, prop, value):
    if (owner, prop) == ('Sample_node', 'data_modality'):
        return sample_step(assay=value)
    step = sample_step(assay='snMultiomics')
    if owner == 'donor':
        step['semantic_registry']['donor_required'] = True
    step['constraints'].append({
        'entity_type': owner,
        'property': prop,
        'operator': '=',
        'value': value,
    })
    kind = ('verified_runtime_source' if prop == 'data_source' else
            'verified_runtime_stage' if prop == 't1d_stage' else
            'verified_runtime_category')
    add_runtime_proof(step, len(step['constraints']) - 1, kind)
    return step


def test_template_source_contains_only_structure_not_known_runtime_values():
    source = Path(templates.__file__).read_text()
    for value in ('MONDO_', 'UBERON_', 'HPAP', 'Control Without Diabetes',
                  'Stage 3', 'scRNA-seq', 'snMultiomics'):
        assert value not in source


def test_active_planning_and_generation_templates_embed_no_entity_ids():
    package = Path(templates.__file__).parent
    detailed_ids = re.compile(
        r'\b(?:MONDO|UBERON|CL|DOID|EFO|HP)_\d+\b|\bENSG\d+\b|\bHPAP-\d+\b|\bPANKREGION:\d+\b')
    for name in ('query_templates.py', 'planning_contract.py', 'graph_patterns.py',
                 'candidate_policy.py', 'llm.py', 'graph_contract.py',
                 'planning_compile.py'):
        source = (package / name).read_text()
        assert not detailed_ids.search(source), name


def test_runtime_inventory_fields_follow_the_donor_category_contract():
    assert templates._RUNTIME_INVENTORY_FIELDS == {
        ('donor', field) for field in CATEGORICAL_FIELDS
    } | {
        ('donor', 't1d_stage'),
        ('Sample_node', 'data_source'),
        ('Sample_node', 'data_modality'),
    }


def test_entity_and_runtime_parameter_bindings_are_auditable_without_values():
    step = runtime_step('donor', 'diabetes_type', 'Control Without Diabetes')
    step['constraints'].append({
        'entity_type': 'donor', 'property': 'data_source',
        'operator': '=', 'value': 'HPAP',
    })
    add_runtime_proof(step, len(step['constraints']) - 1, 'verified_runtime_source')
    compiled = compile_query(step)
    assert compiled and compiled['version'] == 'typed-relation-templates-v5-request-authority'
    assert set(compiled['parameter_bindings']) == set(compiled['parameters'])
    assert {key: compiled['parameter_bindings']['template_0'][key] for key in (
            'constraint_index', 'owner', 'property', 'operator', 'proof_source',
            'proof_kind', 'graph_release')} == {
        'constraint_index': 0,
        'owner': 'anatomical_structure',
        'property': 'id',
        'operator': '=',
        'proof_source': 'resolved_entities',
        'proof_kind': 'current_graph_entity_resolution',
        'graph_release': step['graph_version'],
    }
    assert compiled['parameter_bindings']['template_0']['request_authorization'] == {
        'source': 'immutable_user_request',
        'authorization_kind': 'current_graph_entity_resolution',
        'request_sha256': step['request_filter_bindings'][0]['request_sha256'],
    }
    clinical = compiled['parameter_bindings']['template_2']
    assert clinical['owner'] == 'donor'
    assert clinical['property'] == 'diabetes_type'
    assert clinical['proof_source'] == 'resolved_constraints'
    assert clinical['proof_kind'] == 'verified_runtime_category'
    assert clinical['inventory_sha256'] == step['semantic_registry']['inventory_sha256']
    serialized_bindings = json.dumps(compiled['parameter_bindings'], sort_keys=True)
    for value in compiled['parameters'].values():
        assert value not in compiled['cypher']
        if isinstance(value, str):
            assert value not in serialized_bindings


@pytest.mark.parametrize(('owner', 'prop', 'value'), [
    *(('donor', prop, 'recorded-' + prop) for prop in CATEGORICAL_FIELDS),
    ('donor', 't1d_stage', 'Stage 3: presence of clinical symptoms'),
    ('Sample_node', 'data_source', 'HPAP'),
    ('Sample_node', 'data_modality', 'snMultiomics'),
])
def test_runtime_inventory_fields_cannot_compile_without_matching_live_proof(owner, prop, value):
    step = runtime_step(owner, prop, value)
    target = next(match for match in step['resolved_constraints']
                  if match['canonical_binding']['entity_type'] == owner
                  and match['canonical_binding']['property'] == prop)
    step['resolved_constraints'].remove(target)
    assert compile_query(step) is None
    assert runtime_binding_errors(step) == [
        f'missing_runtime_inventory_binding:{len(step["constraints"]) - 1}:{owner}.{prop}'
    ]


@pytest.mark.parametrize('mutation', [
    {'graph_release': 'stale-release'},
    {'inventory_sha256': 'stale-inventory'},
    {'match_kind': 'reviewed_static_alias'},
])
def test_stale_or_nonruntime_category_proof_is_not_accepted(mutation):
    step = runtime_step('donor', 'diabetes_type', 'Control Without Diabetes')
    proof = next(match for match in step['resolved_constraints']
                 if match['canonical_binding']['property'] == 'diabetes_type')
    proof.update(mutation)
    assert compile_query(step) is None
    assert runtime_binding_errors(step) == [
        'stale_runtime_inventory_binding:2:donor.diabetes_type'
    ]


def test_proof_must_match_the_final_constraint_exactly():
    step = runtime_step('donor', 'derived_diabetes_status', 'Normal')
    proof = next(match for match in step['resolved_constraints']
                 if match['canonical_binding']['property'] == 'derived_diabetes_status')
    proof['canonical_binding'] = deepcopy(proof['canonical_binding'])
    proof['canonical_binding']['value'] = 'Prediabetes'
    assert compile_query(step) is None
    assert runtime_binding_errors(step) == [
        'missing_runtime_inventory_binding:2:donor.derived_diabetes_status'
    ]


def test_runtime_binding_errors_report_missing_digest_and_duplicate_proof_without_values():
    step = runtime_step('donor', 'race', 'Sensitive Runtime Value')
    assert runtime_binding_errors(step) == []
    proof = next(match for match in step['resolved_constraints']
                 if match['canonical_binding']['property'] == 'race')
    step['resolved_constraints'].append(deepcopy(proof))
    errors = runtime_binding_errors(step)
    assert errors == ['duplicate_runtime_inventory_binding:2:donor.race']
    assert 'Sensitive Runtime Value' not in json.dumps(errors)

    step['resolved_constraints'].pop()
    step['semantic_registry'].pop('inventory_sha256')
    assert runtime_binding_errors(step) == [
        'missing_runtime_inventory_digest:1:Sample_node.data_modality',
        'missing_runtime_inventory_digest:2:donor.race',
    ]


def test_runtime_binding_errors_report_identity_failures_for_execution_gate():
    step = sample_step(tissue='SENSITIVE_CURRENT_ID')
    assert runtime_binding_errors(step) == []

    missing = deepcopy(step)
    missing['resolved_entities'] = []
    assert runtime_binding_errors(missing) == [
        'missing_identity_resolution:0:anatomical_structure.id'
    ]

    stale = deepcopy(step)
    stale['resolved_entities'][0]['graph_version'] = 'stale-release'
    assert runtime_binding_errors(stale) == [
        'unverified_resolution:0:anatomical_structure.id'
    ]

    duplicate = deepcopy(step)
    duplicate['resolved_entities'].append(deepcopy(duplicate['resolved_entities'][0]))
    assert runtime_binding_errors(duplicate) == [
        'duplicate_resolution:0:anatomical_structure.id'
    ]

    identity_list = deepcopy(step)
    constraint = identity_list['constraints'][0]
    constraint.update(operator='IN', value=['SENSITIVE_CURRENT_ID', 'SECOND_ID'])
    identity_list['resolved_entities'][0].update(
        requested=deepcopy(constraint), state='literal_predicate')
    identity_list['request_filter_bindings'][0]['canonical_binding'] = deepcopy(constraint)
    errors = runtime_binding_errors(identity_list)
    assert errors == [
        'unverified_identity_resolution:0:anatomical_structure.id'
    ]
    assert 'SENSITIVE_CURRENT_ID' not in json.dumps(errors)

    named = deepcopy(step)
    named_constraint = named['constraints'][0]
    named_constraint.update(property='name', value='Sensitive Current Name')
    named['resolved_entities'][0].update(
        requested=deepcopy(named_constraint), name='Sensitive Current Name')
    named['request_filter_bindings'][0]['canonical_binding'] = deepcopy(named_constraint)
    assert runtime_binding_errors(named) == []
    named['resolved_entities'] = []
    assert runtime_binding_errors(named) == [
        'missing_identity_resolution:0:anatomical_structure.name'
    ]


def test_semantic_template_requires_a_trusted_immutable_request():
    step = sample_step()
    step.pop('semantic_request')
    assert runtime_binding_errors(step) == [
        'missing_trusted_semantic_request:0:anatomical_structure.id',
        'missing_trusted_semantic_request:1:Sample_node.data_modality',
    ]
    assert compile_query(step) is None


@pytest.mark.parametrize(('mutation', 'expected'), [
    ('missing', 'missing_request_authorization:1:Sample_node.data_modality'),
    ('stale_hash', 'stale_request_authorization:1:Sample_node.data_modality'),
    ('stale_release', 'stale_request_authorization:1:Sample_node.data_modality'),
    ('duplicate', 'duplicate_request_authorization:1:Sample_node.data_modality'),
    ('constraint_mismatch', 'missing_request_authorization:1:Sample_node.data_modality'),
])
def test_request_authorization_proof_must_be_unique_current_and_exact(mutation, expected):
    step = sample_step()
    proof = step['request_filter_bindings'][1]
    if mutation == 'missing':
        step['request_filter_bindings'].pop(1)
    elif mutation == 'stale_hash':
        proof['request_sha256'] = 'stale-request'
    elif mutation == 'stale_release':
        proof['graph_release'] = 'stale-release'
    elif mutation == 'duplicate':
        step['request_filter_bindings'].append(deepcopy(proof))
    elif mutation == 'constraint_mismatch':
        proof['canonical_binding'] = deepcopy(proof['canonical_binding'])
        proof['canonical_binding']['operator'] = '!='
    assert expected in runtime_binding_errors(step)
    assert compile_query(step) is None


def test_compiled_audit_has_value_free_request_authority_for_every_parameter():
    step = sample_step()
    compiled = compile_query(step)
    assert compiled
    for parameter, binding in compiled['parameter_bindings'].items():
        assert binding['request_authorization']['source'] == 'immutable_user_request'
        assert binding['request_authorization']['request_sha256']
        assert compiled['parameters'][parameter] not in json.dumps(binding, sort_keys=True)


def test_raw_authorized_non_categorical_donor_filter_uses_request_proof():
    step = sample_step()
    step['semantic_registry']['donor_required'] = True
    step['constraints'].append({'entity_type': 'donor',
        'property': 'other_disease_records', 'operator': 'CONTAINS',
        'value': 'thyroid'})
    add_request_proof(step, 2, 'verified_request_filter')
    compiled = compile_query(step)
    assert compiled and compiled['template_id'] == 'donor_tissue_same_sample_records'
    assert compiled['parameter_bindings']['template_2']['proof_source'] == 'request_filter_bindings'
    assert 'thyroid' not in json.dumps(compiled['parameter_bindings'], sort_keys=True)


def test_generic_nonsemantic_template_remains_compatible_without_request_gate():
    step = {'id': 'generic', 'question': 'Find one gene', 'complete': True,
        'graph_version': templates.REGISTRY['release'], 'relation_types': [],
        'constraints': [], 'resolved_entities': []}
    assert runtime_binding_errors(step) == []
