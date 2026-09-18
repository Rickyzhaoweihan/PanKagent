import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from pankgraph_health.app import create_app, PREFIX
from pankgraph_health.settings import Settings
from pankgraph_health.store import History
from pankgraph_health.collector import Collector
from pankgraph_results.auth import DemoAuthentication, hash_password


def basic(user, password):
    return 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()


def test_operator_namespace_rejects_regular_demo_credentials(tmp_path):
    async def run():
        cfg = Settings(tmp_path, 'operator', hash_password('operator-only'))
        app = create_app(cfg, Collector(cfg, History(tmp_path)))
        app.add_middleware(DemoAuthentication, settings=SimpleNamespace(
            testing=False, basic_user='regular', password_hash=hash_password('regular-only')))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for path in ('/', '/dashboard.js', '/dashboard.css', '/api/snapshot', '/api/history', '/api/incidents', '/api/metrics'):
                for header in ({}, {'Authorization': basic('regular', 'regular-only')}):
                    denied = await client.get(PREFIX + path, headers=header)
                    assert denied.status_code == 401
                    assert 'PanKgraph health' in denied.headers['www-authenticate']
                allowed = await client.get(PREFIX + path, headers={'Authorization': basic('operator', 'operator-only')})
                assert allowed.status_code == 200
            assert (await client.post(PREFIX+'/api/snapshot', headers={'Authorization':basic('operator','operator-only')})).status_code == 405
            # Similar path names never receive the operator namespace exception.
            assert (await client.get('/pankgraph/health-other', headers={'Authorization':basic('operator','operator-only')})).status_code == 401
    asyncio.run(run())


def test_settings_require_separate_protected_operator_file(tmp_path, monkeypatch):
    agent = tmp_path/'agent.env'; agent.write_text('PANK_VNEXT_OPERATOR_TOKEN=fixture\n'); agent.chmod(0o600)
    regular_hash = hash_password('regular-only')
    results = tmp_path/'results.env'
    results.write_text(f'PANK_RESULTS_BASIC_USER=regular\nPANK_RESULTS_PASSWORD_HASH="{regular_hash}"\n'); results.chmod(0o600)
    state = tmp_path/'state'; state.mkdir(mode=0o700)
    monkeypatch.setenv('PANK_HEALTH_AGENT_ENV',str(agent)); monkeypatch.setenv('PANK_HEALTH_RESULTS_ENV',str(results)); monkeypatch.setenv('PANK_HEALTH_STATE_DIR',str(state))
    with pytest.raises(FileNotFoundError): Settings.load()
    operator = state/'operator-auth.json'
    operator.write_text(json.dumps({'user':'operator','password_hash':regular_hash})); operator.chmod(0o600)
    with pytest.raises(ValueError, match='Separate operator'): Settings.load()
    operator.write_text(json.dumps({'user':'operator','password_hash':hash_password('operator-only')}))
    assert Settings.load().user == 'operator'
    operator.chmod(0o644)
    with pytest.raises(ValueError, match='owner-only'): Settings.load()


def test_local_proxy_never_injects_demo_credentials_into_operator_route():
    import http.client
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from deploy_results.local_demo_proxy import make_handler
    seen = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            seen.append((self.path, self.headers.get('Authorization'), self.headers.get('X-Forwarded-For')))
            self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    proxy = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
    proxy.RequestHandlerClass = make_handler('Basic regular-demo', proxy.server_port, upstream.server_port)
    threads = [Thread(target=s.serve_forever, daemon=True) for s in (upstream, proxy)]
    for t in threads: t.start()
    try:
        for path, auth, expected in [('/pankgraph/health/', None, None),
                ('/pankgraph/health/api/snapshot', 'Basic operator-only', 'Basic operator-only'),
                ('/pankgraph-vnext/', 'Basic operator-only', 'Basic regular-demo')]:
            conn = http.client.HTTPConnection('127.0.0.1', proxy.server_port, timeout=3)
            conn.request('GET', path, headers={'Authorization':auth} if auth else {})
            response = conn.getresponse(); assert response.status == 200; response.read(); conn.close()
            assert seen[-1] == (path, expected, '127.0.0.1')
    finally:
        for s in (proxy, upstream): s.shutdown(); s.server_close()
        for t in threads: t.join(timeout=2)
