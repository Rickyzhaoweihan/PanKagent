"""Agent-only switch behind the retained dev authentication/results gateway."""
import asyncio
import json
import os
from pathlib import Path
from contextlib import asynccontextmanager
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from simple_agent.comparison import simple_route

async def body_and_replay(receive):
    chunks=[]
    while True:
        item=await receive()
        if item['type']!='http.request':break
        chunks.append(item.get('body',b''))
        if sum(map(len,chunks))>32000:raise ValueError('input_limit')
        if not item.get('more_body'):break
    body=b''.join(chunks);used=False
    async def replay():
        nonlocal used
        if not used:used=True;return {'type':'http.request','body':body,'more_body':False}
        return await receive()
    return body,replay


def qwen_route(path, method, body):
    if path == '/agent/v2/plans' and method == 'POST': return True
    if any(part.startswith(('simple-qwen-', 'plan-qwen-')) for part in path.split('/')): return True
    if path.startswith('/agent/v2/requests/'):
        identity = path.split('/')[4]
        if not identity or '/' in identity or '..' in identity: return False
        old = Path('/var/local/serviceuser/.local/state/pankagent-simple/requests') / (identity + '.json')
        return not old.exists()
    if path == '/results' and method == 'POST':
        try: return str(json.loads(body).get('run_id', '')).startswith('simple-qwen-')
        except (ValueError, AttributeError): return False
    return False

async def forward_qwen(scope, receive, send, internal, body):
    query = scope.get('query_string', b'').decode()
    target = 'http://127.0.0.1:18864' + internal + ('?' + query if query else '')
    headers = {k.decode(): v.decode() for k,v in scope['headers'] if k.lower() in (b'accept', b'content-type', b'last-event-id')}
    client = httpx.AsyncClient(timeout=None, trust_env=False)
    try:
        upstream = await client.send(client.build_request(scope['method'], target, content=body, headers=headers), stream=True)
    except Exception:
        await client.aclose()
        return await JSONResponse({'detail':'Qwen agent temporarily unavailable'},502)(scope,receive,send)
    async def chunks():
        try:
            async for chunk in upstream.aiter_bytes(): yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
    return await StreamingResponse(chunks(),status_code=upstream.status_code,headers={'Content-Type':upstream.headers.get('content-type','application/json'),'Cache-Control':'no-store','X-Accel-Buffering':'no'})(scope,receive,send)

class AgentDispatch:
    def __init__(self,app,simple):self.app,self.simple=app,simple
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        path=scope['path'];candidate=path.startswith('/v2/') or path.startswith('/simple-api/')
        if not candidate:return await self.app(scope,receive,send)
        try:body,replay=await body_and_replay(receive)
        except ValueError:return await JSONResponse({'detail':'Input exceeds limit'},413)(scope,receive,send)
        internal=('/agent'+path) if path.startswith('/v2/') else path[len('/simple-api'):]
        if qwen_route(internal,scope['method'],body):
            return await forward_qwen(scope,replay,send,internal,body)
        if path.startswith('/simple-api/') or simple_route(internal,scope['method'],body):
            mapped={**scope,'path':internal,'raw_path':internal.encode()}
            return await self.simple(mapped,replay,send)
        if path.startswith('/v2/') and scope['method'] not in ('GET','HEAD'):
            return await JSONResponse({'detail':'The previous dev agent is retired. Start a new question with the current agent; saved history remains readable.'},409)(scope,replay,send)
        return await self.app(scope,replay,send)

def wrap_agent(app):
    os.environ.update(SIMPLE_AGENT_NATIVE='1',SIMPLE_AGENT_ALLOW_REMOTE_MODEL='1',SIMPLE_AGENT_SHARED_BUDGET='1',
        SIMPLE_AGENT_MODEL='claude-haiku-5-5',SIMPLE_FORMATTER_MODEL='claude-sonnet-5-5')
    os.environ.setdefault('SIMPLE_AGENT_STATE_DIR','/var/local/serviceuser/.local/state/pankagent-simple')
    from simple_agent.server import app as simple,transport,workflow
    previous=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(parent):
        async with previous(parent):
            await transport.initialize()
            try:yield
            finally:
                tasks=list(workflow.tasks.values())+list(workflow.clocks.values())+list(workflow.presentations.values())
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
    app.router.lifespan_context=lifespan
    app.add_middleware(AgentDispatch,simple=simple)
    app.state.simple_workflow=workflow
    return app

class ResultsDispatch:
    """Installed inside existing auth/Origin and prefix middleware."""
    def __init__(self,app,agent_url):self.app,self.agent_url=app,agent_url.rstrip('/')
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or not scope['path'].startswith('/api/'):
            return await self.app(scope,receive,send)
        path=scope['path'][4:]
        if not path.startswith(('/agent/v2/','/results')):return await self.app(scope,receive,send)
        try:body,replay=await body_and_replay(receive)
        except ValueError:return await JSONResponse({'detail':'Input exceeds limit'},413)(scope,receive,send)
        if not simple_route(path,scope['method'],body):return await self.app(scope,replay,send)
        target=self.agent_url+(path[len('/agent'):] if path.startswith('/agent/') else '/simple-api'+path)
        query=scope.get('query_string',b'').decode()
        if query:target+='?'+query
        request=Request(scope,replay)
        headers={k:v for k,v in request.headers.items() if k.lower() in ('accept','content-type','last-event-id')}
        client=httpx.AsyncClient(timeout=httpx.Timeout(400,connect=5),trust_env=False)
        try:upstream=await client.send(client.build_request(scope['method'],target,headers=headers,content=body),stream=True)
        except Exception:
            await client.aclose()
            return await JSONResponse({'detail':'Agent temporarily unavailable'},502)(scope,replay,send)
        async def chunks():
            try:
                async for chunk in upstream.aiter_bytes():yield chunk
            finally:await upstream.aclose();await client.aclose()
        response=StreamingResponse(chunks(),status_code=upstream.status_code,
            media_type=upstream.headers.get('content-type','application/json'),headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
        return await response(scope,replay,send)

def configure_results_formatter(app):
    """Preserve tool retrieval/presentation; align only its formatter runtime."""
    runtime=app.state.runtime
    gateway=runtime.gateway
    directory=getattr(runtime.vnext,'budget_dir','') or os.environ.get('PANK_VNEXT_BUDGET_DIR','')
    if directory:
        from pankagent_vnext.budget import Budget,PRICES
        PRICES['claude-sonnet-5-5']=(2,10)
        old=gateway.budget
        gateway.budget=Budget(Path(directory)/'budget.sqlite3',runtime.vnext.budget_usd)
        previous=app.router.lifespan_context
        @asynccontextmanager
        async def lifespan(parent):
            if hasattr(old,'aclose'):await old.aclose()
            async with previous(parent):yield
        app.router.lifespan_context=lifespan
    previous_options=gateway._options
    gateway._options=lambda: {'thinking':{'type':'between_tools'},'output_config':{'effort':'low'}} if gateway.settings.model=='claude-sonnet-5-5' else previous_options()
    return app
