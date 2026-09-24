import asyncio
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from pankgraph_results.auth import DemoAuthentication
from pankgraph_results.app import PrefixMiddleware
from pankgraph_results.config import ResultsSettings


def test_localhost_cors():
    async def check():
        async def endpoint(request):
            if request.url.path.endswith('/events'):
                return StreamingResponse(iter(['data: test\n\n']), media_type='text/event-stream')
            return JSONResponse({'ok': True})
        app = Starlette(routes=[Route('/api/test', endpoint, methods=['GET', 'POST']), Route('/api/events', endpoint)])
        for enabled in [False, True]:
            settings = ResultsSettings(basic_auth=False, allow_localhost_cors=enabled)
            wrapped = PrefixMiddleware(DemoAuthentication(app, settings), prefix=settings.public_path)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url='https://dev.pankgraph.org') as client:
                for origin in ['http://localhost:3000', 'http://localhost:5173', 'http://127.0.0.1:8080', 'https://localhost', 'http://[::1]:3000']:
                    headers = {'Origin': origin, 'Sec-Fetch-Site': 'cross-site'}
                    preflight = await client.options('/pankgraph-vnext/api/test', headers={**headers, 'Access-Control-Request-Method':'POST', 'Access-Control-Request-Headers':'content-type,last-event-id'})
                    response = await client.post('/pankgraph-vnext/api/test', headers=headers, json={})
                    assert response.status_code == (200 if enabled else 403)
                    if enabled:
                        assert preflight.status_code == 200
                        for r in [preflight, response, await client.get('/pankgraph-vnext/api/events', headers=headers), await client.get('/pankgraph-vnext/api/missing', headers=headers)]:
                            assert r.headers['access-control-allow-origin'] == origin
                            assert r.headers['access-control-allow-credentials'] == 'true'
                            assert 'Origin' in r.headers['vary']
                    else:
                        assert 'access-control-allow-origin' not in response.headers
                for origin in ['null', 'https://evil.example', 'http://localhost.evil.example:3000', 'http://localhost@evil.example', 'http://127.0.0.2:3000']:
                    headers={'Origin':origin,'Sec-Fetch-Site':'cross-site'}
                    r=await client.post('/pankgraph-vnext/api/test',headers=headers,json={})
                    assert r.status_code == 403
                    assert 'access-control-allow-origin' not in r.headers
        assert not ResultsSettings().allow_localhost_cors
    asyncio.run(check())
