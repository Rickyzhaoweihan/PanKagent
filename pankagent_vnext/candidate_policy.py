"""Bounded, ordered Cypher generation without speculative graph execution.

The optional two-request initial batch is an experiment policy, not a reason to
accept a broader query or prefer a nonempty result. Callers still validate every
candidate. Closing a batch cancels unused HTTP requests; it cannot establish that
the shared generator stopped work that it had already accepted.
"""
import asyncio
import hashlib
import time
from dataclasses import dataclass


POLICY_VERSION = "ordered-cypher-candidates-v2"
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


def generation_slots():
    """All graph adapters in a service event loop share exactly two permits."""
    loop = asyncio.get_running_loop()
    slots = getattr(loop, "_pankagent_generation_slots", None)
    if slots is None:
        slots = asyncio.BoundedSemaphore(2)
        # The semaphore can itself reference its loop after contention. Keeping
        # it on the loop avoids a global registry retaining finished test loops.
        loop._pankagent_generation_slots = slots
    return slots


@dataclass
class GenerationOutcome:
    candidates: list
    attempt: dict
    error: Exception | None = None


class CandidateBatch:
    """Start one or two generations and expose results in request order.

    Use ``async with`` and ``async for``. Early return after a valid query is
    retrieved drains/cancels unused tasks, including when the caller is cancelled.
    The caller owns the append-only provenance list. It never receives candidate
    results ranked by graph size or HTTP completion order.
    """

    def __init__(self, generate, question, n, *, count=1, timeout=30,
                 attempts=None, slots=None):
        if n not in (1, 8) or count not in (1, 2) or n == 8 and count != 1:
            raise ValueError("candidate policy permits one or two n=1 requests and one n=8 request")
        if timeout <= 0:
            raise ValueError("candidate deadline must be positive")
        self.generate, self.question, self.n = generate, question, n
        self.count, self.timeout = count, timeout
        self.attempts = attempts if attempts is not None else []
        self.slots = slots
        self.tasks = []
        self._records = []
        self._entered = False

    async def __aenter__(self):
        if self._entered:
            raise RuntimeError("candidate batch cannot be reused")
        self._entered = True
        self.slots = self.slots if self.slots is not None else generation_slots()
        offset = len(self.attempts)
        for index in range(self.count):
            attempt = {
                "n": self.n, "attempt_index": offset + index,
                "batch_index": index, "batch_size": self.count,
                "policy_version": POLICY_VERSION,
                "request_sha256": hashlib.sha256(self.question.encode()).hexdigest(),
                "status": "queued", "candidate_count": 0,
            }
            self.attempts.append(attempt)
            self._records.append(attempt)
            self.tasks.append(asyncio.create_task(self._request(attempt)))
        return self

    async def _request(self, attempt):
        started = time.monotonic()

        async def call():
            async with self.slots:
                acquired = time.monotonic()
                attempt["queue_ms"] = round((acquired - started) * 1000, 3)
                attempt["status"] = "generating"
                try:
                    candidates = await self.generate(self.question, self.n)
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
        for task in self.tasks:
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
