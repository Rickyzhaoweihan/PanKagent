import asyncio,copy,json
import pytest
from pankagent_query.core import SchemaPack,Agent,compile_pattern,Feedback
from pankagent_query.evidence import handoff,merge_owned


def make_pack(tmp_path,label='Entity'):
    data={'release':'fixture-release','identity':{'input_manifest_sha256':'fixture'},
          'nodes':{label:['id','tags','name']},'relations':{},'search_roles':{},'notes':[],
          'property_types':{'node':{label:{'id':'string','tags':'string[]'}},'edge':{}},
          'sql_tables':{'metrics':{'all_fields':['value','source','version'],'filters':['condition']}},
          'linked_evidence':{'profiles':{'node:'+label:{'table_ids':['metrics']}}}}
    p=tmp_path/'schema.json';p.write_text(json.dumps(data));return SchemaPack(p)


def pattern(label='Entity'):
    return {'nodes':[{'key':'x','label':label,'filters':[{'property':'id','op':'eq','value':'A'}]}],'edges':[]}


def test_generic_array_membership_and_schema_types(tmp_path):
    p=make_pack(tmp_path,'Book');x=pattern('Book');x['nodes'][0]['filters']=[{'property':'tags','op':'has','value':'history'}]
    q,v=compile_pattern(p,x);assert '$p0 IN n0.`tags`' in q and v['p0']=='history'
    assert p.view(labels=['Book'])['property_types']['node']['Book']['tags']=='string[]'
    x['nodes'][0]['filters'][0]['property']='id'
    with pytest.raises(Feedback,match='array properties'):compile_pattern(p,x)


def test_sql_schema_is_definition_not_records(tmp_path):
    p=make_pack(tmp_path);v=p.view(tables=['metrics']);assert 'all_fields' in v['sql_tables']['metrics'];assert 'record access' in v['ownership']
    with pytest.raises(Feedback,match='Unknown registered'):p.view(tables=['guessed_table'])


def test_selected_schema_exposes_declared_owner_profiles_and_precise_types(tmp_path):
    p=make_pack(tmp_path)
    p.data['linked_evidence']['profiles']['node:Entity']['match']={'labels_all':['Entity']}
    p.data['linked_evidence']['profiles']['node:Other']={'match':{'labels_all':['Other']},'table_ids':['private']}
    p.data['node_variants']={'Specific':{'required_labels':['Entity','Specific'],'property_types':{'tags':'string[]'}}}
    view=p.view(labels=['Entity'],linked_evidence=True)
    assert list(view['linked_evidence']['profiles'])==['node:Entity']
    assert view['node_variants']['Specific']['property_types']['tags']=='string[]'


def ref(id='A',kind='node'):
    return {'kind':kind,'type_id':'Entity','id':id}


def graph(id='A'):
    return {'nodes':[{'id':id,'labels':['Entity'],'properties':{'id':id},'ref':ref(id)}],'edges':[]}


def test_owner_filter_does_not_borrow_kind_or_endpoint():
    assert merge_owned([{'ref':ref('A','edge'),'pages':{'metrics':{'records':[1]}}}],graph())==[]
    assert merge_owned([{'ref':ref('B'),'pages':{'metrics':{'records':[1]}}}],graph())==[]


def query(id='A'):
    return {'graph':graph(id),'pattern':pattern(),'cypher':'MATCH(n) RETURN n','parameters':{},'status':'complete',
            'linked_evidence':[{'ref':ref(id),'tables':[{'id':'metrics'}]}],
            'evidence_reads':[{'ref':ref(id),'table_id':'metrics','response':{'records':[{'value':0,'other':None,'version':'original'}]}}]}


def test_selection_keeps_only_owned_records_and_enrich_deduplicates():
    queries={'q1':query(),'q2':query('B')};selection=[{'query_id':'q1','purpose':'requested'}]
    x=handoff(queries,selection,{'status':'complete'});assert len(x['evidence_reads'])==1 and len(x['linked_evidence'])==1
    assert x['evidence_reads'][0]['response']['records'][0]=={'value':0,'other':None,'version':'original'}
    y=handoff(queries,selection,{'status':'complete'},base=x);assert len(y['evidence_reads'])==1


def tools(*pairs):
    return {'content':[{'type':'tool_use','id':str(i),'name':n,'input':a} for i,(n,a) in enumerate(pairs)]}


class Fake:
    def __init__(self,messages):self.messages=list(messages);self.evidence_calls=0;self.detail=None
    async def call(self,action,args):
        if action=='model':return self.messages.pop(0)
        if action=='query':
            self.detail=args['detail'];return {**query(),'status':'complete','row_count':1,'truncated':False,'omitted_records':0,'execution_s':0}
        if action=='evidence':
            self.evidence_calls+=1;return {'ref':args['ref'],'table_id':'metrics','state':'available','records':[{'value':0,'missing':None,'data_version':'2020-source'}]}
        raise AssertionError(action)


def run_agent(tmp_path,foreign=False):
    pack=make_pack(tmp_path);tr=Fake([
        tools(('query',{'pattern':pattern(),'standardized_question':'Find entity A','detail':'summary'})),
        tools(('inspect_result',{'query_id':'q1','kind':'evidence','ref':ref('B' if foreign else 'A'),'table_id':'metrics','fields':['value']})),
        tools(('finish',{'status':'complete','standardized_question':'Find entity A','answer':'Found A','query_id':'q1'}))])
    x=asyncio.run(asyncio.wait_for(Agent(pack,tr,model='fixture').run('Find A'),3))
    return x,tr


def test_agent_sql_read_retained(tmp_path):
    x,tr=run_agent(tmp_path);assert tr.evidence_calls==1 and tr.detail=='summary'
    assert any(r['response'].get('records',[{}])[0].get('data_version')=='2020-source' for r in x['evidence_reads'])


def test_agent_foreign_ref_rejected_before_transport(tmp_path):
    x,tr=run_agent(tmp_path,True);assert tr.evidence_calls==0
    assert any('exact graph object' in str(e) for e in x['error_records'])
