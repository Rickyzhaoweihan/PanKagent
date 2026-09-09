import asyncio

import pytest

from pankagent_vnext.candidate_policy import CandidateBatch, grounded_prompt_variants, generation_slots


@pytest.mark.parametrize('count', [1, 2, 4])
def test_prompt_variants_keep_full_binding_text_and_have_distinct_construction_emphasis(count):
    question = 'MATCH scope only: Gene.id="GCLC"; tissue_id="UBERON_0001264"; nominal_p<=0.01; dep_0 required.'
    prompts = grounded_prompt_variants(question, count)
    assert len(prompts) == count
    assert len({text for _, text in prompts}) == count
    assert all(text.endswith(question) and text.count(question) == 1 for _, text in prompts)


def test_input_limit_reduces_parallelism_without_truncating_filters():
    question = 'x' * 3990
    assert grounded_prompt_variants(question, 4) == [('canonical', question)]


def test_first_completed_valid_empty_does_not_wait_for_slower_large_result():
    async def run():
        started = asyncio.Event()
        cancelled = []
        attempts = []
        async def generate(question, n):
            if question == 'slow':
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(question)
                    raise
            await started.wait()
            return ['valid_empty']
        async with CandidateBatch(generate, 'same-scope', 1, count=2,
                                  prompts=[('path_first','slow'),('filter_first','fast')],
                                  completion_order=True, attempts=attempts) as batch:
            async for outcome in batch:
                assert outcome.candidates == ['valid_empty']
                break  # Caller validated the same requested scope; zero is valid.
        assert cancelled == ['slow']
        assert [x['status'] for x in attempts] == ['cancelled', 'completed']
        assert attempts[1]['prompt_variant'] == 'filter_first'
    asyncio.run(run())


def test_fast_invalid_candidate_does_not_cancel_other_valid_candidate():
    async def run():
        async def generate(question, n):
            if question == 'valid':
                await asyncio.sleep(.01)
            return [question]
        visited = []
        async with CandidateBatch(generate, 'scope', 1, count=2,
                                  prompts=[('a','invalid'),('b','valid')], completion_order=True) as batch:
            async for outcome in batch:
                visited.extend(outcome.candidates)
                if outcome.candidates == ['valid']:
                    break
        assert visited == ['invalid', 'valid']
    asyncio.run(run())


def test_capacity_is_shared_across_four_way_batches_and_all_tasks_are_drained():
    async def run():
        active = peak = 0
        entered = asyncio.Event()
        stop = asyncio.Event()
        attempts = []
        async def generate(question, n):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            if active == 4: entered.set()
            try:
                await stop.wait()
                return [question]
            finally:
                active -= 1
        async def consume():
            async with CandidateBatch(generate, 'scope', 1, count=4, completion_order=True, attempts=attempts) as batch:
                return [outcome async for outcome in batch]
        tasks = [asyncio.create_task(consume()), asyncio.create_task(consume())]
        await asyncio.wait_for(entered.wait(), .5)
        assert active == peak == 4
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert active == 0 and len(attempts) == 8
        assert all(x['status'] == 'cancelled' for x in attempts)
        slots = generation_slots()
        for _ in range(4): await asyncio.wait_for(slots.acquire(), .1)
        for _ in range(4): slots.release()
    asyncio.run(run())


def test_capacity_comparison_uses_distinct_service_loops():
    async def run(capacity):
        slots = generation_slots(capacity)
        assert slots is generation_slots(capacity)
        with pytest.raises(ValueError, match='cannot change'):
            generation_slots(2 if capacity == 4 else 4)
    asyncio.run(run(2))
    asyncio.run(run(4))
