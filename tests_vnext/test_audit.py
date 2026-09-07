import asyncio
from uuid import uuid4

from pankagent_vnext.audit import provider_event
from pankagent_vnext.store import Store
from tests_vnext.test_runtime import Gateway, new_plan, service, wait_state


def test_revision_chain_confirmation_and_restart(tmp_path):
    store = Store(tmp_path)
    original = store.create('  INS in beta cells?  ', audit={'source': 'audit_replay'})
    store.update(original['run_id'], status='awaiting_confirmation', plan={'filters': ['INS', 'beta cell']})
    _, revised = store.revise(original['plan_id'], 'Only ND', audit={'revision_instruction': 'Only ND', 'revision_mode': 'instruction'})
    store.update(revised['run_id'], status='awaiting_confirmation', plan={'filters': ['ND']})
    assert store.confirm(revised['run_id'])
    assert not store.confirm(revised['run_id'])
    metadata = store.audit_metadata(revised['run_id'])
    assert metadata['original_question'] == '  INS in beta cells?  '
    assert metadata['revision_instruction'] == 'Only ND'
    assert metadata['parent_plan_id'] == original['plan_id']
    assert metadata['source'] == 'audit_replay'
    assert metadata['confirmed_plan_sha256'] == Store.content_hash({'filters': ['ND']})
    store.close()
    reopened = Store(tmp_path)
    assert reopened.audit_metadata(revised['run_id']) == metadata
    reopened.close()


def test_interactions_auth_dedup_and_no_extra_inference(tmp_path):
    async def scenario():
        async with service(tmp_path, operator_token='protected') as (client, runtime, gateway, graph, _):
            run = await new_plan(client)
            path = f'/v2/runs/{run["run_id"]}/interactions'
            event = dict(event_id=str(uuid4()), page_id=str(uuid4()), kind='graph_evidence_inspected',
                         target_id='ref-123', client_timestamp='2026-09-06T18:00:00Z', client_elapsed_ms=1234)
            assert (await client.post(path, json=event)).status_code == 403
            headers = {'Authorization': 'Bearer protected'}
            before = (gateway.plans, gateway.syntheses, graph.calls)
            assert (await client.post(path, json=event, headers=headers)).json()['status'] == 'recorded'
            assert (await client.post(path, json=event, headers=headers)).json()['status'] == 'duplicate'
            assert (gateway.plans, gateway.syntheses, graph.calls) == before
            assert len(runtime.store.audit_snapshot(run['run_id'])['events']) == 1
            bad = {**event, 'target_id': 'https://secret.example/?token=bad'}
            assert (await client.post(path, json=bad, headers=headers)).status_code == 422
            assert (await client.get(f'/v2/runs/{run["run_id"]}/audit')).status_code == 403
    asyncio.run(scenario())


def test_provider_attribution_concurrency_and_cancel(tmp_path):
    class LoggedGateway(Gateway):
        async def plan(self, question, history):
            provider_event('model_reserved', {'reservation_id': question})
            return await super().plan(question, history)
    async def scenario():
        async with service(tmp_path, gateway=LoggedGateway()) as (client, runtime, *_):
            a, b = await asyncio.gather(new_plan(client, 'A'), new_plan(client, 'B'))
            for run, question in [(a, 'A'), (b, 'B')]:
                events = runtime.store.audit_snapshot(run['run_id'])['events']
                assert [e['payload']['reservation_id'] for e in events] == [question]
                await client.post(f'/v2/runs/{run["run_id"]}/cancel')
                assert runtime.store.get(run['run_id'])['status'] == 'cancelled'
    asyncio.run(scenario())


def test_missing_legacy_metadata_not_invented_and_telemetry_failure_counted(tmp_path):
    store = Store(tmp_path)
    run = store.create('old question')
    store.db.execute('DELETE FROM run_audit')
    store.db.commit()
    assert store.audit_metadata(run['run_id']) is None
    store.update(run['run_id'], status='awaiting_confirmation')
    _, replacement = store.revise(run['plan_id'], 'new question')
    assert store.audit_metadata(replacement['run_id'])['revision_instruction'] is None
    assert store.audit_metadata(replacement['run_id'])['revision_mode'] == 'legacy_replacement'
    assert store.audit_event(run['run_id'], 'test', {'too_large': 'x' * 33000}) == 'unavailable'
    assert store.audit_dropped == 1
    assert store.get(run['run_id'])['status'] == 'superseded'
    store.close()
