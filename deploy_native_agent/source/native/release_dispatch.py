"""Keep recorded runs on their original workflow while admitting new-release runs."""
import json
from starlette.responses import JSONResponse
from urllib.parse import unquote

class ReleaseDispatch:
    def __init__(self, app, current, legacy_workflow):
        self.app, self.current, self.legacy = app, current, legacy_workflow

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        chunks = []
        while True:
            item = await receive()
            if item['type'] != 'http.request':
                return
            chunks.append(item.get('body', b''))
            if not item.get('more_body'): break
        body = b''.join(chunks)
        used = False
        async def replay():
            nonlocal used
            if not used:
                used = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()
        parts = [unquote(p) for p in scope['path'].split('/') if p]
        old = any(p.split(':')[0] in self.legacy.runs or
                  any(p == r.get('plan_id') for r in self.legacy.runs.values())
                  for p in parts)
        if 'requests' in parts:
            index = parts.index('requests') + 1
            old = index < len(parts) and parts[index] in self.legacy.requests
        try: payload = json.loads(body) if body else {}
        except ValueError: payload = {}
        if isinstance(payload, dict):
            old = old or payload.get('run_id') in self.legacy.runs or payload.get('request_id') in self.legacy.requests
        if old and scope['method'] not in ('GET', 'HEAD') and any(p in ('confirm', 'revise') for p in parts):
            return await JSONResponse({'detail':'This saved plan belongs to an older graph release. Start a new question on PanKgraph_1104.'}, status_code=409)(scope, replay, send)
        target = self.app if old else self.current
        return await target(scope, replay, send)
