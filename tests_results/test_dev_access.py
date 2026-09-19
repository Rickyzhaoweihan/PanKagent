"""Dev bootstrap, fixed health alias and trusted Origin; all work is mocked."""
import asyncio

import httpx
import pytest

from pankgraph_results.auth import hash_password
from pankgraph_results.config import ResultsSettings
from pankgraph_health.app import create_app as health_app
from pankgraph_health.collector import Collector
from pankgraph_health.settings import Settings as HealthSettings
from pankgraph_health.store import History
from tests_results.test_app import service


AUTH = httpx.BasicAuth('demo', 'synthetic-test-password')


def test_bootstrap_authenticates_then_redirects_without_work(tmp_path):
    async def run():
        async with service(tmp_path, testing=False) as s:
            for path in ('/pankgraph-vnext/access', '/pankgraph-vnext/api/access'):
                response = await s.client.get(path)
                assert response.status_code == 401
                assert response.headers['cache-control'] == 'no-store'
                if path.endswith('/api/access'):
                    assert 'www-authenticate' not in response.headers
                else:
                    assert 'PanKgraph demo' in response.headers['www-authenticate']
                assert (await s.client.get(path, auth=httpx.BasicAuth('demo', 'wrong'))).status_code == 401
            response = await s.client.get('/pankgraph-vnext/access?return_to=%2Fagent-vnext', auth=AUTH)
            assert response.status_code == 303
            assert response.headers['location'] == '/agent-vnext'
            assert response.headers['cache-control'] == 'no-store'
            response = await s.client.get('/pankgraph-vnext/api/access', auth=AUTH)
            assert response.status_code == 200 and response.json() == {'authenticated': True}
            assert response.headers['cache-control'] == 'no-store'
            assert s.query.calls == s.query.searches == s.layout.calls == s.gateway.calls == 0
            assert s.resources.calls == s.resources.downloads == 0
            assert not s.upstream_calls
    asyncio.run(run())


@pytest.mark.parametrize('method,path,challenge', [
    ('GET', '/pankgraph-vnext/api/access', False),
    ('GET', '/pankgraph-vnext/api/access?probe=1', False),
    ('HEAD', '/pankgraph-vnext/api/access', True),
    ('POST', '/pankgraph-vnext/api/access', True),
    ('GET', '/pankgraph-vnext/api/access/', True),
    ('GET', '/pankgraph-vnext/api/access-other', True),
    ('GET', '/pankgraph-vnext/access?return_to=%2Fagent-vnext', True),
    ('GET', '/pankgraph-vnext/api/results', True),
])
def test_only_get_access_probe_omits_native_auth_challenge(tmp_path, method, path, challenge):
    async def run():
        async with service(tmp_path, testing=False) as s:
            for auth in (None, httpx.BasicAuth('demo', 'wrong')):
                response = await s.client.request(method, path, auth=auth)
                assert response.status_code == 401
                assert response.headers['cache-control'] == 'no-store'
                assert ('www-authenticate' in response.headers) is challenge
                if challenge:
                    assert 'PanKgraph demo' in response.headers['www-authenticate']
                if method != 'HEAD':
                    assert response.json() == {'detail': 'Demo login required.'}
            assert not s.upstream_calls
            assert s.query.calls == s.query.searches == s.layout.calls == s.gateway.calls == 0
    asyncio.run(run())


@pytest.mark.parametrize('query', [
    'return_to=https%3A%2F%2Fevil.example', 'return_to=%2F%2Fevil.example',
    'return_to=%2Fagent-vnext%3Ftoken%3Dx', 'return_to=%2Fagent-vnext%23fragment',
    'return_to=%2Fagent-vnext%2F..%2F', 'return_to=%5C%5Cevil.example',
    'return_to=%2Fagent-vnext%0d%0aLocation%3Aevil', 'return_to=',
    'return_to=%2Fagent-vnext&return_to=%2Fevil', 'return_to=%252Fagent-vnext',
    'url=https%3A%2F%2Fevil.example',
])
def test_bootstrap_rejects_unapproved_destinations(tmp_path, query):
    async def run():
        async with service(tmp_path, testing=False) as s:
            response = await s.client.get('/pankgraph-vnext/access?' + query, auth=AUTH)
            assert response.status_code == 400 and 'location' not in response.headers
            assert not s.upstream_calls and s.gateway.calls == 0
    asyncio.run(run())


