"""Saved plot upgrades must not rerun scientific retrieval or synthesis."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pankgraph_results.app import ResultsRuntime
from pankgraph_results import functional


def runtime(source=None):
    obj = object.__new__(ResultsRuntime)
    payload = {'status': 'ready', 'answer': 'Keep this answer', 'rows': [1, 2]}
    obj.store = SimpleNamespace(
        owner=None, source=lambda rid: source or {'template_id': 'functional_traces', 'parameters': {'age_min': '17'}},
        get=lambda rid: copy.deepcopy(payload))
    obj.io = SimpleNamespace(call=lambda function, *args, **kwargs: asyncio.to_thread(function, *args, **kwargs))
    obj.plot_refresh_tasks = {}
    obj.shutting_down = False
    async def update(rid, **changes):
        payload.update(changes)
    async def resolve(rid, evidence):
        await asyncio.sleep(0.01)
        payload['functional_plot_version'] = functional.VERSION
    obj.update = update
    obj.resolve_resources = AsyncMock(side_effect=resolve)
    return obj, payload


def test_refresh_preserves_answer_filters_and_deduplicates():
    async def scenario():
        obj, payload = runtime()
        first, second = await asyncio.gather(*[
            obj.refresh_saved_functional_plot('id', copy.deepcopy(payload)) for _ in range(2)])
        obj.resolve_resources.assert_awaited_once_with('id', {'functional_filters': {'age_min': '17'}})
        assert first['answer'] == second['answer'] == 'Keep this answer'
        assert first['rows'] == [1, 2]
        await obj.refresh_saved_functional_plot('id', first)
        assert obj.resolve_resources.await_count == 1
    asyncio.run(scenario())


def test_failed_refresh_has_cooldown():
    async def scenario():
        obj, payload = runtime()
        obj.resolve_resources = AsyncMock()  # Existing resolver records upstream failure.
        await obj.refresh_saved_functional_plot('id', copy.deepcopy(payload))
        await obj.refresh_saved_functional_plot('id', copy.deepcopy(payload))
        assert obj.resolve_resources.await_count == 1
        assert payload['answer'] == 'Keep this answer'
    asyncio.run(scenario())


def test_nonfunctional_and_running_results_are_untouched():
    async def scenario():
        obj, payload = runtime({'template_id': 'qtl'})
        await obj.refresh_saved_functional_plot('id', payload)
        await obj.refresh_saved_functional_plot('id', {'status': 'preparing'})
        obj.resolve_resources.assert_not_awaited()
    asyncio.run(scenario())
