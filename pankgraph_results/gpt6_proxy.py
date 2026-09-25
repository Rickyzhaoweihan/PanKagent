"""Additive dev ingress for the isolated GPT backend and unchanged demo asset.

The public namespace is /gpt6/. Returned API URLs stay inside that namespace.
The existing frontend source is unchanged.
No cookies, provider switches, operator endpoints or frontend rewrites are used.
"""
import re
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse

UPSTREAM = 'http://127.0.0.1:8798'


def install_gpt6_proxy(app, client):
    @app.get('/api/gpt6/')
    async def gpt6_info():
        return {'service':'PanKagent GPT-6 Sol', 'model':'gpt-6-sol', 'mode':'backend-only',
                'create_plan':{'method':'POST','url':'/gpt6/v2/plans', 'body':{'question':'What is T1D?'}},
                'workflow':'POST a question; poll plan_url until awaiting_confirmation, then POST plan_url + /confirm; stream events_url.',
                'frontend_source_modified':False}

    @app.api_route('/api/gpt6/{path:path}', methods=['GET', 'POST'])
    async def gpt6_proxy(path: str, request: Request):
        is_demo = request.method == 'GET' and path == 'demo'
        is_create = request.method == 'POST' and path == 'v2/plans'
        is_run = re.fullmatch(r'v2/(?:plans/[0-9a-f-]{36}(?:/(?:confirm|revise))?|runs/[0-9a-f-]{36}(?:/(?:events|cancel))?)', path)
        needs_post = path.endswith(('/confirm', '/revise', '/cancel'))
        if not (is_demo or is_create or (is_run and (request.method == 'POST') == needs_post)):
            raise HTTPException(404, 'Unknown GPT agent operation.')
        raw = await request.body()
        if len(raw) > 32000:
            raise HTTPException(413, 'Request exceeds the input limit.')
        query = request.url.query
        if query and not (is_demo and re.fullmatch(r'run=[0-9a-f-]{36}', query)) and not re.fullmatch(r'(?:after|after_sequence)=\d{1,10}', query):
            raise HTTPException(422, 'Unsupported replay parameter.')
        headers = {k: request.headers[k] for k in ('accept', 'content-type', 'last-event-id') if k in request.headers}
        upstream = client.build_request(request.method, UPSTREAM + '/' + path + ('?' + query if query else ''), headers=headers, content=raw)
        for name in ('authorization', 'cookie', 'x-operator-token', 'x-api-key'):
            upstream.headers.pop(name, None)
        response = await client.send(upstream, stream=True)
        if request.method == 'POST' and 'application/json' in response.headers.get('content-type', ''):
            try:
                await response.aread()
                payload = response.json()
                if isinstance(payload, dict):
                    for name in ('events_url', 'plan_url'):
                        if isinstance(payload.get(name), str) and payload[name].startswith('/v2/'):
                            payload[name] = '/gpt6' + payload[name]
                return JSONResponse(payload, status_code=response.status_code,
                    headers={'Cache-Control':'no-store', 'X-PanKagent-Model':'gpt-6-sol'})
            finally:
                await response.aclose()
        async def chunks():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
        return StreamingResponse(chunks(), status_code=response.status_code,
            media_type=response.headers.get('content-type', 'application/json'),
            headers={'Cache-Control':'no-store', 'X-Accel-Buffering':'no', 'X-PanKagent-Model':'gpt-6-sol'})
