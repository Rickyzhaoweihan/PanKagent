"""Standalone UI + sanitized, authenticated dashboard API; no app route edits."""
import asyncio
import base64
import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .settings import Settings
from .collector import Collector
from .store import History

PREFIX = '/pankgraph/health'


def valid_auth(raw, settings):
    try:
        if len(raw)>4096 or not raw.startswith('Basic '): return False
        user,password=base64.b64decode(raw[6:],validate=True).decode().split(':',1)
        algorithm,rounds,salt,expected=settings.password_hash.split('$')
        if algorithm!='pbkdf2_sha256' or not 100000<=int(rounds)<=1000000: return False
        digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),int(rounds)).hex()
        return hmac.compare_digest(user,settings.user) and hmac.compare_digest(digest,expected)
    except (ValueError,UnicodeError): return False


def create_app(settings=None, collector=None):
    settings=settings or Settings.load()
    history=collector.history if collector else History(settings.state_dir,settings.retention_days)
    collector=collector or Collector(settings,history)
    @asynccontextmanager
    async def lifespan(app):
        await collector.start()
        yield
        await collector.close()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.collector=collector
    cache={}
    @app.middleware('http')
    async def authenticate(request:Request,call_next):
        # Only the supervisor gets unauthenticated liveness; detailed data always needs Basic.
        local=request.client and request.client.host in ('127.0.0.1','::1') and not request.headers.get('x-forwarded-for')
        if request.url.path=='/health/live' and local:
            return await call_next(request)
        if request.method not in ('GET','HEAD'): return JSONResponse({'error':'read_only'},status_code=405)
        raw=request.headers.get('authorization','')
        key=hashlib.sha256(raw.encode()).hexdigest();now=time.monotonic()
        if cache.get(key,0)<now:
            if not await asyncio.to_thread(valid_auth,raw,settings):
                return JSONResponse({'error':'authentication_required'},401,headers={'WWW-Authenticate':'Basic realm="PanKgraph health"'})
            if len(cache)>128:cache.clear()
            cache[key]=now+30
        response=await call_next(request)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @app.get('/health/live')
    async def live():
        snap=collector.snapshot()
        return {'service':'pankgraph-health','state':snap['monitor']['state'], 'collector_age_seconds':snap['monitor']['age_seconds'],
                'uptime_seconds':time.time()-collector.started}
    @app.get('/')
    async def root():return RedirectResponse(PREFIX+'/')
    @app.get(PREFIX)
    async def redirect():return RedirectResponse(PREFIX+'/')
    @app.get(PREFIX+'/api/snapshot')
    async def snapshot():return collector.snapshot()
    @app.get(PREFIX+'/api/history')
    async def history_api(hours:int=Query(24,ge=1,le=168)):return await asyncio.to_thread(history.history,hours)
    @app.get(PREFIX+'/api/incidents')
    async def incidents():return {'incidents':await asyncio.to_thread(history.incidents)}
    @app.get(PREFIX+'/api/metrics')
    async def metrics():
        snap=collector.snapshot();lines=[]
        for c in snap['components']:
            for state in ('healthy','degraded','unavailable','unknown'):
                lines.append(f'pank_health_component_state{{component="{c["id"]}",state="{state}"}} {int(c["state"]==state)}')
        lines.append('pank_health_collector_stale '+str(int(snap['stale'])))
        return PlainTextResponse('\n'.join(lines)+'\n')
    web=Path(__file__).parent/'web'
    @app.get(PREFIX+'/')
    async def index():return FileResponse(web/'index.html')
    @app.get(PREFIX+'/{asset}')
    async def asset(asset:str):
        if asset not in ('dashboard.js','dashboard.css'):return JSONResponse({'error':'not_found'},404)
        return FileResponse(web/asset)
    return app
