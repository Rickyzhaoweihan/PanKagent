from pankagent_vnext.tissue_aliases import matched_tissues, PLN_ID, PLN_NAME
VOCAB = [{'id':PLN_ID,'name':PLN_NAME},{'id':'spleen','name':'spleen'}]
def test_pln_requires_verified_proxy_and_keeps_provenance():
    for term in ['PLN','pancreatic lymph node','pancreatic LN']:
        result=matched_tissues('Find HPAP samples for '+term,VOCAB)
        assert len(result)==1 and result[0]['id']==PLN_ID
        assert result[0]['match_kind']=='dataset_proxy'
    assert matched_tissues('PLN',[])==[]
    assert matched_tissues('PLN',[{'id':PLN_ID,'name':'unverified replacement'}])==[]
def test_fractions_and_other_tissues_not_silently_substituted():
    assert matched_tissues('PLN_B',VOCAB)==[]
    assert matched_tissues('PLN B',VOCAB)==[]
    assert len(matched_tissues('PLN and spleen',VOCAB))==2
    assert matched_tissues('spleen',VOCAB)[0]['id']=='spleen'

def test_nested_names_and_repeated_aliases_are_one_tissue():
    vocab=VOCAB+[{'id':'generic','name':'lymph node'},{'id':'pancreas','name':'pancreas'},{'id':'exocrine','name':'exocrine pancreas'}]
    assert [t['id'] for t in matched_tissues('pancreatic lymph node (PLN)',vocab)]==[PLN_ID]
    assert [t['id'] for t in matched_tissues('exocrine pancreas',vocab)]==['exocrine']
    assert len(matched_tissues('exocrine pancreas and pancreas',vocab))==2
    assert len(matched_tissues('pancreatic lymph node and lymph node',vocab))==2
    assert len(matched_tissues('PLN and spleen',vocab))==2

def test_identical_names_with_distinct_ids_remain_ambiguous():
    assert len(matched_tissues('blood',[{'id':'a','name':'blood'},{'id':'b','name':'blood'}]))==2


def test_typed_id_keeps_generated_canonical_description_as_one_tissue():
    vocab=VOCAB+[{'id':'generic','name':'lymph node'}]
    constraints=[{'entity_type':'anatomical_structure','property':'id','value':PLN_ID,'operator':'='}]
    question='pancreatic lymph node (pancreaticosplenic lymph node, UBERON_0015865)'
    result=matched_tissues(question,vocab,constraints=constraints)
    assert [t['id'] for t in result]==[PLN_ID]
    assert len(matched_tissues(question,vocab))==2  # no new global alias


def test_typed_qualified_record_cannot_hide_a_separate_generic_tissue():
    vocab=[{'id':'region','name':'special lymph node (recorded subdivision)'},
           {'id':'generic','name':'lymph node'}]
    constraints=[{'entity_type':'anatomical_structure','property':'id','value':'region'}]
    assert [t['id'] for t in matched_tissues('special lymph node (region)',vocab,constraints=constraints)]==['region']
    assert len(matched_tissues('special lymph node and lymph node',vocab,constraints=constraints))==2


def test_unverified_or_negated_tissue_id_cannot_authorize_description_alias():
    vocab=[{'id':'region','name':'special lymph node (recorded subdivision)'},
           {'id':'generic','name':'lymph node'}]
    for identifier,operator in [('wrong','='),('region','!=')]:
        constraints=[{'entity_type':'anatomical_structure','property':'id','value':identifier,'operator':operator}]
        assert [t['id'] for t in matched_tissues('special lymph node',vocab,constraints=constraints)]==['generic']
