"""Request ordering, shared capacity and cancellation; no external services."""
import asyncio
import unittest

from pankagent_vnext.candidate_policy import CandidateBatch, generation_slots


class CandidatePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_completion_does_not_change_candidate_priority(self):
        first_release, second_done = asyncio.Event(), asyncio.Event()
        called = []

        async def generate(question, n):
            index = len(called)
            called.append((question, n))
            if index == 0:
                await first_release.wait()
                return ["valid primary, empty"]
            second_done.set()
            return ["valid secondary, many nodes"]

        async with CandidateBatch(generate, "scope", 1, count=2) as batch:
            iterator = batch.__aiter__()
            next_result = asyncio.create_task(anext(iterator))
            await asyncio.wait_for(second_done.wait(), 1)
            self.assertFalse(next_result.done())
            first_release.set()
            self.assertEqual((await next_result).candidates, ["valid primary, empty"])
        self.assertEqual([n for _, n in called], [1, 1])

    async def test_early_selection_cancels_unused_generation_and_retains_provenance(self):
        both_started, stop = asyncio.Event(), asyncio.Event()
        calls, cancelled = [], []
        attempts = []

        async def generate(question, n):
            index = len(calls)
            calls.append(index)
            if index == 0:
                await both_started.wait()
                return ["selected"]
            both_started.set()
            try:
                await stop.wait()
            except asyncio.CancelledError:
                cancelled.append(index)
                raise

        async with CandidateBatch(generate, "scope", 1, count=2, attempts=attempts) as batch:
            async for outcome in batch:
                self.assertEqual(outcome.candidates, ["selected"])
                break
        self.assertEqual(cancelled, [1])
        self.assertEqual([item["status"] for item in attempts], ["completed", "cancelled"])
        self.assertTrue(all("elapsed_ms" in item for item in attempts))

    async def test_separate_batches_share_four_permits(self):
        active, peak = 0, 0
        release, two_active = asyncio.Event(), asyncio.Event()

        async def generate(question, n):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 4:
                two_active.set()
            try:
                await release.wait()
                return [question]
            finally:
                active -= 1

        async def consume(name):
            async with CandidateBatch(generate, name, 1, count=2) as batch:
                return [outcome async for outcome in batch]

        one, two = asyncio.create_task(consume("one")), asyncio.create_task(consume("two"))
        await asyncio.wait_for(two_active.wait(), 1)
        self.assertEqual(active, 4)
        release.set()
        result = await asyncio.gather(one, two)
        self.assertEqual(peak, 4)
        self.assertEqual(len(result[0]) + len(result[1]), 4)

    async def test_deadline_includes_waiting_for_shared_capacity(self):
        slots = generation_slots()
        for _ in range(4):
            await slots.acquire()
        calls = []

        async def generate(question, n):
            calls.append(n)
            return ["unused"]

        try:
            async with CandidateBatch(generate, "scope", 1, timeout=.02) as batch:
                outcomes = [outcome async for outcome in batch]
        finally:
            for _ in range(4):
                slots.release()
        self.assertEqual(calls, [])
        self.assertEqual(outcomes[0].attempt["status"], "timeout")
        self.assertIsInstance(outcomes[0].error, TimeoutError)

    async def test_parent_cancellation_drains_children_and_restores_capacity(self):
        started = asyncio.Event()
        calls, cancelled = [], []

        async def generate(question, n):
            index = len(calls)
            calls.append(index)
            if len(calls) == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(index)
                raise

        async def consume():
            async with CandidateBatch(generate, "scope", 1, count=2) as batch:
                return [outcome async for outcome in batch]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sorted(cancelled), [0, 1])
        slots = generation_slots()
        await asyncio.wait_for(slots.acquire(), .1)
        await asyncio.wait_for(slots.acquire(), .1)
        slots.release()
        slots.release()

    async def test_error_metadata_does_not_include_exception_message(self):
        async def generate(question, n):
            raise RuntimeError("secret token and protected query")

        attempts = []
        async with CandidateBatch(generate, "scope", 1, attempts=attempts) as batch:
            outcome = await anext(batch.__aiter__())
        self.assertIsInstance(outcome.error, RuntimeError)
        self.assertEqual(attempts[0]["error_category"], "RuntimeError")
        self.assertNotIn("secret", str(attempts))
        self.assertNotIn("scope", str(attempts))

    async def test_http_timeout_category_is_preserved_without_http_library_dependency(self):
        class ReadTimeout(Exception):
            pass

        async def generate(question, n):
            raise ReadTimeout("upstream private details")

        async with CandidateBatch(generate, "scope", 1) as batch:
            outcome = await anext(batch.__aiter__())
        self.assertEqual(outcome.attempt["status"], "timeout")
        self.assertTrue(outcome.attempt["retryable"])

    def test_escalation_is_always_one_request(self):
        for n, count in [(8, 2), (1, 3), (4, 1), (1, 0)]:
            with self.assertRaises(ValueError):
                CandidateBatch(None, "scope", n, count=count)


class CohortSelectionTests(unittest.TestCase):
    def test_two_initial_requests_are_scoped_to_verified_cohort_metadata(self):
        from types import SimpleNamespace
        from pankagent_vnext.candidate_policy import initial_request_count
        settings=SimpleNamespace(cypher_initial_requests=2,graph_version='release')
        verified={'semantic_registry':{'version':'v','sha256':'digest','graph_release':'release'},'semantic_issues':[]}
        self.assertEqual(initial_request_count(settings,verified),2)
        for step in ({}, {'question':'Find a gene'}, {'semantic_registry':{}},
                     {**verified,'semantic_issues':['ambiguous stage']},
                     {'semantic_registry':{**verified['semantic_registry'],'graph_release':'different'}}):
            self.assertEqual(initial_request_count(settings,step),1)
        settings.cypher_initial_requests=1
        self.assertEqual(initial_request_count(settings,verified),1)
        settings.cypher_initial_requests=2;settings.cypher_initial_scope='all'
        self.assertEqual(initial_request_count(settings,{}),2)
