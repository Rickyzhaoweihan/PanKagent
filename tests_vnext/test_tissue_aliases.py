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
