from pankagent_vnext.donor_query_guard import normalize_diagnosis,sample_path_errors

def test_canonical_diagnosis_does_not_change_stage_or_other_constraints():
    step={'constraints':[{'entity_type':'donor','property':'diabetes_type','operator':'=','value':'type 1 diabetes'}, {'entity_type':'donor','property':'t1d_stage','operator':'=','value':'stage fixture'}]}
    result=normalize_diagnosis(step, {'donor_categories_complete': True,
        'donor_categorical_values': {'diabetes_type': ['Diabetes (Type I)', 'Diabetes (Type II)']},
        'inventory_sha256': 'fixture'})
    assert result['constraints'][0]['value']=='Diabetes (Type I)'
    assert result['constraints'][1]==step['constraints'][1]
    assert step['constraints'][0]['value']=='type 1 diabetes'

def test_wrong_sample_direction_is_rejected_even_without_a_tissue_filter():
    binding={'d':{'donor'},'s':{'Sample_node'},'a':{'anatomical_structure'}}
    assert sample_path_errors(binding,[('s','a',{'HAS_SAMPLE'})])
    assert not sample_path_errors(binding,[('a','s',{'HAS_SAMPLE'}),('d','s',{'HAS_SAMPLE'})])
