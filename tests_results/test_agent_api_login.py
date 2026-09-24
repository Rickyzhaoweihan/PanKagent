"""The dev agent exemption must not disable other auth or origin checks."""
import unittest
from types import SimpleNamespace
from starlette.responses import JSONResponse
from pankgraph_results.auth import DemoAuthentication

class AgentApiLoginTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, path, enabled=False, method='GET', headers=()):
        async def app(scope, receive, send):
            await JSONResponse({'ok': True})(scope, receive, send)
        middleware = DemoAuthentication(app, SimpleNamespace(testing=False,
            password_hash='configured', basic_user='test', agent_api_basic_auth=enabled,
            trusted_browser_origin='https://dev.pankgraph.org'))
        messages = []
        async def send(message): messages.append(message)
        async def receive(): return {'type': 'http.request', 'body': b''}
        await middleware({'type':'http', 'path':path, 'method':method,
            'client':('127.0.0.1',123), 'headers':[(b'host',b'dev.pankgraph.org'),
            (b'x-forwarded-for',b'203.0.113.1'),*headers]}, receive, send)
        return messages[0]

    async def test_agent_read_and_post_without_password(self):
        for method in ('GET','POST'):
            result = await self.request('/api/agent/v2/plans',method=method)
            self.assertEqual(result['status'],200)
            self.assertNotIn(b'www-authenticate',dict(result['headers']))

    async def test_other_routes_remain_protected(self):
        for path in ('/api/access','/api/results','/api/agent-other','/metrics','/health/components'):
            self.assertEqual((await self.request(path))['status'],401)

    async def test_default_policy_still_requires_password(self):
        self.assertEqual((await self.request('/api/agent/v2/plans',enabled=True))['status'],401)

    async def test_cross_site_post_still_denied(self):
        self.assertEqual((await self.request('/api/agent/v2/plans',method='POST',
            headers=[(b'origin',b'https://elsewhere.example')]))['status'],403)

if __name__ == '__main__': unittest.main()
