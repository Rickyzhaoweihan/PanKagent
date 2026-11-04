"""Verify the actual deployment-source router with the site's auth middleware."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI

ROOT=Path(__file__).resolve().parents[1]/'deploy_native_agent/source/gateway_results/pankgraph_results'
def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
proxy=load('viewer_proxy');auth=load('auth')
REF={'kind':'node','type_id':'Gene','id':'A'}

def test_authenticated_exact_release_bridge(tmp_path):
    async def check():
        token=tmp_path/'token';token.write_text('test-only-server-token')
        calls=[]
        def handler(request):
            calls.append(request)
            assert request.headers['authorization']=='Bearer test-only-server-token'
            assert 'cookie' not in request.headers
            if request.method=='GET':return httpx.Response(200,json={'release_id':'r1','profiles':{},'tables':{}})
            return httpx.Response(200,json={'ref':REF,'fields':[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            app=FastAPI();proxy.install_viewer_proxy(app,upstream,url='http://127.0.0.1:8826',release='r1',token_file=str(token))
            settings=SimpleNamespace(testing=False,basic_auth=True,basic_user='demo',password_hash=auth.hash_password('test-password'),trusted_browser_origin='',allow_localhost_cors=False)
            app.add_middleware(auth.DemoAuthentication,settings=settings)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://site') as browser:
                assert (await browser.get('/api/viewer/v1/schema?release_id=r1')).status_code==401
                assert not calls
                browser.auth=('demo','test-password')
                assert (await browser.get('/api/viewer/v1/schema?release_id=old')).status_code==409
                assert not calls
                assert (await browser.get('/api/viewer/v1/schema?release_id=r1')).status_code==200
                result=await browser.post('/api/viewer/v1/inspect',json={'release_id':'r1','ref':REF},headers={'cookie':'browser-private'})
                assert result.status_code==200 and result.json()['release_id']=='r1'
                assert result.headers['cache-control']=='no-store'
                count=len(calls)
                for body in ({'release_id':'r1','ref':REF,'cypher':'MATCH (n) RETURN n'},{'release_id':'r1','ref':{'id':'A'}}):
                    assert (await browser.post('/api/viewer/v1/inspect',json=body)).status_code==422
                assert (await browser.post('/api/viewer/v1/raw',json={})).status_code==404
                assert (await browser.post('/api/viewer/v1/table',json={'release_id':'r1','ref':REF},headers={'origin':'https://other.example','sec-fetch-site':'cross-site'})).status_code==403
                assert len(calls)==count
    asyncio.run(check())

@pytest.mark.parametrize('payload',[{'release_id':'wrong','ref':REF},{'ref':{**REF,'id':'other'}},['not','an','object']])
def test_invalid_upstream_identity_rejected(tmp_path,payload):
    async def check():
        token=tmp_path/'token';token.write_text('test-only')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=payload))) as upstream:
            app=FastAPI();proxy.install_viewer_proxy(app,upstream,url='http://127.0.0.1:8826',release='r1',token_file=str(token))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://site') as browser:
                result=await browser.post('/api/viewer/v1/inspect',json={'release_id':'r1','ref':REF})
                assert result.status_code==502
    asyncio.run(check())

def test_no_arbitrary_upstream():
    with pytest.raises(ValueError):proxy.install_viewer_proxy(FastAPI(),None,url='https://untrusted.example',release='r1',token_file='unused')
