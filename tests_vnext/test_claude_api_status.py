import asyncio
from types import SimpleNamespace

import httpx
import pytest

from pankagent_vnext.health import HealthMonitor
from pankgraph_health.collector import component


@pytest.mark.parametrize('status,expected', [
    ('operational', 'healthy'), ('degraded_performance', 'degraded'),
    ('partial_outage', 'degraded'), ('major_outage', 'unavailable'),
    ('under_maintenance', 'degraded'),
])
def test_api_component_overrides_provider_wide_indicator(monkeypatch, status, expected):
    payload = {'status': {'indicator': 'minor'}, 'components': [
        {'id': 'bpp5gb3hpjcl', 'name': 'Claude Cowork', 'status': 'degraded_performance'},
        {'id': 'k8w3r06qmzrp', 'name': 'Claude API (api.anthropic.com)', 'status': status},
    ]}
    result = probe(monkeypatch, payload)
    assert result['state'] == expected
    assert result['api_component_status'] == status
    assert 'provider_indicator' not in result
    row = component('agent', 'claude_provider', {'state': expected, 'details': result})
    assert row['label'] == 'Claude API service status'
    assert row['details']['api_component_status'] == status
    assert 'model access is checked separately' in row['scope']


@pytest.mark.parametrize('payload', [
    {}, [], {'components': None}, {'components': {}},
    {'status': {'indicator': 'none'}, 'components': []},
    {'components': [{'id': 'different', 'name': 'Claude API', 'status': 'operational'}]},
    {'components': [{'id': 'k8w3r06qmzrp', 'status': []}]},
    {'components': [{'id': 'k8w3r06qmzrp', 'status': 'new_unknown_status'}]},
    {'components': [{'id': 'k8w3r06qmzrp', 'status': 'operational'}] * 2},
])
def test_missing_ambiguous_or_unrecognized_component_is_unknown(monkeypatch, payload):
    assert probe(monkeypatch, payload) == {'state': 'unknown'}


def probe(monkeypatch, payload):
    real_client = httpx.AsyncClient
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=payload)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs))
    monitor = HealthMonitor(SimpleNamespace(provider_status_url='https://status.claude.com/api/v2/summary.json'),
                            None, None, None, None, lambda: {})
    result = asyncio.run(monitor._provider())
    assert len(calls) == 1 and calls[0].method == 'GET'
    assert calls[0].url.path == '/api/v2/summary.json'
    return result
