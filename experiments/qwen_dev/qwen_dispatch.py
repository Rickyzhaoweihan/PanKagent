"""Switch only new agent requests; preserve the original frontend protocol/UI."""
import asyncio,json,re
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse,StreamingResponse
from dev_integration import body_and_replay
QWEN='http://127.0.0.1:18827'
HAIKU='http://127.0.0.1:8794'
IDENTITY=re.compile(r'^/agent/v2/(?:runs|plans)/(qwen-[^/]+)(?:/|$)')
REQUEST=re.compile(r'^/agent/v2/requests/([0-9a-fA-F-]{36})(?:/cancel)?$')

def route_kind(path,method,body):
    if path.startswith('/api/qwen/'):
        return 'qwen',path[len('/api/qwen'):]
    if path.startswith('/api/haiku/'):
        return 'haiku',path[len('/api/haiku'):]
    if not path.startswith('/api/'):
        return 'other',path
    internal=path[4:]
    if IDENTITY.match(internal) or internal.startswith('/results/qwen-'):
        return 'qwen',internal
    if internal=='/results' and method=='POST' and str(body.get('run_id','')).startswith('qwen-'):
        return 'qwen',internal
    match=REQUEST.fullmatch(internal)
    if match:return 'request',internal
    if internal=='/agent/v2/plans' and method=='POST':return 'admit',internal
    return 'other',internal

class QwenDispatch:
    def __init__(self,app):self.app=app
    async def request_owner(self,request_id):
        if not isinstance(request_id,str) or not re.fullmatch(r'[0-9a-fA-F-]{36}',request_id):return 'haiku'
        async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
            # Read-only ownership checks; never admit or run a model here.
            responses=await asyncio.gather(client.get(QWEN+'/agent/v2/requests/'+request_id),client.get(HAIKU+'/v2/requests/'+request_id),return_exceptions=True)
        for owner,response in zip(('qwen','haiku'),responses):
            if isinstance(response,Exception):raise RuntimeError('Request ownership unavailable')
            if response.status_code==200:return owner
            if response.status_code!=404:raise RuntimeError('Request ownership unknown')
        # New requests use the current agent facade on 8794; old ownership remains above.
        return 'haiku'
    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        if scope['type']!='http' or not path.startswith('/api/'):
            return await self.app(scope,receive,send)
        candidate=path.startswith(('/api/qwen/','/api/haiku/','/api/agent/v2/','/api/results'))
        if not candidate:return await self.app(scope,receive,send)
        try:body,replay=await body_and_replay(receive)
        except ValueError:return await JSONResponse({'detail':'Input exceeds limit'},413)(scope,receive,send)
        try:data=json.loads(body) if body else {}
        except (ValueError,UnicodeDecodeError):data={}
        if not isinstance(data,dict):data={}
        kind,internal=route_kind(path,scope['method'],data)
        if kind in ('admit','request'):
            request_id=data.get('request_id') if kind=='admit' else internal.split('/')[4]
            try:kind=await self.request_owner(request_id)
            except Exception:return await JSONResponse({'detail':'Cannot verify saved request ownership; retry without creating a new request'},503)(scope,replay,send)
        if kind=='other':return await self.app(scope,replay,send)
        if kind=='haiku':
            mapped={**scope,'path':'/api'+internal,'raw_path':('/api'+internal).encode()}
            return await self.app(mapped,replay,send)
        if not (internal.startswith('/agent/v2/') or internal.startswith('/results') or internal=='/health'):
            return await JSONResponse({'detail':'Unknown local Qwen route'},404)(scope,replay,send)
        query=scope.get('query_string',b'').decode()
        url=QWEN+internal+('?' + query if query else '')
        request=Request(scope,replay)
        headers={k:v for k,v in request.headers.items() if k.lower() in ('accept','content-type','last-event-id')}
        client=httpx.AsyncClient(timeout=httpx.Timeout(400,connect=5),trust_env=False)
        try:up=await client.send(client.build_request(scope['method'],url,headers=headers,content=body),stream=True)
        except Exception:
            await client.aclose()
            return await JSONResponse({'detail':'Local Qwen temporarily unavailable'},502)(scope,replay,send)
        async def chunks():
            try:
                async for c in up.aiter_bytes():yield c
            finally:await up.aclose();await client.aclose()
        return await StreamingResponse(chunks(),status_code=up.status_code,media_type=up.headers.get('content-type','application/json'),headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})(scope,replay,send)
