"""Disposable, network-free recovery observations; never a scientific result."""
import asyncio
import json
from pathlib import Path
import tempfile

from tests_vnext.test_runtime import Gateway, Graph, Literature, PLAN, service, new_plan, wait_state


async def check():
    results = {'layer': 'disposable ASGI integration; fake scientific dependencies', 'upstream_calls': 0}
    with tempfile.TemporaryDirectory(prefix='pank-ux-recovery-') as directory:
        async with service(Path(directory), graph=Graph(delay=5)) as (client, runtime, gateway, graph, literature):
            created = (await client.post('/v2/plans', json={'question': 'What pathways and interaction partners connect HLA-DRA to antigen presentation in T1D?', 'event_source': 'synthetic_fault'})).json()
            for _ in range(100):
                if graph.calls: break
                await asyncio.sleep(.01)
            await client.post(f'/v2/runs/{created["run_id"]}/cancel')
            await asyncio.sleep(.02)
            before = (gateway.plans, gateway.syntheses, graph.calls, literature.calls)
            await client.get(created['events_url'])
            await client.get(f'/v2/runs/{created["run_id"]}')
            run = runtime.store.get(created['run_id'])
            assert run['status'] == 'cancelled' and graph.cancelled == 1
            assert before == (gateway.plans, gateway.syntheses, graph.calls, literature.calls)
            results['X02'] = {'status': 'passed', 'cancelled_active_graph_call': True, 'reconnect_extra_calls': 0,
                              'events': runtime.store.events_after(created['run_id'], 0), 'audit': runtime.store.audit_snapshot(created['run_id'])}
    with tempfile.TemporaryDirectory(prefix='pank-ux-recovery-') as directory:
        async with service(Path(directory), gateway=Gateway(plan={**PLAN, 'literature': True}), literature=Literature(available=False)) as (client, runtime, gateway, graph, literature):
            created = await new_plan(client, 'Is CFTR specifically enriched in ductal cells?', event_source='synthetic_fault')
            await client.post(f'/v2/plans/{created["plan_id"]}/confirm')
            run = await wait_state(client, created['run_id'], {'partial'})
            assert run['graph_answer'] and run['literature']['status'] == 'unavailable'
            assert 'UPSTREAM_SECRET' not in json.dumps(run)
            results['X04'] = {'status': 'passed', 'graph_answer_preserved': True, 'literature_status': 'unavailable',
                              'scientific_content_is_fixture': True, 'events': runtime.store.events_after(created['run_id'], 0)}
    return results


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(check())
    args.output.write_text(json.dumps(result, indent=2))
    args.output.chmod(0o600)
    print(json.dumps({k: v['status'] for k, v in result.items() if isinstance(v, dict)}))
