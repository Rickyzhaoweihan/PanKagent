import asyncio
import json

import httpx
import pytest

from pankgraph_health.collector import Collector, observation
from pankgraph_health.settings import Settings
from pankgraph_health.store import History


def protected_settings(tmp_path, monkeypatch, agent_values=''):
    agent = tmp_path / 'agent.env'
    agent.write_text('PANK_VNEXT_OPERATOR_TOKEN=private-fixture-token\n' + agent_values)
    agent.chmod(0o600)
    results = tmp_path / 'results.env'
    results.write_text('PANK_RESULTS_PASSWORD_HASH=regular-hash\n')
    results.chmod(0o600)
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    operator = state / 'operator-auth.json'
    operator.write_text(json.dumps({'user': 'operator', 'password_hash': 'operator-hash'}))
    operator.chmod(0o600)
    monkeypatch.setenv('PANK_HEALTH_AGENT_ENV', str(agent))
    monkeypatch.setenv('PANK_HEALTH_RESULTS_ENV', str(results))
    monkeypatch.setenv('PANK_HEALTH_STATE_DIR', str(state))
    for name in ('PANK_HEALTH_CYPHER_REPLICA_A_URL', 'PANK_HEALTH_CYPHER_REPLICA_B_URL'):
        monkeypatch.delenv(name, raising=False)


def test_gateway_follows_protected_agent_configuration_and_replicas_are_explicit(tmp_path, monkeypatch):
    protected_settings(tmp_path, monkeypatch,
        'PANK_VNEXT_CYPHER_URL=http://127.0.0.1:44917/\n'
        'PANK_HEALTH_CYPHER_REPLICA_A_URL=http://127.0.0.1:44918\n'
        'PANK_HEALTH_CYPHER_REPLICA_B_URL=http://[::1]:44919\n')
    # An unrelated parent-shell endpoint must not override the service's configured gateway.
    monkeypatch.setenv('PANK_VNEXT_CYPHER_URL', 'http://127.0.0.1:23917')
    cfg = Settings.load()
    assert cfg.cypher_url == 'http://127.0.0.1:44917'
    assert cfg.cypher_replica_a_url == 'http://127.0.0.1:44918'
    assert cfg.cypher_replica_b_url == 'http://[::1]:44919'
    monkeypatch.setenv('PANK_HEALTH_CYPHER_REPLICA_A_URL', '')
    monkeypatch.setenv('PANK_HEALTH_CYPHER_REPLICA_B_URL', 'http://127.0.0.1:45919')
    cfg = Settings.load()
    assert cfg.cypher_replica_a_url == ''
    assert cfg.cypher_replica_b_url == 'http://127.0.0.1:45919'


def test_default_endpoints_use_job_owned_forwarding_without_inventing_replica_b(tmp_path, monkeypatch):
    protected_settings(tmp_path, monkeypatch)
    cfg = Settings.load()
    assert cfg.cypher_url == 'http://127.0.0.1:33917'
    assert cfg.cypher_replica_a_url == 'http://127.0.0.1:33918'
    assert cfg.cypher_replica_b_url == ''


@pytest.mark.parametrize('url', [
    None, 123, 'http://127.0.0.1', 'http://127.0.0.1:0', 'http://127.0.0.1:65536',
    'https://127.0.0.1:33917', 'http://192.0.2.1:33917', 'http://example.com:33917',
    'http://127.0.0.1.example.com:33917', 'http://private-token@127.0.0.1:33917',
    'http://user:private-token@127.0.0.1:33917', 'http://127.0.0.1:33917/health',
    'http://127.0.0.1:33917?token=private-token', 'http://127.0.0.1:33917#private-token',
    ' http://127.0.0.1:33917', 'http://127.0.0.1:33917\n', 'x' * 257,
])
@pytest.mark.parametrize('field', ['cypher_url', 'cypher_replica_a_url', 'cypher_replica_b_url'])
def test_invalid_endpoints_fail_closed_without_echoing_values(tmp_path, field, url):
    with pytest.raises(ValueError, match='loopback HTTP base URL') as exc:
        Settings(tmp_path, 'operator', 'hash', **{field: url})
    assert 'private-token' not in str(exc.value)


def test_gateway_cannot_be_disabled(tmp_path):
    with pytest.raises(ValueError, match='loopback HTTP base URL'):
        Settings(tmp_path, 'operator', 'hash', cypher_url='')


@pytest.mark.parametrize('replica_b', ['', 'http://[::1]:44919'])
def test_collection_probes_only_configured_endpoints_and_never_sends_tokens(tmp_path, replica_b):
    async def run():
        cfg = Settings(tmp_path, 'operator', 'hash', agent_token='private-fixture-token',
                       cypher_url='http://127.0.0.1:44917',
                       cypher_replica_a_url='http://127.0.0.1:44918',
                       cypher_replica_b_url=replica_b)
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(200, json={'status': 'ok', 'backends_up': '1/1'})

        collector = Collector(cfg, History(tmp_path), httpx.MockTransport(handle))

        async def service(*args):
            return [], {}, {}, {}

        async def frontend():
            return observation('frontend.delivery', 'Frontend', 'healthy')

        collector.service, collector.frontend, collector.dev_frontend = service, frontend, frontend
        try:
            await collector.collect()
            endpoints = {str(request.url) for request in calls}
            expected = {cfg.cypher_url + '/health', cfg.cypher_replica_a_url + '/health', cfg.functional_url}
            if replica_b:
                expected.add(replica_b + '/health')
            assert endpoints == expected
            assert all(request.method == 'GET' and 'authorization' not in request.headers for request in calls)
            snapshot = collector.snapshot()
            assert 'private-fixture-token' not in json.dumps(snapshot)
            replica = next(item for item in snapshot['components'] if item['id'] == 'cypher.replica_b')
            if replica_b:
                assert replica['state'] == 'healthy'
            else:
                assert replica['state'] == 'unknown'
                assert replica['error_category'] == 'not_configured'
                assert replica['checked_at'] is None
                assert replica['required'] is False
        finally:
            await collector.close()

    asyncio.run(run())
