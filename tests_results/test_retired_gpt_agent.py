import asyncio
import httpx
from fastapi import FastAPI
from pankgraph_results.gpt6_proxy import install_gpt6_proxy


def test_retired_namespace_never_contacts_upstream():
    async def run():
        app=FastAPI();install_gpt6_proxy(app, None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            for method,path in [('GET','/api/gpt6/'),('POST','/api/gpt6/v2/plans'),('GET','/api/gpt6/demo')]:
                response=await client.request(method,path)
                assert response.status_code==410
                assert response.json()['agent_url']=='/agent-vnext'
    asyncio.run(run())
