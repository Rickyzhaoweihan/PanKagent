"""Bounded serialized database work; asyncio handlers never wait on SQLite locks."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
import threading
import time


class PersistenceBusy(RuntimeError):
    category = "queue_full"


class AdmissionContinuations:
    """Finish accepted commit-to-launch transitions after an HTTP disconnect.

    Waiters remain cancellable before admission; only one admitted continuation
    exists at a time. Shutdown drains it before interrupting owned jobs.
    """
    def __init__(self):
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.closed = False

    async def run(self, function, *args, **kwargs):
        await self.lock.acquire()
        if self.closed:
            self.lock.release()
            raise PersistenceBusy("admission_closed")
        async def complete():
            try:
                return await function(*args, **kwargs)
            finally:
                self.lock.release()
        task = asyncio.create_task(complete())
        self.tasks.add(task)
        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def close(self):
        self.closed = True
        await asyncio.gather(*list(self.tasks), return_exceptions=True)


class SerializedPersistence:
    def __init__(self, name, capacity=64, admission_timeout=2.0):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self.permits = threading.BoundedSemaphore(capacity)
        self.capacity = capacity
        self.admission_timeout = admission_timeout
        self.pending = 0
        self.closed = False
        self.lock = threading.Lock()

    def _submit(self, function, args, kwargs, on_error=None):
        if self.closed or not self.permits.acquire(blocking=False):
            return None
        context = copy_context()
        with self.lock:
            self.pending += 1
        try:
            future = self.executor.submit(context.run, partial(function, *args, **kwargs))
        except BaseException:
            with self.lock:
                self.pending -= 1
            self.permits.release()
            raise
        def finished(done):
            with self.lock:
                self.pending -= 1
            self.permits.release()
            if on_error is not None and (done.cancelled() or done.exception() is not None):
                on_error()
        future.add_done_callback(finished)
        return future

    async def call(self, function, *args, **kwargs):
        deadline = time.monotonic() + self.admission_timeout
        while True:
            future = self._submit(function, args, kwargs)
            if future is not None:
                # Cancellation cannot retract a transaction already accepted by
                # SQLite. Keep its place in the serialized order before cleanup.
                wrapped = asyncio.wrap_future(future)
                # An abandoned caller still leaves an observed completion.
                wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                return await asyncio.shield(wrapped)
            if self.closed or time.monotonic() >= deadline:
                raise PersistenceBusy("persistence_queue_unavailable")
            await asyncio.sleep(0.005)

    def record(self, function, *args, on_drop=None, **kwargs):
        """Bounded best-effort audit hooks cannot block provider streaming."""
        if self._submit(function, args, kwargs, on_error=on_drop) is None:
            if on_drop is not None:
                on_drop()
            return False
        return True

    async def close(self):
        self.closed = True
        # One shutdown waiter only; accepted writes are drained, not discarded.
        await asyncio.to_thread(self.executor.shutdown, wait=True)
