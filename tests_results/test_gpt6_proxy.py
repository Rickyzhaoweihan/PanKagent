import asyncio
import httpx
from fastapi import FastAPI
from pankgraph_results.gpt6_proxy import install_gpt6_proxy


def test_explicit_gpt_namespace_isolated_and_stream_preserved():
    async def run():
        seen=[]
        async def provider(request):
            seen.append(request)
            return httpx.Response(200, content=b'data: {"text":"Public donor HPAP-041"}\n\n', headers={'content-type':'text/event-stream'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider), headers={'authorization':'secret'}) as upstream:
            app=FastAPI();install_gpt6_proxy(app,upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://dev.pankgraph.org') as client:
                p='/api/gpt6/v2/runs/12345678-1234-1234-1234-123456789abc/events'
                response=await client.get(p+'?after=4',headers={'cookie':'not-forwarded','x-api-key':'not-forwarded'})
                assert response.status_code==200 and 'HPAP-041' in response.text
                assert response.headers['x-pankagent-model']=='gpt-6-sol'
                assert str(seen[0].url)=='http://127.0.0.1:8798'+p.removeprefix('/api/gpt6')+'?after=4'
                assert not any(k in seen[0].headers for k in ['authorization','cookie','x-api-key'])
                for path in ['health/components','metrics','v2/runs/12345678-1234-1234-1234-123456789abc/interactions','../health']:
                    assert (await client.get('/api/gpt6/'+path)).status_code==404
                assert (await client.get(p+'?upstream=http://evil')).status_code==422
                assert (await client.post('/api/gpt6/v2/plans',content=b'x'*32001)).status_code==413
                assert len(seen)==1
                info=await client.get('/api/gpt6/')
                assert info.status_code==200 and info.json()['model']=='gpt-6-sol'
    asyncio.run(run())


def test_created_run_urls_stay_inside_gpt_namespace():
    async def run():
        async def provider(request):
            return httpx.Response(202, json={'events_url':'/v2/runs/id/events','plan_url':'/v2/plans/id','run_id':'id'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
            app=FastAPI();install_gpt6_proxy(app,upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://dev.pankgraph.org') as client:
                response=await client.post('/api/gpt6/v2/plans',json={'question':'T1D'})
                assert response.status_code==202
                assert response.json()=={'events_url':'/gpt6/v2/runs/id/events','plan_url':'/gpt6/v2/plans/id','run_id':'id'}
    asyncio.run(run())
