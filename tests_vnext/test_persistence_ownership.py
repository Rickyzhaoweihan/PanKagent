"""Storage contention, bounded admission and single-owner recovery contracts."""
import asyncio
import sqlite3
import threading
import time

import pytest

from pankagent_vnext.budget import Budget
from pankagent_vnext.ownership import OwnerAlive, OwnerLost
from pankagent_vnext.persistence import PersistenceBusy, SerializedPersistence
from pankagent_vnext.store import Store
from pankgraph_results.store import ResultStore


def test_serialized_queue_has_a_real_bound_and_preserves_order():
    async def scenario():
        io = SerializedPersistence("bounded-test", capacity=2, admission_timeout=.02)
        release = threading.Event()
        order, dropped = [], []
        def first():
            release.wait(2)
            order.append(1)
        one = asyncio.create_task(io.call(first))
        await asyncio.sleep(.01)
        two = asyncio.create_task(io.call(lambda: order.append(2)))
        await asyncio.sleep(.01)
        assert io.pending == 2
        assert not io.record(lambda: order.append(3), on_drop=lambda: dropped.append(True))
        with pytest.raises(PersistenceBusy):
            await io.call(lambda: order.append(4))
        assert dropped == [True]
        release.set()
        await asyncio.gather(one, two)
        await io.close()
        assert order == [1, 2]
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["store", "budget"])
def test_sqlite_lock_wait_does_not_block_event_loop(tmp_path, kind):
    async def scenario():
        if kind == "store":
            value = Store(tmp_path)
            run = value.create("fixture")
            io = SerializedPersistence("sqlite-test")
            operation = lambda: io.call(value.event, run["run_id"], "heartbeat", {})
        else:
            value = Budget(tmp_path / "budget.sqlite3", 10)
            io = value.io
            operation = lambda: value.areserve("claude-sonnet-5", "fixture", 100, 100)
        ready, release = threading.Event(), threading.Event()
        def writer():
            with sqlite3.connect(value.path) as db:
                db.execute("BEGIN IMMEDIATE")
                ready.set()
                release.wait(3)
                db.rollback()
        thread = threading.Thread(target=writer)
        thread.start()
        await asyncio.to_thread(ready.wait, 1)
        pending = asyncio.create_task(operation())
        started = time.monotonic()
        await asyncio.sleep(.03)
        assert time.monotonic() - started < .2
        assert not pending.done()
        release.set()
        await pending
        await asyncio.to_thread(thread.join)
        await io.close()
        if kind == "store":
            value.close()
    asyncio.run(scenario())


def test_agent_live_owner_refused_expired_owner_fenced_and_only_old_work_recovered(tmp_path):
    clock = [1000.0]
    first, second = Store(tmp_path), Store(tmp_path)
    first.acquire_owner(ttl=10, clock=lambda: clock[0])
    old = first.create("uncertain paid work")
    first.update(old["run_id"], status="running", stage="writing_answer", graph_answer="Retained prefix")
    budget = Budget(tmp_path / "budget.sqlite3", 10)
    budget.reserve("claude-sonnet-5", "synthesis", 100, 100)
    reserved = budget.snapshot()["reserved_usd"]
    with pytest.raises(OwnerAlive):
        second.acquire_owner(ttl=10, clock=lambda: clock[0])
    assert first.get(old["run_id"])["status"] == "running"
    clock[0] += 11
    second.acquire_owner(ttl=10, clock=lambda: clock[0])
    current = second.create("new owner work")
    recovered = second.interrupt_active(recovery=True)
    assert recovered == [old["run_id"]]
    assert second.get(current["run_id"])["status"] == "planning"
    assert second.get(old["run_id"])["graph_answer"] == "Retained prefix"
    assert budget.snapshot()["reserved_usd"] == reserved
    for operation in (lambda: first.update(old["run_id"], graph_answer="stale overwrite"),
                      lambda: first.event(old["run_id"], "late_delta", {}), first.renew_owner):
        with pytest.raises(OwnerLost):
            operation()
    first.release_owner()  # Stale release cannot release the new owner's lease.
    second.renew_owner()
    second.release_owner()
    first.close(); second.close()


def test_clean_restart_preserves_review_confirmation_and_monotonic_replay(tmp_path):
    first = Store(tmp_path)
    first.acquire_owner()
    run = first.create("reviewed fixture")
    first.update(run["run_id"], status="awaiting_confirmation", stage="awaiting_confirmation", plan={"steps": []})
    before = first.event(run["run_id"], "plan_ready", {})
    first.interrupt_active()
    first.release_owner(); first.close()
    second = Store(tmp_path)
    second.acquire_owner()
    assert second.interrupt_active(recovery=True) == []
    assert second.confirm(run["run_id"])
    assert not second.confirm(run["run_id"])
    after = second.event(run["run_id"], "progress", {})
    assert after["sequence"] == before["sequence"] + 1
    assert second.events_after(run["run_id"], before["sequence"]) == [after]
    second.release_owner(); second.close()


