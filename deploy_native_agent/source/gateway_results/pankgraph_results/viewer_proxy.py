"""Read-only exact-release viewer bridge, inside the existing site authentication."""
import json
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


def install_viewer_proxy(app, http, *, url='', release='', token_file=''):
    if url:
        parsed=urlsplit(url)
        if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','::1'} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('viewer upstream must be configured loopback')
    async def forward(operation, request):
        if not url or not release or not token_file:
            raise HTTPException(503,'Detailed evidence is not configured.')
        if operation=='schema':
            if list(request.query_params.keys())!=['release_id'] or len(request.query_params.multi_items())!=1:
                raise HTTPException(422,'An exact release is required.')
            body=None;requested=request.query_params['release_id']
        else:
            if request.url.query:raise HTTPException(422,'Unsupported evidence query parameter.')
            raw=bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw)>65536:raise HTTPException(413,'Evidence request exceeds limit.')
            try:body=json.loads(raw)
            except (ValueError,UnicodeError):raise HTTPException(422,'Invalid evidence request.') from None
            if not isinstance(body,dict):raise HTTPException(422,'Invalid evidence request.')
            allowed={'release_id','ref'} if operation=='inspect' else {'release_id','ref','table_id','fields','filters','record_id','limit','cursor'}
            if set(body)-allowed:raise HTTPException(422,'Unknown evidence parameter.')
            ref=body.get('ref')
            if not isinstance(ref,dict) or set(ref)!={'kind','type_id','id'} or ref['kind'] not in {'node','edge'} or not all(isinstance(ref[k],str) and 0<len(ref[k])<=1024 for k in ('type_id','id')):
                raise HTTPException(422,'An exact typed graph object is required.')
            requested=body.get('release_id')
        if requested!=release:raise HTTPException(409,'Detailed evidence is unavailable for this graph version.')
        try:token=Path(token_file).read_text().strip()
        except OSError:raise HTTPException(503,'Detailed evidence is temporarily unavailable.') from None
        if not token:raise HTTPException(503,'Detailed evidence is temporarily unavailable.')
        # Fetch the complete selected-object annotations once. Compact selection
        # is presentation-only; expansion must not silently reuse a partial record.
        if operation=='inspect':body={**body,'mode':'expanded'}
        upstream=http.build_request('GET' if body is None else 'POST',url.rstrip('/')+'/api/viewer/v1/'+operation,json=body)
        # Browser credentials and client cookies never cross into the data API.
        for name in ('authorization','cookie','x-api-key','x-operator-token'):upstream.headers.pop(name,None)
        upstream.headers['authorization']='Bearer '+token
        try:
            response=await http.send(upstream,stream=True)
            try:
                if response.status_code>=400:
                    status=response.status_code if response.status_code in {400,404,409,422} else 503
                    raise HTTPException(status,'Requested evidence is unavailable for this object or version.')
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>16*1024*1024:raise HTTPException(502,'Evidence response exceeds limit.')
                result=json.loads(raw)
            finally:await response.aclose()
        except (httpx.HTTPError,ValueError,UnicodeError):raise HTTPException(503,'Detailed evidence is temporarily unavailable.') from None
        if not isinstance(result,dict) or result.get('release_id',release)!=release:
            raise HTTPException(502,'Evidence response version mismatch.')
        if operation=='schema' and result.get('release_id')!=release:
            raise HTTPException(502,'Evidence schema version mismatch.')
        if operation=='inspect' and result.get('ref')!=body['ref']:
            raise HTTPException(502,'Evidence object mismatch.')
        return JSONResponse({**result,'release_id':release},headers={'Cache-Control':'no-store'})

    @app.get('/api/viewer/v1/schema')
    async def schema(request:Request):return await forward('schema',request)

    @app.post('/api/viewer/v1/inspect')
    async def inspect(request:Request):return await forward('inspect',request)

    @app.post('/api/viewer/v1/table')
    async def table(request:Request):return await forward('table',request)
