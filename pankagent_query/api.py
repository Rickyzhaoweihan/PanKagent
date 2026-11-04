"""Independent loopback query-dev API. Does not select a production deployment."""
import asyncio
import copy
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
from . import core,native_client
from .transport import IntegratedTransport
from .workflow import Workflow,router

CROSSCHECK = '\nBefore finishing, cross-check the approved evidence against all entities and conditions requested in the question. Review whether retrieval coverage is sufficient and whether identity, scope or interpretation remains ambiguous. If the answer is already supported, use this phase to verify completeness rather than immediately finishing. Choose any further tool calls based on unresolved uncertainty; do not repeat successful work or require new evidence when the retained evidence suffices. Briefly state the verification outcome and any remaining uncertainty in finish.\n'
if CROSSCHECK not in core.ENRICHMENT:core.ENRICHMENT+=CROSSCHECK


def create_app(path):
    config=json.loads(Path(path).read_text())
    private=Path(config['state_root']);private.mkdir(parents=True,exist_ok=True,mode=0o700)
    pack=core.SchemaPack(config['schema_path'])
    assert pack.data['release']==config['release_id']
    api_token=Path(config['api_token_file']).read_text().strip()
    query_token=Path(config['query_token_file']).read_text().strip()
    model_token=Path(config['model_token_file']).read_text().strip()
    assert all(len(t)>=16 for t in [api_token,query_token,model_token])
    native_client.BASE=config['model_url'].rstrip('/');native_client.MODEL=config['model']
    client=native_client.Client(private/'model-calls',model_token)
    transport=IntegratedTransport(config,query_token,client)
    gate=asyncio.Semaphore(1)

    async def runner(question,history=None,on_progress=None,**kwargs):
        async with gate:
            await transport.verify_release(pack)
            return await core.Agent(pack,transport,model=config['model']).run(question,history,on_progress,**kwargs)

    async def query_finish(question,result,on_delta=None):
        # This is the query agent's own brief finish, not the existing site's GPT
        # formatter. Keeping the retrieval service independent avoids changing it.
        return {'answer':result.get('answer',''),'model':config['model']+' query finish',
                'usage':{},'elapsed_s':0,'stop_reason':'end_turn'}

    workflow=Workflow(runner,private/'workflow',query_finish,literature=None,
                      presentation=None,graph_release=config['release_id'])

    async def probe():
        live=await transport.verify_release(pack)
        async with httpx.AsyncClient(timeout=10,trust_env=False) as http:
            r=await http.get(config['model_url'].rstrip('/')+'/models',headers={'Authorization':'Bearer '+model_token})
            r.raise_for_status();assert config['model'] in [x['id'] for x in r.json()['data']]
        return {'ready':True,'release_id':config['release_id'],'schema_pack_sha256':pack.sha256,
            'input_manifest_sha256':live['input_manifest_sha256'],'counts':live['counts'],
            'model':config['model'],'authenticated_model':True,'query_url':config['query_url'],
            'scope':'Independent Qwen query dev; old GPT formatter and UI unchanged',
            'two_loops':True,'tool_count':len(core.TOOLS),'production_changed':False}

    @asynccontextmanager
    async def lifespan(app):
        await probe();await transport.initialize()
        try:yield
        finally:
            tasks=list(workflow.tasks.values())+list(workflow.clocks.values())+list(workflow.presentations.values())
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

    app=FastAPI(title='Schema-bound query dev',lifespan=lifespan,docs_url=None,redoc_url=None)
    @app.middleware('http')
    async def auth(request:Request,next):
        if request.url.path not in ['/health','/health/live','/health/ready']:
            if not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+api_token):
                return JSONResponse({'detail':'Authentication required'},status_code=401)
            if len(await request.body())>65536:return JSONResponse({'detail':'Request too large'},status_code=413)
        result=await next(request);result.headers['Cache-Control']='no-store';return result
    app.include_router(router(workflow))
    @app.get('/health/live')
    def live():return {'status':'live','release_id':config['release_id']}
    @app.get('/health')
    @app.get('/health/ready')
    async def ready():
        try:return await probe()
        except Exception as e:return JSONResponse({'ready':False,'state':'temporarily_unavailable','category':type(e).__name__},status_code=503)
    @app.get('/agent/v2/schema')
    def schema():return pack.data
    @app.get('/agent/v2/runs/{identity}/evidence')
    def evidence(identity:str):
        run=workflow.get(identity);result=run.get('_result',{})
        return {'release_id':config['release_id'],'run_id':run['run_id'],
                'linked_evidence':copy.deepcopy(result.get('linked_evidence',[])),
                'evidence_reads':copy.deepcopy(result.get('evidence_reads',[])),
                'selected_queries':copy.deepcopy(result.get('selected_queries',[])),
                'graph':copy.deepcopy(result.get('graph',{})),
                'status':run['status']}
    app.state.workflow=workflow;app.state.pack=pack;app.state.transport=transport
    return app


app=create_app(os.environ['PANK_QUERY_AGENT_CONFIG']) if os.environ.get('PANK_QUERY_AGENT_CONFIG') else FastAPI(docs_url=None,redoc_url=None)
