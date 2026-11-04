"""Serve the identical deployed dev frontend; vary only agent/results API routing."""
import os
import json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

app = FastAPI(title='Unmodified dev frontend comparison proxy')
VARIANT = os.environ.get('SIMPLE_COMPARISON_VARIANT', 'simple')
DEV = 'http://127.0.0.1:18805'
SIMPLE = 'http://127.0.0.1:18801'
PREFIX = '/pankgraph-vnext/api'
DROP = {'host', 'connection', 'content-length', 'transfer-encoding', 'content-encoding', 'x-forwarded-for', 'x-forwarded-proto'}

def simple_route(internal, method, body=b''):
    """Replace agent work only; keep templates and historical runs on dev."""
    if internal == '/agent/v2/plans' and method == 'POST':
        return True
    if internal.startswith('/agent/v2/requests/'):
        return True
    if internal.startswith('/agent/v2/'):
        identity = internal.split('/')[4] if len(internal.split('/')) > 4 else ''
        return identity.startswith(('simple-', 'plan-'))
    if internal == '/results' and method == 'POST':
        try:
            return str(json.loads(body).get('run_id', '')).startswith('simple-')
        except (ValueError, AttributeError):
            return False
    if internal.startswith('/results/'):
        return internal.split('/')[2].startswith('simple-')
    return False

@app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
async def proxy(path: str, request: Request):
    route = '/' + path
    internal = route[len(PREFIX):] if route.startswith(PREFIX + '/') else ''
    body = await request.body()
    experimental = VARIANT == 'simple' and simple_route(internal, request.method, body)
    target = (SIMPLE + internal) if experimental else (DEV + route)
    if request.url.query:
        target += '?' + request.url.query
    client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10), follow_redirects=False)
    try:
        upstream = await client.send(client.build_request(request.method, target, content=body,
            headers={k:v for k,v in request.headers.items() if k.lower() not in DROP}), stream=True)
    except Exception:
        await client.aclose()
        raise
    async def close():
        await upstream.aclose()
        await client.aclose()
    return StreamingResponse(upstream.aiter_bytes(), status_code=upstream.status_code,
        headers={k:v for k,v in upstream.headers.items() if k.lower() not in DROP}, background=BackgroundTask(close))
