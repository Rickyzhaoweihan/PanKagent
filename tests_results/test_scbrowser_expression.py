import asyncio, base64, gzip, json, struct, tempfile, unittest
from pankgraph_results.scbrowser_expression import compact, ExpressionCache, DATASET, ATLAS
class CacheTests(unittest.IsolatedAsyncioTestCase):
    def test_exact(self):
        row={'data':[{'dataset':DATASET,'gene':'CFTR','expression':[0,1.123456789,0,2]}]}
        p=compact(row,'CFTR',4)
        self.assertEqual(struct.unpack('<2I',base64.b64decode(p['indices'])),(1,3))
        self.assertEqual(struct.unpack('<2d',base64.b64decode(p['values'])),(1.123456789,2))
        for bad in [dict(row,restricted=True),dict(row,continuation='next'),{'data':[{'dataset':DATASET,'gene':'CFTR','expression':[0,-1]}]}]:
            with self.assertRaises(ValueError):compact(bad,'CFTR',4)
    async def test_singleflight_and_persistence(self):
        calls=[]
        async def fetch(gene):
            calls.append(gene);await asyncio.sleep(.03)
            return {'data':[{'dataset':DATASET,'gene':gene,'expression':[0]*448935}]}
        with tempfile.TemporaryDirectory() as root:
            cache=ExpressionCache(root,fetch)
            a,b=await asyncio.gather(cache.get('CFTR'),cache.get('CFTR'))
            self.assertEqual(len(calls),1)
            self.assertEqual(a[0],b[0])
            self.assertEqual((await ExpressionCache(root,fetch).get('CFTR'))[1],'hit')
            self.assertEqual(json.loads(gzip.decompress(a[0]))['count'],448935)
    async def test_failure_never_cached(self):
        async def fetch(gene):return {'restricted':True}
        with tempfile.TemporaryDirectory() as root:
            c=ExpressionCache(root,fetch)
            with self.assertRaises(Exception):await c.get('CFTR')
            self.assertIsNone(c._read('CFTR'))


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_unregistered_identity_and_gene(self):
        import httpx
        from fastapi import FastAPI
        from pankgraph_results.scbrowser_expression import install
        with tempfile.TemporaryDirectory() as root:
            app=FastAPI();install(app,root)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                response=await client.get('/api/scbrowser/expression',params={'dataset':'other','atlas':ATLAS,'gene':'CFTR'})
                self.assertEqual(response.status_code,409)
                response=await client.get('/api/scbrowser/expression',params={'dataset':DATASET,'atlas':ATLAS,'gene':'../CFTR'})
                self.assertEqual(response.status_code,422)
