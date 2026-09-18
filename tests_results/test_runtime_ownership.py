"""Results admission survives disconnects; stale owners serve snapshots only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from tests_results.test_app import service, finished
from tests_results.test_saved_functional_plot import runtime as saved_runtime


def test_disconnect_after_committed_result_still_launches_once(tmp_path):
    async def scenario():
        async with service(tmp_path) as s:
            committed, release = asyncio.Event(), asyncio.Event()
            original_call = s.runtime.io.call
            captured = {}
            async def injected(function, *args, **kwargs):
                value = await original_call(function, *args, **kwargs)
                if function == s.runtime.store.create:
                    captured['result'] = value[0]
                    committed.set()
                    await release.wait()
                return value
            s.runtime.io.call = injected
            # Use the same accepted template contract as the integration fixture.
            body = {'template_id': 'qtl_by_gene', 'parameters': {'gene_id': 'ENSG00000001084'}}
            request = asyncio.create_task(s.client.post('/api/results', json=body))
            await asyncio.wait_for(committed.wait(), 1)
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            assert len(s.runtime.admission.tasks) == 1
            release.set()
            result = await finished(s.client, captured['result']['result_id'])
            assert result['status'] == 'ready'
            s.runtime.io.call = original_call
            repeat = await s.client.post('/api/results', json=body)
            assert repeat.status_code == 202 and repeat.json()['result_id'] == result['result_id']
            assert s.query.calls == s.gateway.calls == 1
    asyncio.run(scenario())


def test_lost_owner_never_refreshes_saved_plot():
    async def scenario():
        obj, payload = saved_runtime()
        obj.store.owner = SimpleNamespace(snapshot=lambda: {'state': 'lost'})
        obj.io.call = AsyncMock(side_effect=AssertionError('stale owner must not start refresh'))
        assert await obj.refresh_saved_functional_plot('id', payload) == payload
        obj.resolve_resources.assert_not_awaited()
        assert obj.plot_refresh_tasks == {}
    asyncio.run(scenario())


def test_lease_lost_during_source_read_does_not_start_refresh():
    async def scenario():
        obj, payload = saved_runtime()
        async def lose_owner(function, *args):
            obj.shutting_down = True
            return {'template_id': 'functional_traces', 'parameters': {}}
        obj.io.call = lose_owner
        assert await obj.refresh_saved_functional_plot('id', payload) == payload
        obj.resolve_resources.assert_not_awaited()
        assert obj.plot_refresh_tasks == {}
    asyncio.run(scenario())


def test_lost_owner_download_rejected_without_asset_side_effect(tmp_path):
    async def scenario():
        async with service(tmp_path) as s:
            s.runtime.store.owner.state = 'lost'
            s.resources.download = AsyncMock(side_effect=AssertionError('no stale fetch or cache write'))
            response = await s.client.get('/api/resources/download?source=1_t1d-susie&credible_set=fixture')
            assert response.status_code == 503
            s.resources.download.assert_not_awaited()
    asyncio.run(scenario())


def test_fixed_dashboard_proxy_preserves_security_and_rejects_arbitrary_destinations(tmp_path):
    async def scenario():
        def upstream(request):
            return httpx.Response(401, text='Authentication required', headers={
                'WWW-Authenticate': 'Basic realm="Health"',
                'Content-Security-Policy': "default-src 'self'",
                'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                'Set-Cookie': 'secret=upstream'})
        async with service(tmp_path, upstream_handler=upstream) as s:
            response = await s.client.get('/pankgraph/health/', headers={
                'Authorization': 'Basic browser', 'Cookie': 'secret=browser', 'X-Api-Key': 'secret'})
            assert response.status_code == 401
            assert response.headers['www-authenticate'] == 'Basic realm="Health"'
            assert "default-src 'self'" in response.headers['content-security-policy']
            assert 'set-cookie' not in response.headers
            sent = s.upstream_calls[-1]
            assert str(sent.url) == 'http://127.0.0.1:8796/pankgraph/health/'
            assert sent.headers['authorization'] == 'Basic browser'
            assert 'cookie' not in sent.headers and 'x-api-key' not in sent.headers
            for path in ('/pankgraph/health/https://bad.invalid', '/pankgraph/health/?target=https://bad.invalid'):
                assert (await s.client.get(path)).status_code in {404, 422}
            assert (await s.client.post('/pankgraph/health/')).status_code == 405
            assert len(s.upstream_calls) == 1
    asyncio.run(scenario())
