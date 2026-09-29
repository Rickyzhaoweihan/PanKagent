from copy import deepcopy
import pytest
from pankagent_vnext.agent_schemas import module
from pankagent_vnext.coordinate_lookup import draft, lookup, verified_spec


def spec():
    d=deepcopy(module('database_schema')['postgresql'])
    d['enabled']=True
    d['verification'].update(verified=True,assembly='fixture-build',coordinate_conventions={
        'ensembl_genes_node':'1-based_inclusive','ocr_peak_node':'0-based_half-open'})
    return d


class Cursor:
    def __init__(self, peaks):self.calls=[];self.peaks=peaks
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,query,params):self.calls.append((query,params))
    def fetchmany(self,n):return ([('gene-x','7',101,200)] if len(self.calls)==2 else self.peaks)[:n]
class Connection:
    def __init__(self,peaks):self.c=Cursor(peaks);self.rolled_back=False;self.closed=False
    def cursor(self):return self.c
    def set_session(self,**kw):assert kw=={'readonly':True,'isolation_level':'REPEATABLE READ','autocommit':False}
    def rollback(self):self.rolled_back=True
    def close(self):self.closed=True


def test_sequential_bound_parameters_and_normalized_boundary():
    c=Connection([('peak-a','7',99,101),('peak-b','7',199,205)])
    r=lookup(c,spec(),module('query_patterns')['coordinate_lookup'],"gene'input",10,1000)
    assert c.c.calls[1][1]==("gene'input",)
    assert "gene'input" not in c.c.calls[1][0]
    assert c.c.calls[2][1]==('7',0,200,100)
    assert len(r['peaks'])==2 and c.closed and c.rolled_back
    assert not any('LIMIT' in query or 'ORDER BY' in query for query,_ in c.c.calls)


@pytest.mark.parametrize('peaks,reason',[
    ([('outside','7',0,100)],'overlap_verification'),
    ([('wrong-chromosome','8',101,120)],'overlap_verification'),
    ([('bad-number','7','101',120)],'coordinate_types'),
    ([('a','7',101,120),('b','7',102,121)],'materialization_limit'),
])
def test_bad_or_incomplete_lookup_cannot_be_complete(peaks,reason):
    c=Connection(peaks)
    with pytest.raises(ValueError,match=reason):lookup(c,spec(),module('query_patterns')['coordinate_lookup'],'g',1,1000)
    assert c.closed and c.rolled_back


def test_unverified_current_mapping_stays_disabled():
    with pytest.raises(ValueError,match='mapping_unverified'):verified_spec(module('database_schema')['release'])


def test_around_requests_clarification_and_overlap_uses_grounded_identity():
    grounding={'status':'ready','identity':{'graph_release':module('database_schema')['release']},
        'mentions':[{'requested':'OtherGene','state':'resolved','candidates':[{'entity_type':'Gene','id':'gene-x'}]}]}
    assert draft('Find 50 OCR peaks around OtherGene',grounding)['clarification']
    p=draft('Find 50 OCR peaks overlapping OtherGene',grounding)
    assert p['steps'][0]['coordinate_lookup']['display_count']==50
    assert p['steps'][0]['complete']
    assert p['steps'][0]['constraints'][0]['value']=='gene-x'
    assert draft('Find OCR peaks overlapping OtherGene in beta cells',grounding) is None


def test_coordinate_plan_reaches_normal_scope_validation():
    import asyncio
    from test_planning_compiler_gateway import gateway_for
    grounding={'status':'ready','identity':{'graph_release':module('database_schema')['release']},
        'mentions':[{'requested':'OtherGene','state':'resolved','candidates':[
            {'entity_type':'Gene','labels':['Gene'],'id':'gene-x','name':'OtherGene'}]}]}
    question='Find OCR peaks overlapping OtherGene'
    async def check():
        gateway,_=gateway_for(lambda _:draft(question,grounding))
        p=await gateway.plan(question,[],grounding=grounding)
        assert not p.get('proposal_issue') and not p.get('clarification')
        assert p['steps'][0]['coordinate_lookup']['selection']=='overlap'
    asyncio.run(check())
