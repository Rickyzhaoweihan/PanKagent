"""The public dev probe is a fixed, credential-free observation, never a canary."""
import asyncio
import hashlib
import time

import httpx
import pytest

from pankgraph_health.collector import Collector, observation
from pankgraph_health.settings import Settings
from pankgraph_health.store import History


HTML = '<div id="root"></div><script src="/static/js/main.abcdef12.js"></script>'
BUNDLE = b'console.log("dev fixture")'


def test_dev_delivery_separate_credentials_hashes_refresh_and_staleness(tmp_path):
    async def run():
        calls = []
        cfg = Settings(tmp_path, 'operator', 'hash', agent_token='private-agent',
                       frontend_headers={'Authorization': 'Basic private-demo'})
        def handle(request):
            calls.append(request)
            assert request.method == 'GET' and request.url.host == 'dev.pankgraph.org'
            assert request.url.scheme == 'https'
            assert not {'authorization', 'cookie', 'x-api-key', 'x-operator-token'} & set(request.headers)
            if request.url.path == '/':
                return httpx.Response(200, text=HTML, headers={'Content-Type': 'text/html', 'Set-Cookie': 'dev=ignored; Path=/'})
            assert request.url.path == '/static/js/main.abcdef12.js'
            return httpx.Response(200, content=BUNDLE, headers={'Content-Type': 'application/javascript'})
        c = Collector(cfg, History(tmp_path), httpx.MockTransport(handle))
        c.http.headers['Authorization'] = 'Bearer private-agent'
        c.http.cookies.set('private-session', 'never-forward', domain='dev.pankgraph.org')
        try:
            first = await c.dev_frontend()
            assert first['id'] == 'dev.frontend.delivery' and first['state'] == 'healthy'
            assert first['details']['html_sha256'] == hashlib.sha256(HTML.encode()).hexdigest()
            assert first['details']['bundle_sha256'] == hashlib.sha256(BUNDLE).hexdigest()
            assert len(calls) == 2
            assert (await c.dev_frontend())['state'] == 'healthy' and len(calls) == 3
            c.dev_asset_checked = time.time()-301
            assert (await c.dev_frontend())['state'] == 'healthy' and len(calls) == 5
            c.current = {'components': [first]}; c.last_cycle = time.time()-100
            stale = c.snapshot()['components'][0]
            assert stale['state'] == 'unknown' and stale['error_category'] == 'collector_stale'
            assert c.asset is None and c.asset_checked == 0
        finally:
            await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('source', [
    'https://evil.example/static/js/main.bad.js', '//evil.example/static/js/main.bad.js',
    'http://dev.pankgraph.org/static/js/main.bad.js',
    'https://user:secret@dev.pankgraph.org/static/js/main.bad.js',
    '/static/js/main.bad.js?token=secret', '/static/js/main.bad.js#fragment',
    '/api/agent/v2/plans', '/static/js/main.bad/other.js',
])
def test_invalid_asset_never_becomes_a_request(tmp_path, source):
    async def run():
        calls=[]
        def handle(request):
            calls.append(request)
            return httpx.Response(200, text=f'<div id="root"></div><script src="{source}"></script>', headers={'Content-Type':'text/html'})
        c=Collector(Settings(tmp_path,'operator','hash'),History(tmp_path),httpx.MockTransport(handle))
        try:
            result = await c.dev_frontend()
            assert result['state'] == 'unavailable'
            assert len(calls) == 1 and calls[0].url.path == '/'
            assert 'secret' not in str(result)
        finally:
            await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['redirect', 'denied', 'timeout', 'html_mime', 'bundle_mime', 'empty_bundle', 'oversized'])
def test_dev_bad_delivery_stays_unavailable(tmp_path, kind):
    async def run():
        calls=[]
        def handle(request):
            calls.append(request)
            if kind == 'timeout': raise httpx.ReadTimeout('private error')
            if kind == 'redirect': return httpx.Response(302,headers={'Location':'https://evil.example'})
            if kind == 'denied': return httpx.Response(403)
            if kind == 'oversized': return httpx.Response(200,content=b'x'*(1024*1024+1),headers={'Content-Type':'text/html'})
            if request.url.path == '/': return httpx.Response(200,text=HTML,headers={'Content-Type':'application/json' if kind=='html_mime' else 'text/html'})
            return httpx.Response(200,content=b'' if kind=='empty_bundle' else BUNDLE,
                headers={'Content-Type':'text/html' if kind=='bundle_mime' else 'text/javascript'})
        c=Collector(Settings(tmp_path,'operator','hash'),History(tmp_path),httpx.MockTransport(handle))
        try:
            result=await c.dev_frontend()
            assert result['state']=='unavailable' and result['error_category']
            assert c.dev_asset_checked == 0
            assert all(r.method=='GET' and r.url.host=='dev.pankgraph.org' for r in calls)
        finally: await c.close()
    asyncio.run(run())


def test_collection_keeps_dev_separate_without_inference(tmp_path):
    async def run():
        c=Collector(Settings(tmp_path,'operator','hash'),History(tmp_path))
        async def service(*_): return [], {}, {}, {}
        async def frontend(): return observation('frontend.delivery','Isolated demo','healthy')
        async def dev(): return observation('dev.frontend.delivery','Dev','unavailable',error_category='timeout')
        async def simple(key,label,*_): return observation(key,label,'healthy')
        c.service, c.frontend, c.dev_frontend, c.simple = service, frontend, dev, simple
        try:
            await c.collect()
            rows={row['id']:row for row in c.snapshot()['components']}
            assert rows['frontend.delivery']['state']=='healthy'
            assert rows['dev.frontend.delivery']['state']=='unavailable'
            assert c.snapshot()['monitor']['state']=='healthy'
            assert c.history.history()['samples']==1
        finally: await c.close()
    asyncio.run(run())
