import json
from pathlib import Path
from collections import Counter
from pankagent_vnext.anatomy_resolution import resolve_anatomy, normalize, RELEASE
RECORDS=json.loads((Path(__file__).parent/'fixtures/anatomy_20260907.json').read_text())
def test_entire_inventory_identity_and_name_collisions():
    counts=Counter(normalize(r['name']) for r in RECORDS)
    assert len(RECORDS)==58
    for r in RECORDS:
        by_id=resolve_anatomy(r['id'],RECORDS,RELEASE,'id')
        assert by_id['state']=='resolved' and by_id['id']==r['id']
        by_name=resolve_anatomy(r['name'].upper().replace(' ','_'),RECORDS,RELEASE)
        assert by_name['state']==('resolved' if counts[normalize(r['name'])]==1 else 'ambiguous')
        if by_name['state']=='resolved':assert by_name['id']==r['id']
def test_shared_alias_and_spelling_cases():
    cases={'Β CELLS':'CL_0000169','ductal':'CL_0002079','α cells':'CL_0000171','beta cells':'CL_0000169','ductla cells':'CL_0002079','spleeen':'UBERON_0002106',
           'pancretic acinar cell':'CL_0002064','acinar cells':'CL_0002064','islets':'UBERON_0000006',
           'PLN':'UBERON_0015865','activated pancreatic stellate cell':'CL_0002410_active',
           'quiescent pancreatic stellate cell':'CL_0002410_quiescent','MUC5B+ ductal cells':'CL_0002079_MUC5B'}
    for q,target in cases.items():
        result=resolve_anatomy(q,RECORDS,RELEASE)
        assert result['state']=='resolved' and result['id']==target,(q,result)
def test_unsafe_changes_are_never_automatic():
    for q in ['PLN_B','MUC5A+ ductal cell','active cell','PLN B','islet subregion','not beta cell','liver','CL_0000168']:
        r=resolve_anatomy(q,RECORDS,RELEASE,'id' if q.startswith('CL_') else 'name')
        assert r['state']!='resolved',(q,r)
    assert resolve_anatomy('PLN',RECORDS,'different-release')['state']!='resolved'
    duplicate=RECORDS+[{'id':'new-duplicate','name':'ductal cell','labels':['anatomical_structure']}]
    assert resolve_anatomy('ductla cell',duplicate,RELEASE)['state']=='ambiguous'

def test_systematic_typos_never_select_a_different_record():
    import re
    for record in RECORDS:
        for word in re.findall(r"[a-z]{6,}",record['name']):
            typo=word[:2]+word[3]+word[2]+word[4:]
            question=record['name'].replace(word,typo,1)
            result=resolve_anatomy(question,RECORDS,RELEASE)
            if result['state']=='resolved':assert result['id']==record['id'],(question,result)
            else:assert result['state'] in {'ambiguous','needs_clarification','not_found'}
