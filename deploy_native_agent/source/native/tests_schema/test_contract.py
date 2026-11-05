import asyncio,copy,json,tempfile,unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pankagent_query.core import Agent,SchemaPack,Feedback,compile_pattern,SYSTEM,TOOLS
from pankagent_query.identity import validate_bindings,reject_broadened_request,missing_scope_alternatives
from pankagent_query.formatting import prepare
from pankagent_query.semantics import augment,interpretation

PACK={'release':'fixture','nodes':{'Object':['id','name','external'],'Category':['id','name'],'Term':['id','name']},
 'relations':{k:{'properties':[],'paths':[{'source':['Object'],'target':[target]}],'allowed_traversal':dirs,'description':k} for k,target,dirs in [('LINK_A','Object',['out','either']),('LINK_B','Object',['out','either']),('MEMBER','Category',['out']),('ANNOTATED','Term',['out'])]},
 'identity_fields':{'Object':{'id':{'role':'primary','namespace':'object'},'external':{'role':'external','namespace':'other'}}},
 'search_roles':{'Object':{'identifiers':['id','external'],'names':['name'],'aliases':[]}},'interpretation':{'aliases':{},'rules':[{'id':'member-rule','match':{'edges_any':['MEMBER']},'text':'Membership does not prove activity.'}]},'semantics_version':'test'}

def pattern(link='LINK_A',target='Category',annotation='MEMBER'):
 return {'nodes':[{'key':'x','label':'Object','filters':[{'property':'id','op':'eq','value':'a'}]},{'key':'p','label':'Object'},{'key':'c','label':target}], 'edges':[{'key':'i','source':'x','target':'p','type':link,'direction':'either'},{'key':'f','source':'p','target':'c','type':annotation}]}
def call(name,args):return [{'type':'tool_use','id':'c','name':name,'input':args}]
def query(purpose='requested',pat=None):return call('query',{'purpose':purpose,'pattern':pat or pattern(),'standardized_question':'original scope'})
def select(ids=('q1',),purpose='requested'):return call('select_results',{'selections':[{'query_id':q,'purpose':purpose} for q in ids]})
def finish(status='empty'):return call('finish',{'status':status,'standardized_question':'original scope','answer':'Outcome.'})
class Transport:
 def __init__(self,steps,nonempty=False):self.steps=iter(steps);self.calls=[];self.nonempty=nonempty;self.final_retries=0;self.last_finish=None
 async def call(self,action,args):
  self.calls.append((action,copy.deepcopy(args)))
  if action=='model':
   try:decision=next(self.steps)
   except StopIteration:
    self.final_retries+=1
    if self.final_retries>2 or not self.last_finish:raise AssertionError('Unexpected model call')
    decision=self.last_finish
   if decision[0]['name']=='finish':self.last_finish=decision
   return {'content':decision,'usage':{}}
  if action=='query':return {'status':'complete' if self.nonempty else 'empty','graph':{'nodes':[{'id':'a','labels':['Object']} ] if self.nonempty else [],'edges':[]},'row_count':1 if self.nonempty else 0,'truncated':False}
  raise AssertionError(action)
