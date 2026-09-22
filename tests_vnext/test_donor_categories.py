from copy import deepcopy
import json
import pytest
from pankagent_vnext.donor_categories import resolve_categories
from pankagent_vnext.semantic_registry import resolve, PROPERTIES, RELEASE

V = {'donor_categories_complete': True, 'donor_categorical_values': {
    'gender': ['Female', 'Male'], 'sex_at_birth': ['F', 'M'], 'race': ['White', 'Asian']}}

def constraint(value='female', owner=None, field='gender', op='='):
    return {'entity_type': owner, 'property': field, 'operator': op, 'value': value}

def normalize(c, vocabulary=V):
    return resolve_categories([c], vocabulary, PROPERTIES, RELEASE)

def test_verified_case_binding_preserves_original_and_owner():
    c=constraint(); old=deepcopy(c)
    resolved, matches=normalize(c)
    assert c==old
    assert resolved[0]==constraint('Female', 'donor')
    assert matches[0]['requested']['entity_type'] is None
    assert matches[-1]['requested']['value']=='female'
    assert matches[-1]['canonical_binding']['value']=='Female'
    assert matches[-1]['match_kind']=='verified_runtime_case_category'

@pytest.mark.parametrize('c', [constraint('Female','disease'), constraint('F'),
    constraint('female','donor','sex_at_birth'), constraint('femle'), constraint('female',op='CONTAINS')])
def test_no_wrong_owner_cross_field_fuzzy_or_operator_substitution(c):
    resolved, matches=normalize(c)
    assert resolved[0]['value']==c['value']
    assert not any(m['match_kind']=='verified_runtime_case_category' for m in matches)

@pytest.mark.parametrize('values', [['Female','FEMALE'], [None], 'Female'])
def test_ambiguous_or_invalid_inventory_is_not_an_alias(values):
    v={**V,'donor_categorical_values':{'gender':values}}
    assert normalize(constraint(),v)[0][0]['value']=='female'

def test_incomplete_inventory_does_not_infer_case():
    assert normalize(constraint(),{**V,'donor_categories_complete':False})[0][0]['value']=='female'

def test_set_case_alias_is_all_or_nothing_and_retains_operator():
    c=constraint(json.dumps(['female','male']),op='IN')
    resolved,_=normalize(c)
    assert json.loads(resolved[0]['value'])==['Female','Male'] and resolved[0]['operator']=='IN'
    c['value']=json.dumps(['female','unknown'])
    assert normalize(c)[0][0]['value']==c['value']

def test_property_owner_is_unique_and_does_not_bind_shared_source():
    assert normalize(constraint('100',field='age',op='<'))[0][0]['entity_type']=='donor'
    assert normalize(constraint('HPAP',field='data_source'))[0][0]['entity_type'] is None

def test_live_semantic_resolution_uses_labels_without_new_filters():
    step={'id':'s1','question':'List female donors younger than 100',
          'relation_types':['HAS_DONOR'],'constraints':[constraint(),constraint('100',field='age',op='<')]}
    r=resolve(step,V,RELEASE)
    assert r['constraints']==[constraint('Female','donor'),constraint('100','donor','age','<')]
    assert not r['semantic_issues']

def test_unknown_release_does_not_use_inventory():
    c=constraint()
    assert resolve_categories([c],V,PROPERTIES,'other-release')==([c],[])
