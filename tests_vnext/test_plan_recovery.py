import asyncio
import unittest
from pankagent_vnext.plan_recovery import recover_empty_plan
class Gateway:
    def __init__(self, output): self.output=output; self.calls=0
    async def plan(self,q,h):
        self.calls+=1
        if isinstance(self.output,Exception):raise self.output
        return self.output
class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_repairs_once(self):
        g=Gateway({'steps':[{'id':'s1'}]})
        p=await recover_empty_plan(g,{'steps':[]},'HPAP islet count?',[],1)
        self.assertEqual(len(p['steps']),1);self.assertEqual(g.calls,1)
    async def test_failure_not_user_blame(self):
        for output in ({'steps':[]},RuntimeError('secret should not leak')):
            g=Gateway(output);p=await recover_empty_plan(g,{'steps':[]},'HPAP?',[],1)
            self.assertEqual(g.calls,1);self.assertEqual(p['recovery']['category'],'planning_failure')
            self.assertNotIn('secret',str(p))
    async def test_real_clarification_and_success_do_not_retry(self):
        for plan in ({'steps':[],'clarification':'Which stage?'},{'steps':[{'id':'s1'}]}):
            g=Gateway({});self.assertEqual(await recover_empty_plan(g,plan,'q',[],1),plan);self.assertEqual(g.calls,0)
    async def test_cancel_propagates(self):
        class Cancel:
            async def plan(self,q,h):raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await recover_empty_plan(Cancel(),{'steps':[]},'q',[],1)