def test_results_lease_fences_old_payload_and_preserves_completed_components(tmp_path):
    clock = [1000.0]
    first, second = ResultStore(tmp_path), ResultStore(tmp_path)
    first.acquire_owner(ttl=10, clock=lambda: clock[0])
    old, _ = first.create({"question": "fixture"}, {"input": 1})
    first.update(old["result_id"], status="ready", component_status={"graph": "available", "layout": "available"})
    with pytest.raises(OwnerAlive):
        second.acquire_owner(ttl=10, clock=lambda: clock[0])
    clock[0] += 11
    second.acquire_owner(ttl=10, clock=lambda: clock[0])
    current, _ = second.create({"question": "new"}, {"input": 2})
    second.interrupt(recovery=True)
    recovered = second.get(old["result_id"])
    assert recovered["component_status"]["graph"] == "available"
    assert recovered["component_status"]["answer"] == "interrupted"
    assert second.get(current["result_id"])["status"] == "preparing"
    with pytest.raises(OwnerLost):
        first.update(old["result_id"], answer="late")
    first.release_owner()
    second.renew_owner(); second.release_owner()


def test_cancelled_waiter_does_not_reorder_or_discard_accepted_transaction(tmp_path):
    async def scenario():
        io = SerializedPersistence("cancel-test")
        release = threading.Event()
        order = []
        def write():
            release.wait(2)
            order.append("accepted")
        task = asyncio.create_task(io.call(write))
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        cleanup = asyncio.create_task(io.call(lambda: order.append("cleanup")))
        release.set()
        await cleanup
        await io.close()
        assert order == ["accepted", "cleanup"]
    asyncio.run(scenario())


def test_runtime_health_stays_responsive_during_writer_lock_and_second_start_refused(tmp_path):
    from test_runtime import service
    async def scenario():
        async with service(tmp_path) as (client, runtime, gateway, graph, literature):
            await runtime.health.refresh(online=True)
            run = await runtime.io.call(runtime.store.create, "fixture")
            with pytest.raises(OwnerAlive):
                async with service(tmp_path):
                    pytest.fail("second owner must not start")
            assert runtime.store.get(run["run_id"])["status"] == "planning"
            # Fail the disk hook if a health read unexpectedly tries to touch it.
            original = runtime.store.probe
            runtime.store.probe = lambda: (_ for _ in ()).throw(AssertionError("health touched disk"))
            ready, release = threading.Event(), threading.Event()
            def lock_writer():
                with sqlite3.connect(runtime.store.path) as db:
                    db.execute("BEGIN IMMEDIATE"); ready.set(); release.wait(2); db.rollback()
            worker = threading.Thread(target=lock_writer); worker.start()
            await asyncio.to_thread(ready.wait, 1)
            pending = asyncio.create_task(runtime.io.call(runtime.store.event, run["run_id"], "fixture", {}))
            started = time.monotonic()
            for path in ("/health/live", "/health/ready", "/health/components", "/metrics"):
                assert (await client.get(path)).status_code == 200
            assert time.monotonic() - started < .2
            assert not pending.done()
            release.set(); await pending; await asyncio.to_thread(worker.join)
            runtime.store.probe = original
            assert gateway.plans == gateway.syntheses == graph.calls == literature.calls == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['create', 'revise', 'confirm'])
def test_disconnect_after_committed_admission_still_launches_once(tmp_path, operation):
    from test_runtime import service, new_plan, wait_state
    async def scenario():
        async with service(tmp_path) as (client, runtime, gateway, graph, _):
            original_run = await new_plan(client) if operation != 'create' else None
            committed, release = asyncio.Event(), asyncio.Event()
            original_call = runtime.io.call
            captured = {}
            async def injected(function, *args, **kwargs):
                value = await original_call(function, *args, **kwargs)
                if function == getattr(runtime.store, operation):
                    captured['run'] = value[1] if operation == 'revise' else original_run if operation == 'confirm' else value
                    committed.set()
                    await release.wait()
                return value
            runtime.io.call = injected
            url = '/v2/plans' if operation == 'create' else f"/v2/plans/{original_run['plan_id']}/{operation}"
            request = asyncio.create_task(client.post(url, json={} if operation == 'confirm' else {'question': 'Which cell types express INS?'}))
            await asyncio.wait_for(committed.wait(), 1)
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            assert len(runtime.admission.tasks) == 1
            release.set()
            run = captured['run']
            await wait_state(client, run['run_id'], {'completed'} if operation == 'confirm' else {'awaiting_confirmation'})
            runtime.io.call = original_call
            if operation == 'confirm':
                assert gateway.syntheses == 1
                assert (await client.post(f"/v2/plans/{run['plan_id']}/confirm")).status_code == 202
                assert gateway.syntheses == 1
            else:
                assert gateway.plans == (2 if operation == 'revise' else 1)
            events = await runtime.io.call(runtime.store.events_after, run['run_id'], 0)
            assert len({event['sequence'] for event in events}) == len(events)
            assert any(event['type'] == 'progress' and event['payload'].get('stage') == 'queued' for event in events)
    asyncio.run(scenario())
