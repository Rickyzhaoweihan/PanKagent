import unittest
from simple_agent.transport import DevTransport
from simple_agent.resolver import lexical
ROLES={'Author':{'identifiers':['id'],'names':['name'],'aliases':['synonyms'],'context':['description']}}
class Fake(DevTransport):
    def __init__(self,ids=(),words=()):
        super().__init__();self.indexes=[{'name':'id','type':'RANGE','state':'ONLINE','labelsOrTypes':['Author'],'properties':['id']},{'name':'words','type':'FULLTEXT','state':'ONLINE','labelsOrTypes':['Author'],'properties':['name','synonyms']}];self.ids=list(ids);self.words=list(words);self.calls=[]
    async def _raw(self,q,p,c):self.calls.append(q);return {'rows':self.ids,'elapsed_s':.001}
    async def _fulltext(self,i,t,cap=200):self.calls.append((i,t));return {'rows':self.words,'elapsed_s':.001}
class Tests(unittest.IsolatedAsyncioTestCase):
    async def lookup(self,t,text='Ada',**kw):return await t.call('resolve',{'text':text,'search_roles':ROLES,**kw})
    async def test_id_index_no_lexical(self):
        t=Fake(ids=[{'element_id':'1','labels':['Author'],'properties':{'id':'A1'}}]);r=await self.lookup(t,'A1');self.assertEqual(len(t.calls),1);self.assertIn('USING INDEX',t.calls[0]);self.assertEqual(r['candidates'][0]['match_class'],'exact')
    async def test_alias_context_dedup(self):
        n={'element_id':'1','labels':['Author'],'properties':{'synonyms':['Ada'],'description':'context'},'score':1};r=await self.lookup(Fake(words=[n,n]));self.assertEqual(len(r['candidates']),1);self.assertEqual(r['candidates'][0]['matched_fields'][0]['role'],'aliases')
    async def test_wrong_type_no_fallback(self):
        t=Fake();r=await self.lookup(t,label='Journal');self.assertEqual(t.calls,[]);self.assertTrue(r['limitations'])
    async def test_cap_disclosure(self):
        ns=[{'element_id':str(i),'labels':['Author'],'properties':{'name':'Ada'},'score':1} for i in range(200)];r=await self.lookup(Fake(words=ns));self.assertTrue(r['retrieval_cap_reached']);self.assertEqual(len(r['candidates']),12);self.assertIn('before requested-type',r['coverage'])
    async def test_total_cap_stops_additional_indexes(self):
        ns=[{'element_id':str(i),'labels':['Author'],'properties':{'name':'Ada'},'score':1} for i in range(200)]
        t=Fake(words=ns);t.indexes.append({**t.indexes[-1],'name':'second'})
        r=await self.lookup(t);self.assertEqual(len([x for x in t.calls if isinstance(x,tuple)]),1);self.assertTrue(r['retrieval_cap_reached'])
    async def test_omitted_type_visible_as_feedback(self):
        ns=[{'element_id':str(i),'labels':['Author'],'properties':{'name':'Ada'},'score':20-i} for i in range(12)]
        ns.append({'element_id':'other','labels':['Author','Secondary'],'properties':{'name':'Ada'},'score':0})
        r=await self.lookup(Fake(words=ns));self.assertEqual(r['omitted_candidate_types'],['Secondary'])
    async def test_description_unavailable(self):
        t=Fake();r=await self.lookup(t,mode='description');self.assertEqual(t.calls,[]);self.assertTrue(r['limitations'])
    async def test_failure_feedback(self):
        class Broken(Fake):
            async def _fulltext(self,*a):raise RuntimeError('unavailable')
        r=await self.lookup(Broken());self.assertEqual(r['search_status'],'incomplete')
    def test_explicit_fuzzy_and_escaping(self):self.assertEqual(lexical('CTLA-4 *:*','identity'),'"CTLA 4"');self.assertEqual(lexical('leukocite','fuzzy'),'leukocite~1')
