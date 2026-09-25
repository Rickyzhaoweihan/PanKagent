"""Answer deadlines and transactional replay; no provider or database calls."""
import asyncio

import pytest

from pankagent_vnext.store import Store
from test_runtime import Gateway, service, new_plan, wait_state


@pytest.mark.parametrize('stall', [False, True])
def test_writing_has_own_deadline_and_timeout_replays_exactly(tmp_path, stall):
    class Writer(Gateway):
        async def synthesize(self, question, evidence):
            self.syntheses += 1
            yield 'Verified evidence [G1]. '
            await asyncio.sleep(1 if stall else .18)
            yield 'Finished.'

    async def scenario():
        async with service(tmp_path, gateway=Writer(), run_timeout=.1,
                           answer_timeout=.1 if stall else .8) as (client, runtime, gateway, graph, _):
            created = await new_plan(client)
            await client.post(f"/v2/plans/{created['plan_id']}/confirm")
            run = await wait_state(client, created['run_id'], {'completed', 'partial', 'failed'})
            events = runtime.store.events_after(created['run_id'], 0, limit=1000)
            text = ''.join(e['payload']['text'] for e in events
                           if e['type'] == 'graph_answer' and e['payload'].get('delta'))
            assert text == run['graph_answer']
            assert gateway.syntheses == graph.calls == 1
            if stall:
                assert run['status'] == 'partial'
                assert run['evidence']['synthesis_error']['stage'] == 'writing_answer'
                assert any(d['code'] == 'E10.TIMEOUT' for d in run['diagnostics'])
                assert 'deadline' in text
            else:
                assert run['status'] == 'completed'
                assert text.endswith('Finished.')
    asyncio.run(scenario())


def test_answer_and_event_rollback_together(tmp_path, monkeypatch):
    store = Store(tmp_path)
    try:
        run = store.create('fixture')
        rid = run['run_id']
        def fail(*args):
            raise RuntimeError('injected event insertion failure')
        with monkeypatch.context() as patch:
            patch.setattr(store, '_event_in_transaction', fail)
            with pytest.raises(RuntimeError):
                store.persist_answer_event(rid, 'orphan', {'text': 'orphan', 'delta': True})
        assert not store.get(rid)['graph_answer']
        assert not [e for e in store.events_after(rid, 0) if e['type'] == 'graph_answer']
        store.persist_answer_event(rid, 'committed', {'text': 'committed', 'delta': True})
        store.update(rid, status='cancelled')
        assert store.persist_answer_event(rid, 'late', {'text': 'late', 'delta': True}) is None
        assert store.get(rid)['graph_answer'] == 'committed'
    finally:
        store.close()
