"""Technical contracts allow model decisions without imposing semantic policy."""
import asyncio
import copy
import json
import unittest
import test_contract
from test_contract import Transport, pattern, query, select, finish, call
from pankagent_query.core import Agent, ResourceLimit, TOOLS, validate_arguments, Feedback
from pankagent_query.formatting import prepare

class ModelOwned(unittest.IsolatedAsyncioTestCase):
    setUp = test_contract.Contract.setUp
    tearDown = test_contract.Contract.tearDown
    run_steps = test_contract.Contract.run_steps
    async def test_one_finish_no_forced_review(self):
        r, t = await self.run_steps([query(), select(), finish()])
        self.assertEqual(r['metrics']['model_calls'], 3)
        self.assertEqual(t.final_retries, 0)
        self.assertFalse(any(e['output'].get('status') == 'review_required' for e in r['events']))

    async def test_model_may_relax_query_and_keep_independent_branch(self):
        full = pattern()
        full['nodes'][2]['filters'] = [{'property': 'name', 'op': 'contains', 'value': 'Case'}]
        direct = pattern()
        direct['nodes'] = direct['nodes'][1:]
        direct['edges'] = direct['edges'][1:]
        r, _ = await self.run_steps([query(pat=full), query(), query(pat=direct), select(('q1','q2','q3')), finish()])
        self.assertEqual(r['error_records'], [])
        self.assertEqual(len(r['selected_queries']), 3)

    async def test_selection_reassessment_keeps_rationale_and_original_execution(self):
        s = select()
        s[0]['input']['selections'][0]['rationale'] = 'Observed record supports the requested connection.'
        r, _ = await self.run_steps([query('diagnostic'), s, finish()])
        self.assertIn('supports', r['selected_queries'][0]['rationale'])
        self.assertEqual(r['_query_results']['q1']['purpose'], 'diagnostic')
        body = json.loads(prepare('Original question', r, 'fixture').body)
        self.assertEqual(body['model_assessment']['status'], 'empty')

    def test_readers_expose_only_legal_arguments(self):
        definitions = {t['name']: t['input_schema'] for t in TOOLS}
        self.assertNotIn('fields', definitions['inspect_result']['properties'])
        self.assertNotIn('kind', definitions['inspect_evidence']['properties'])
        with self.assertRaisesRegex(Feedback, 'unknown fields.*fields'):
            validate_arguments({'query_id':'q1','fields':['id']}, definitions['inspect_result'], 'inspect_result')
        validate_arguments({'query_id':'q1','kind':'nodes','limit':30}, definitions['inspect_result'], 'inspect_result')

    async def test_resource_limit_preserves_checkpoint_without_semantic_failure(self):
        checkpoints = []
        t = Transport([query(), select()])
        with self.assertRaises(ResourceLimit):
            await Agent(self.pack,t,max_turns=2).run('question',on_checkpoint=checkpoints.append)
        self.assertIn('q1', checkpoints[-1]['_query_results'])
        self.assertEqual(checkpoints[-1]['selected_queries'][0]['query_id'], 'q1')
        self.assertNotEqual(checkpoints[-1].get('status'), 'failed')

    async def test_wall_time_budget_is_operational(self):
        class Slow(Transport):
            async def call(self, action, args):
                await asyncio.sleep(1)
        with self.assertRaises(ResourceLimit):
            await Agent(self.pack,Slow([])).run('question',deadline_s=.01)

    async def test_retrieval_partial_does_not_overwrite_model_assessment(self):
        class Partial(Transport):
            async def call(self, action, args):
                value = await super().call(action,args)
                if action == 'query':
                    value.update(status='partial',truncated=True)
                return value
        r = await Agent(self.pack,Partial([query(),select(),finish('complete')],True)).run('question')
        self.assertEqual(r['status'], 'complete')
        self.assertEqual(r['retrieval_status'], 'partial')
        self.assertFalse(r['retrieval_checks']['pagination_complete'])

    async def test_replace_selection_drops_old_graph_even_in_enrichment(self):
        class Records(Transport):
            async def call(self, action, args):
                value = await super().call(action,args)
                if action == 'query':
                    ident = 'b' if 'LINK_B' in args['cypher'] else 'a'
                    value['graph']['nodes']=[{'id':ident,'labels':['Object']}]
                return value
        first = await Agent(self.pack,Records([query(),select(),finish('complete')],True)).run('question')
        replace = select(('q2',));replace[0]['input']['replace']=True
        second = await Agent(self.pack,Records([query(pat=pattern('LINK_B')),replace,finish('complete')],True)).run('question',seed=first,phase='enrich')
        self.assertEqual([n['id'] for n in second['graph']['nodes']],['b'])

    def test_distinct_external_accessions_execute_literally(self):
        from pankagent_query.core import compile_pattern
        p=pattern();p['distinct_nodes']=True
        for n in p['nodes'][:2]:n['filters']=[{'property':'external','op':'eq','value':'42'}]
        cypher,_=compile_pattern(self.pack,p)
        self.assertIn('n0 <> n1',cypher)

    async def test_cache_reuses_execution_across_working_purposes(self):
        r,t=await self.run_steps([query('diagnostic'),query('requested'),select(),finish()])
        self.assertEqual(sum(a=='query' for a,_ in t.calls),1)
        self.assertEqual(r['error_records'],[])

    async def test_context_only_selection_is_not_semantically_rejected(self):
        r,_=await self.run_steps([query('context'),select(purpose='context'),finish('partial')])
        self.assertEqual(r['error_records'],[])
        self.assertEqual(r['selected_queries'][0]['purpose'],'context')

    async def test_registry_recovery_lists_exact_retained_queries(self):
        r,_=await self.run_steps([query('diagnostic'),call('list_results',{}),select(),finish()])
        event=next(e for e in r['events'] if e['tool']=='list_results')
        self.assertEqual(event['output']['queries'][0]['pattern'],pattern())
        self.assertEqual(event['output']['queries'][0]['purpose'],'diagnostic')

    def test_large_history_window_preserves_tool_pairs_and_original_question(self):
        from pankagent_query.context import model_messages
        messages=[{'role':'user','content':'Original question'}]
        for i in range(20):
            messages.extend([{'role':'assistant','content':[{'type':'tool_use','id':str(i),'name':'query','input':{'literal':'x'*1000}}]},
                {'role':'user','content':[{'type':'tool_result','tool_use_id':str(i),'content':'{}'}]}])
        retained=copy.deepcopy(messages)
        wire,stats=model_messages(messages,5000)
        self.assertEqual(messages,retained)
        self.assertEqual(wire[0],messages[0])
        self.assertIn('list_results',wire[1]['content'])
        calls={b['id'] for m in wire if isinstance(m['content'],list) for b in m['content'] if b['type']=='tool_use'}
        for m in wire:
            if isinstance(m['content'],list):
                for b in m['content']:
                    if b['type']=='tool_result':self.assertIn(b['tool_use_id'],calls)
        self.assertLessEqual(stats['sent_bytes'],5000)