@pytest.mark.parametrize('origin', ['https://evil.example', 'https://dev.pankgraph.org/',
    'http://dev.pankgraph.org', 'https://dev.pankgraph.org.evil.example', '*'])
def test_trusted_origin_configuration_is_fixed(origin):
    with pytest.raises(ValueError, match='unsupported_trusted_browser_origin'):
        ResultsSettings(trusted_browser_origin=origin)


def test_trusted_dev_origin_preserves_cross_site_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv('PANK_RESULTS_TRUSTED_BROWSER_ORIGIN', 'https://dev.pankgraph.org')
    async def run():
        async with service(tmp_path, testing=False) as s:
            for origin, site, status in [
                ('http://results.local', 'same-origin', 422),
                ('https://dev.pankgraph.org', 'same-origin', 422),
                ('https://dev.pankgraph.org', 'cross-site', 403),
                ('https://evil.example', 'same-origin', 403),
                ('https://dev.pankgraph.org.evil.example', 'same-origin', 403),
            ]:
                response = await s.client.post('/pankgraph-vnext/api/results', auth=AUTH, json={},
                    headers={'Origin': origin, 'Sec-Fetch-Site': site, 'X-Forwarded-For': '192.0.2.1'})
                assert response.status_code == status
                assert 'access-control-allow-origin' not in response.headers
            assert s.gateway.calls == s.query.calls == 0 and not s.upstream_calls
    asyncio.run(run())


def test_dev_origin_is_denied_without_protected_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv('PANK_RESULTS_TRUSTED_BROWSER_ORIGIN', raising=False)
    async def run():
        async with service(tmp_path, testing=False) as s:
            response = await s.client.post('/pankgraph-vnext/api/results', auth=AUTH, json={},
                headers={'Origin': 'https://dev.pankgraph.org', 'Sec-Fetch-Site': 'same-origin'})
            assert response.status_code == 403
    asyncio.run(run())


def test_health_alias_retains_independent_operator_boundary(tmp_path):
    async def run():
        cfg = HealthSettings(tmp_path/'health', 'operator', hash_password('operator-only'))
        cfg.state_dir.mkdir()
        collector = Collector(cfg, History(tmp_path/'health'))
        monitor = health_app(cfg, collector)
        calls = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=monitor), base_url='http://health.local') as health:
            async def upstream(request):
                calls.append(request)
                return await health.request(request.method, str(request.url), headers=request.headers)
            async with service(tmp_path, testing=False, upstream_handler=upstream) as s:
                operator = httpx.BasicAuth('operator', 'operator-only')
                for prefix in ('/pankgraph/health', '/pankgraph-vnext/health-dashboard'):
                    for suffix in ('/', '/dashboard.js', '/dashboard.css', '/api/snapshot', '/api/history?hours=1', '/api/incidents', '/api/metrics'):
                        assert (await s.client.get(prefix+suffix)).status_code == 401
                        assert (await s.client.get(prefix+suffix, auth=AUTH)).status_code == 401
                        response = await s.client.get(prefix+suffix, auth=operator)
                        assert response.status_code == 200
                    response = await s.client.get(prefix, auth=operator)
                    assert response.status_code == 307 and response.headers['location'] == prefix+'/'
                    assert (await s.client.post(prefix+'/api/snapshot', auth=operator)).status_code == 405
                    assert (await s.client.get(prefix+'/api/snapshot?url=http://evil.example', auth=operator)).status_code == 422
                    assert (await s.client.get(prefix+'/settings.py', auth=operator)).status_code == 404
                assert (await s.client.get('/pankgraph-vnext/health-dashboard-other', auth=operator)).status_code == 401
                assert all(r.method == 'GET' and r.url.host == '127.0.0.1' and r.url.port == 8796
                           and r.url.path.startswith('/pankgraph/health/') for r in calls)
                assert collector.last_cycle is None and s.gateway.calls == s.query.calls == 0
        await collector.close()
    asyncio.run(run())