class Contract(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name)/'pack.json';p.write_text(json.dumps(PACK));self.pack=SchemaPack(p)
 def tearDown(self):self.tmp.cleanup()
 async def run_steps(self,steps,nonempty=False,seed=None):
  t=Transport(steps,nonempty);r=await Agent(self.pack,t).run('original scope',seed=seed,phase='enrich' if seed else 'prepare');return r,t
 def test_index_has_endpoints_and_directions(self):
  v=self.pack.view();self.assertEqual(v['relationship_index']['LINK_B']['allowed_traversal'],['out','either']);self.assertIn('identity_fields',v)
 def test_no_dataset_literals_in_prompt(self):
  self.assertIn('An empty prefix is NOT a reason to skip',SYSTEM)
  for word in ['HLA','Gene','GO_term','BioGRID','Reactome','PHYSICAL_INTERACTION','QTL']:self.assertNotIn(word,SYSTEM)
 def test_direction_and_endpoint_validation(self):
  p=pattern();p['edges'][1]['direction']='either'
  with self.assertRaises(Feedback):compile_pattern(self.pack,p)
  p=pattern(target='Term')
  with self.assertRaises(Feedback):compile_pattern(self.pack,p)
 def test_same_partner_join(self):
  q,_=compile_pattern(self.pack,pattern());self.assertIn('(n1)-[r1:',q);self.assertIn(']-(n1)',q)
 def test_external_not_primary(self):
  contexts=[{'tool':'resolve_entity','output':{'candidates':[{'labels':['Object'],'properties':{'id':'a','external':['42'],'name':'Alpha'}}]}}]
  p=pattern();p['nodes'][0]['filters'][0]['value']='42'
  with self.assertRaisesRegex(Feedback,'another identity'):validate_bindings(PACK,p,contexts)
  p['nodes'][0]['filters'][0]['value']='a';validate_bindings(PACK,p,contexts)
 async def test_diagnostic_cannot_be_selected(self):
  r,t=await self.run_steps([query('diagnostic'),select(),query(),select(('q2',)),finish()]);self.assertTrue(any('Diagnostic' in e['message'] for e in r['error_records']));self.assertEqual([s['query_id'] for s in r['selected_queries']],['q2'])
 async def test_purpose_required_and_legacy_finish_rejected(self):
  q=query();q[0]['input'].pop('purpose');bad=finish();bad[0]['input']['query_ids']=['q1']
  r,t=await self.run_steps([q,query(),bad,select(),finish()]);self.assertEqual(len(r['error_records']),2);self.assertEqual(sum(a=='query' for a,_ in t.calls),1)
 async def test_exact_query_reused(self):
  r,t=await self.run_steps([query(),query(),select(),finish()]);self.assertEqual(sum(a=='query' for a,_ in t.calls),1)
 async def test_both_alternative_types_retained(self):
  r,t=await self.run_steps([query(),query(pat=pattern('LINK_B')),select(('q1','q2')),finish()]);self.assertEqual(len(r['retrieval_checks']['queries']),2);self.assertEqual({e['type'] for e in r['metapath']['edges']},{'LINK_A','LINK_B','MEMBER'})
 async def test_category_vs_term(self):
  for p in [pattern(),pattern(target='Term',annotation='ANNOTATED')]:
   r,_=await self.run_steps([query(pat=p),select(),finish()]);self.assertEqual(r['metapath']['nodes'][2]['label'],p['nodes'][2]['label'])
 async def test_nonempty_alternatives(self):
  for k in ['LINK_A','LINK_B']:
   r,_=await self.run_steps([query(pat=pattern(k)),select(),finish('complete')],True);self.assertEqual(r['retrieval_status'],'complete')
 async def test_schema_formatter_empty_status(self):
  r,_=await self.run_steps([query(),select(),finish()]);p=prepare('original scope',r,'fixture');b=json.loads(p.body);self.assertEqual(b['retrieval_checks']['schema_validation'],'passed');self.assertTrue(b['retrieval_checks']['pagination_complete']);self.assertIn('member-rule',p.profile['selected_rules'])
 async def test_context_version_rejects_stale(self):
  r,_=await self.run_steps([query(),select(),finish()]);r['context_identity']['schema_hash']='old'
  with self.assertRaisesRegex(Feedback,'another schema'):await self.run_steps([],seed=r)
 async def test_same_context_reuses_queries(self):
  r,_=await self.run_steps([query(),select(),finish()]);r2,t=await self.run_steps([query(),finish()],seed=r);self.assertEqual(sum(a=='query' for a,_ in t.calls),0);self.assertEqual(r2['context_identity'],r['context_identity'])
 async def test_unfulfilled_condition_marks_partial(self):
  f=finish();f[0]['input']['unfulfilled_conditions']=['unresolved requested branch']
  r,_=await self.run_steps([query(),select(),f]);self.assertEqual(r['status'],'partial');self.assertEqual(r['retrieval_checks']['condition_coverage'],'incomplete')
 async def test_context_purpose_cannot_be_promoted(self):
  r,_=await self.run_steps([query('context'),select(),query(),select(('q2',)),finish()]);self.assertTrue(any('purpose' in e['message'] for e in r['error_records']))
 async def test_formatter_does_not_infer_legacy_rules(self):
  r,_=await self.run_steps([query(),select(),finish()]);p=prepare('original scope',r,'fixture')
  self.assertNotIn('QTL',p.system[0]['text']);self.assertNotIn('donor',p.system[0]['text'])
  with self.assertRaises(ValueError):prepare('x',{**r,'schema_context':None},'fixture')
 def test_source_rule_texts_preserved(self):
  data=json.loads((ROOT/'schema_semantics.json').read_text())
  self.assertEqual(len(data['interpretation']['rules'][:len(data['provenance']['bundle_manifest']['rules'])]),len(data['provenance']['bundle_manifest']['rules']))
 def test_relaxed_probe_cannot_replace_requested(self):
  p=pattern();old=copy.deepcopy(p);old['nodes'][2]['filters']=[{'property':'id','op':'eq','value':'category1'}]
  with self.assertRaisesRegex(Feedback,'removes constraints'):reject_broadened_request(p,{'q1':{'purpose':'requested','pattern':old}})
  reject_broadened_request(pattern('LINK_B'),{'q1':{'purpose':'requested','pattern':old}})
 async def test_colabel_fulltext_preserves_requested_label(self):
  from pankagent_query.resolver import resolve
  class Index:
   async def initialize(self):return [{'state':'ONLINE','type':'FULLTEXT','name':'parent_idx','labelsOrTypes':['Parent'],'properties':['name']}]
   async def _fulltext(self,*args):return {'rows':[{'element_id':'1','labels':['Parent','Child'],'properties':{'id':'x','name':'Concept'}},{'element_id':'2','labels':['Parent','Other'],'properties':{'id':'y','name':'Concept'}}]}
  r=await resolve(Index(),{'text':'Concept','label':'Child','search_roles':{l:{'identifiers':['id'],'names':['name'],'aliases':[]} for l in ['Parent','Child']},'node_variants':{'child':{'label_sets':{'Parent;Child':1}}}})
  self.assertEqual([c['properties']['id'] for c in r['candidates']],['x']);self.assertTrue(r['search_attempts'])
 def test_schema_families_prevent_silent_subtype_narrowing(self):
  pack=copy.deepcopy(PACK);pack['scope_families']=[{'id':'links','members':[{'relation':'LINK_A','qualifiers':['alpha']},{'relation':'LINK_B','qualifiers':['beta']}]}]
  qs={'q1':{'pattern':pattern(),'purpose':'requested'}};ss=[{'query_id':'q1','purpose':'requested'}]
  self.assertEqual(missing_scope_alternatives(pack,'Find related objects',qs,ss),[{'relation':'LINK_B'}])
  self.assertEqual(missing_scope_alternatives(pack,'Find alpha links',qs,ss),[])
  self.assertTrue(missing_scope_alternatives(pack,'Find alpha or beta links',qs,ss))
  qs['q2']={'pattern':pattern('LINK_B'),'purpose':'diagnostic'}
  self.assertTrue(missing_scope_alternatives(pack,'Find related objects',qs,ss+[{'query_id':'q2','purpose':'context'}]))
 def test_joined_scope_requires_all_combinations(self):
  pack={**PACK,'scope_families':[{'members':[{'relation':'LINK_A'},{'relation':'LINK_B'}]},{'members':[{'relation':'MEMBER','target':'Category'},{'relation':'ANNOTATED','target':'Term'}]}]}
  qs={};ss=[]
  for k,p in enumerate([pattern(),pattern('LINK_B'),pattern(target='Term',annotation='ANNOTATED')]):
   key=str(k);qs[key]={'purpose':'requested','pattern':p};ss.append({'purpose':'requested','query_id':key})
  missing=missing_scope_alternatives(pack,'original scope',qs,ss)
  self.assertEqual(missing,[{'joined_alternatives':[{'relation':'LINK_B'},{'relation':'ANNOTATED','target':'Term'}]}])
  qs['3']={'purpose':'requested','pattern':pattern('LINK_B','Term','ANNOTATED')};ss.append({'purpose':'requested','query_id':'3'})
  self.assertEqual(missing_scope_alternatives(pack,'original scope',qs,ss),[])
 async def test_stale_history_drops_query_context(self):
  t=Transport([query(),select(),finish()])
  await Agent(self.pack,t).run('original scope',history=[{'question':'previous user text','metapath':{'old':'type'},'answer':'old claims','context_identity':{'schema_hash':'old'}}])
  message=next(args['messages'][0] for action,args in t.calls if action=='model')
  self.assertIn('previous user text',message['content']);self.assertNotIn('old claims',message['content']);self.assertNotIn('metapath',message['content'])
 def test_ambiguous_name_cannot_choose_one_id(self):
  ctx=[{'tool':'resolve_entity','input':{'text':'Alpha'},'output':{'candidates':[{'labels':['Object'],'properties':{'id':x,'name':'Alpha'}} for x in ['a','b']]}}]
  with self.assertRaisesRegex(Feedback,'multiple distinct'):validate_bindings(PACK,pattern(),ctx)
  with self.assertRaisesRegex(Feedback,'multiple distinct'):validate_bindings(PACK,pattern(),ctx,'Find Alpha')
  p=pattern();p['nodes'][0]['filters'][0].update(op='in',value=['a','b']);validate_bindings(PACK,p,ctx)
 def test_source_switch_keeps_qualifying_concept(self):
  pack={**PACK,'scope_families':[{'members':[{'relation':'MEMBER','target':'Category'},{'relation':'ANNOTATED','target':'Term'}]}]}
  old=pattern();old['nodes'][2]['filters']=[{'property':'id','op':'eq','value':'c'}]
  with self.assertRaisesRegex(Feedback,'qualifying endpoint'):reject_broadened_request(pattern(target='Term',annotation='ANNOTATED'),{'q1':{'purpose':'requested','pattern':old}},pack)
 async def test_repeated_empty_resolution_reused(self):
  class ResolverTransport(Transport):
   async def call(self,action,args):
    if action=='resolve':self.calls.append((action,args));return {'candidates':[]}
    return await super().call(action,args)
  resolve=call('resolve_entity',{'text':'unknown','label':'Object'})
  t=ResolverTransport([resolve,resolve,query(),select(),finish()])
  await Agent(self.pack,t).run('original scope')
  self.assertEqual(sum(a=='resolve' for a,_ in t.calls),1)
 async def test_pathway_only_positive(self):
  p=pattern();p['nodes']=p['nodes'][1:];p['edges']=p['edges'][1:]
  r,_=await self.run_steps([query(pat=p),select(),finish('complete')],True)
  self.assertEqual(r['status'],'complete');self.assertEqual([e['type'] for e in r['metapath']['edges']],['MEMBER'])
 def test_external_accession_multiple_ids_remain_explicit(self):
  ctx=[{'tool':'resolve_entity','input':{'text':'42'},'output':{'candidates':[{'labels':['Object'],'properties':{'id':x,'external':['42']}} for x in ['a','b']]}}]
  with self.assertRaisesRegex(Feedback,'multiple distinct'):validate_bindings(PACK,pattern(),ctx)
 def test_category_qualifier_does_not_require_other_endpoint(self):
  family={'members':[{'relation':'MEMBER','target':'Category','qualifiers':['specific-source']},{'relation':'ANNOTATED','target':'Term','qualifiers':['term-source']}],'qualifier_groups':[{'qualifiers':['catalog'],'members':[{'relation':'MEMBER','target':'Category'}]}]}
  pack={**PACK,'scope_families':[family]};qs={'q1':{'purpose':'requested','pattern':pattern()}};ss=[{'purpose':'requested','query_id':'q1'}]
  self.assertEqual(missing_scope_alternatives(pack,'catalog membership',qs,ss),[])
  self.assertTrue(missing_scope_alternatives(pack,'functional membership',qs,ss))
  self.assertTrue(missing_scope_alternatives(pack,'term-source catalog membership',qs,ss))
 def test_model_context_compaction_retains_recoverable_conversation(self):
  from pankagent_query.context import model_messages
  messages=[{'role':'assistant','content':[{'type':'tool_use','id':'call1','name':'query','input':{'pattern':pattern()}}]},{'role':'user','content':[{'type':'tool_result','tool_use_id':'call1','content':json.dumps({'query_id':'q1','status':'empty','row_count':0,'unused':'x'*150000})}]}]
  original=copy.deepcopy(messages);wire,stats=model_messages(messages,10000)
  self.assertEqual(messages,original);self.assertEqual(wire[0],messages[0]);self.assertEqual(wire[1]['content'][0]['tool_use_id'],'call1')
  compact=json.loads(wire[1]['content'][0]['content']);self.assertEqual(compact['status'],'empty');self.assertEqual(compact['recover_with']['name'],'inspect_result');self.assertLess(stats['sent_bytes'],10000)
 def test_unrelated_inventory_id_cannot_bind_qualifier(self):
  pack={**PACK,'identity_fields':{**PACK['identity_fields'],'Category':{'id':{'role':'primary'}}},'scope_families':[{'members':[{'relation':'MEMBER','target':'Category'}]}]}
  p=pattern();p['nodes'][2]['filters']=[{'property':'id','op':'eq','value':'c'}]
  inventory={'q0':{'pattern':{'nodes':[{'key':'x','label':'Category'}],'edges':[]},'graph':{'nodes':[{'labels':['Category'],'properties':{'id':'c','name':'Unrelated'}}]}}}
  with self.assertRaisesRegex(Feedback,'not resolved'):validate_bindings(pack,p,[],'original question',True,inventory)
  ctx=[{'tool':'resolve_entity','input':{'text':'desired concept'},'output':{'candidates':[{'labels':['Category'],'properties':{'id':'c','name':'Desired'}}]}}]
  validate_bindings(pack,p,ctx,'original question',True,inventory)
  validate_bindings(pack,p,[],'Find category c',True,inventory)
 def test_prompt_identity_includes_scope_review(self):
  from unittest.mock import patch
  from pankagent_query.core import context_identity
  before=context_identity(self.pack)
  with patch('pankagent_query.core.REVIEW_INSTRUCTION','changed review'):
   self.assertNotEqual(before,context_identity(self.pack))
 def test_resolved_user_concept_must_reach_requested_query(self):
  from pankagent_query.identity import unbound_user_concepts,selected_prefixes
  full=pattern();full['nodes'][2]['filters']=[{'property':'id','op':'eq','value':'category1'}]
  prefix=copy.deepcopy(full);prefix['nodes']=prefix['nodes'][:2];prefix['edges']=prefix['edges'][:1]
  qs={'q1':{'purpose':'requested','pattern':prefix},'q2':{'purpose':'requested','pattern':full}}
  ctx=[{'tool':'resolve_entity','input':{'text':'Beta'},'output':{'candidates':[{'labels':['Category'],'properties':{'id':'category1','name':'Beta'}}]}}]
  one=[{'purpose':'requested','query_id':'q1'}];both=one+[{'purpose':'requested','query_id':'q2'}]
  self.assertTrue(unbound_user_concepts('Find Beta',qs,one,ctx))
  self.assertEqual(unbound_user_concepts('Find Beta',qs,both,ctx),[])
  self.assertEqual(selected_prefixes(qs,both)[0]['query_id'],'q1')
  self.assertEqual(selected_prefixes(qs,both[1:]),[])
 async def test_inherited_unfulfilled_condition_keeps_coverage_incomplete(self):
  f=finish('partial');f[0]['input']['unfulfilled_conditions']=['unresolved concept']
  seed,_=await self.run_steps([query(),select(),f])
  r,_=await self.run_steps([finish('empty')],seed=seed)
  self.assertEqual(r['status'],'partial');self.assertEqual(r['retrieval_checks']['condition_coverage'],'incomplete')
 def test_migrated_rule_retention(self):
  data=json.loads((ROOT/'schema_semantics.json').read_text());self.assertGreater(len(data['interpretation']['rules']),20);self.assertTrue(data['provenance']['source_hashes'])
if __name__=='__main__':unittest.main()
