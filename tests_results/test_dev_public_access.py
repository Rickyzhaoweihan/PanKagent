import asyncio, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from pankgraph_results.auth import DemoAuthentication
from pankgraph_results.config import ResultsSettings
from pankgraph_health.app import create_app
from pankgraph_health.settings import Settings

async def main():
    async def endpoint(request): return JSONResponse({'ok':True})
    app=Starlette(routes=[Route('/api/access',endpoint,methods=['GET','POST']),Route('/api/results',endpoint,methods=['GET','POST'])])
    for enabled in [True,False]:
        cfg=ResultsSettings(basic_auth=enabled,password_hash='',trusted_browser_origin='https://dev.pankgraph.org')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=DemoAuthentication(app,cfg)),base_url='http://upstream') as client:
            for path in ['/api/access','/api/results']:
                r=await client.get(path); assert r.status_code==(503 if enabled else 200),r.text
                if not enabled:assert 'www-authenticate' not in r.headers
            if not enabled:
                assert (await client.get('/api/results',headers={'Authorization':'Basic stale'})).status_code==200
                assert (await client.post('/api/results',headers={'Origin':'https://dev.pankgraph.org'})).status_code==200
                assert (await client.post('/api/results',headers={'Origin':'https://evil.example'})).status_code==403
                assert (await client.post('/api/results',headers={'Sec-Fetch-Site':'cross-site'})).status_code==403
    for enabled in [True,False]:
        cfg=Settings(Path(tempfile.mkdtemp()),'operator','',basic_auth=enabled)
        collector=SimpleNamespace(history=None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(cfg,collector)),base_url='http://upstream') as client:
            r=await client.get('/pankgraph/health/');assert r.status_code==(401 if enabled else 200),r.text
            if not enabled:
                assert 'www-authenticate' not in r.headers
                assert r.headers['cache-control']=='no-store'
            assert (await client.post('/pankgraph/health/')).status_code==405
    with patch.dict('os.environ',{'PANK_RESULTS_BASIC_AUTH':'false'}):assert not ResultsSettings().basic_auth
    with patch.dict('os.environ',{'PANK_HEALTH_STATE_DIR':tempfile.mkdtemp()}), patch('pankgraph_health.settings.protected_values',return_value={'PANK_RESULTS_HEALTH_BASIC_AUTH':'false'}):
        assert not Settings.load().basic_auth
    print('PASS: public access, stale credentials, same-origin POST, cross-site rejection, default authentication, dashboard read-only and configuration loading')
def test_dev_public_access():
    asyncio.run(main())
