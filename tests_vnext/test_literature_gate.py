from pankagent_vnext.literature_gate import literature_gate
P={'literature':True,'steps':[{'id':'s1'}]}
def ev(status='complete',nodes=None,rows=None,purpose='primary'):
    return {'steps':[{'status':status,'nodes':nodes or [],'rows':rows or [],'purpose':purpose}]}
def test_requires_new_primary_evidence_and_answer():
    for evidence in (ev('empty'),ev('failed',nodes=[{'id':'x'}]),ev(nodes=[{'id':'x'}],purpose='context'),ev(rows=[{'nodes':[],'edges':[]}]),ev(rows=[{'count':0}])):
        assert literature_gate(P,evidence,'No matches')[0] is False
    assert literature_gate(P,ev(nodes=[{'id':'x'}]),'')[0] is False
    assert literature_gate({**P,'steps':[]},ev(nodes=[{'id':'x'}]),'PIP means...')[0] is False
    assert literature_gate({**P,'answer_mode':'skills'},ev(nodes=[{'id':'x'}]),'PIP means...')[0] is False

def test_graph_and_positive_scalar_answer_can_be_enriched():
    assert literature_gate(P,ev(nodes=[{'id':'x'}]),'Supported answer')[0]
    assert literature_gate(P,ev(rows=[{'donors':10}]),'10 donors')[0]
