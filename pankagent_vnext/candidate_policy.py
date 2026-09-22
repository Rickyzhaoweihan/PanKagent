"""Bounded, ordered Cypher generation without speculative graph execution.

The bounded initial batch is an experiment policy, not a reason to
accept a broader query or prefer a nonempty result. Callers still validate every
candidate. Closing a batch cancels unused HTTP requests; it cannot establish that
the shared generator stopped work that it had already accepted.
"""
import asyncio
import hashlib
import time
from dataclasses import dataclass


POLICY_VERSION = "grounded-parallel-cypher-candidates-v3"
_TIMEOUT_NAMES = {"ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"}


def is_generation_timeout(error):
    return isinstance(error, TimeoutError) or type(error).__name__ in _TIMEOUT_NAMES


def retryable_generation_error(error):
    """Retry transient transport faults within the existing escalation budget."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status is not None:
        return status in (502, 503, 504)
    return is_generation_timeout(error) or type(error).__name__ in {
        "ConnectError", "ReadError", "WriteError", "RemoteProtocolError",
    }


def generation_slots(capacity=4):
    """All graph adapters in a service event loop share the configured permits."""
    if capacity not in (1, 2, 4):
        raise ValueError('GPU generation capacity must be one, two or four')
    loop = asyncio.get_running_loop()
    slots = getattr(loop, "_pankagent_generation_slots", None)
    if slots is None:
        slots = asyncio.BoundedSemaphore(capacity)
        # The semaphore can itself reference its loop after contention. Keeping
        # it on the loop avoids a global registry retaining finished test loops.
        loop._pankagent_generation_slots = slots
        loop._pankagent_generation_capacity = capacity
    elif getattr(loop, '_pankagent_generation_capacity', capacity) != capacity:
        raise ValueError('GPU generation capacity cannot change within a running service loop')
    return slots


@dataclass
class GenerationOutcome:
    candidates: list
    attempt: dict
    error: Exception | None = None


class CandidateBatch:
    """Start bounded generation, optionally consuming completed prompts first.

    Use ``async with`` and ``async for``. Early return after a valid query is
    retrieved drains/cancels unused tasks, including when the caller is cancelled.
    The caller owns the append-only provenance list. It never receives candidate
    results ranked by graph size. Completion order is explicit for grounded
    equivalent-scope prompts and never bypasses the caller's validation.
    """

    def __init__(self, generate, question, n, *, count=1, timeout=30,
                 attempts=None, slots=None, prompts=None, completion_order=False,
                 capacity=4, route=None):
        if n not in (1, 8) or count not in (1, 2, 4) or n == 8 and count != 1:
            raise ValueError("candidate policy permits one, two or four n=1 requests and one n=8 request")
        if timeout <= 0:
            raise ValueError("candidate deadline must be positive")
        self.generate, self.question, self.n = generate, question, n
        self.count, self.timeout = count, timeout
        self.attempts = attempts if attempts is not None else []
        self.slots = slots
        self.capacity, self.route = capacity, route
        self.prompts = prompts if prompts is not None else [('ordered_' + str(i), question) for i in range(count)]
        if len(self.prompts) != count or any(not isinstance(text, str) or not text for _, text in self.prompts):
            raise ValueError('candidate prompts must match batch count')
        self.completion_order = completion_order
        self.tasks = []
        self._records = []
        self._entered = False

    async def __aenter__(self):
        if self._entered:
            raise RuntimeError("candidate batch cannot be reused")
        self._entered = True
        self.slots = self.slots if self.slots is not None else generation_slots(self.capacity)
        offset = len(self.attempts)
        for index in range(self.count):
            variant, question = self.prompts[index]
            attempt = {
                "n": self.n, "attempt_index": offset + index,
                "batch_index": index, "batch_size": self.count,
                "policy_version": POLICY_VERSION,
                "request_sha256": hashlib.sha256(question.encode()).hexdigest(),
                "prompt_variant": variant,
                "status": "queued", "candidate_count": 0,
            }
            if self.route:
                attempt['route'] = self.route
            self.attempts.append(attempt)
            self._records.append(attempt)
            self.tasks.append(asyncio.create_task(self._request(attempt, question)))
        return self

    async def _request(self, attempt, question):
        started = time.monotonic()

        async def call():
            async with self.slots:
                acquired = time.monotonic()
                attempt["queue_ms"] = round((acquired - started) * 1000, 3)
                attempt["status"] = "generating"
                try:
                    candidates = await self.generate(question, self.n)
                    attempt.update(candidate_count=len(candidates),
                                   reported_identity=getattr(candidates, "metadata", {}),
                                   status="completed")
                    return GenerationOutcome(candidates, attempt)
                finally:
                    attempt["generation_ms"] = round((time.monotonic() - acquired) * 1000, 3)

        try:
            return await asyncio.wait_for(call(), timeout=self.timeout)
        except asyncio.CancelledError:
            attempt["status"] = "cancelled"
            raise
        except Exception as exc:
            attempt["status"] = "timeout" if is_generation_timeout(exc) else "failed"
            attempt["error_category"] = type(exc).__name__
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(status, int):
                attempt["http_status"] = status
            attempt["retryable"] = retryable_generation_error(exc)
            return GenerationOutcome([], attempt, exc)
        finally:
            attempt["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)

    async def __aiter__(self):
        for task in asyncio.as_completed(self.tasks) if self.completion_order else self.tasks:
            yield await task

    async def __aexit__(self, exc_type, exc, tb):
        for index, task in enumerate(self.tasks):
            if not task.done():
                task.cancel()
                # A queued task can be cancelled before its coroutine starts.
                self._records[index]["status"] = "cancelled"
        await asyncio.gather(*self.tasks, return_exceptions=True)
        return False


def initial_request_count(settings, step):
    maximum = getattr(settings, 'cypher_initial_requests', 1)
    if maximum == 1:
        return 1
    if getattr(settings, 'cypher_initial_scope', 'cohort') == 'all':
        return maximum
    registry = step.get('semantic_registry') or {}
    verified = bool(registry.get('version') and registry.get('sha256')
                    and registry.get('graph_release') == getattr(settings, 'graph_version', None)
                    and not step.get('semantic_issues'))
    return maximum if verified else 1


def grounded_prompt_variants(question, count):
    """Vary construction emphasis while retaining every input byte/constraint."""
    if count not in (1, 2, 4):
        raise ValueError('grounded prompt count must be one, two or four')
    instructions = [
        ('canonical', ''),
        ('path_first', 'Build the verified directed paths first; then apply every required predicate to its verified owner.\n'),
        ('filter_first', 'Place every required predicate on its verified owner first; connect them using only the verified directed paths.\n'),
        ('scope_first', 'Preserve the exact requested evidence scope, then return its complete nodes and relationships using the verified paths.\n'),
    ]
    result = [(identifier, prefix + question) for identifier, prefix in instructions[:count]
              if len(prefix + question) <= 4000]
    # A partially fitting four-way batch becomes a two-way batch; do not drop
    # or shorten the mandatory binding text to fill a concurrency target.
    if len(result) == 3:
        result = result[:2]
    return result
