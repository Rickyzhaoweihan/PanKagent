"""Aggregate, thread-safe stats for the pipeline admission gate.

Tracks how many pipelines are actively running (``active``) and how many callers
are currently waiting for a slot (``waiting``), plus an exponential moving
average of recent pipeline service times. Used to produce a *rough* ETA for a
queued request without tracking exact per-request position.

This is pure bookkeeping — it does NOT own the semaphore. ``server.py`` wires the
counter transitions around the real ``_pipeline_semaphore`` (blocking path via
``pipeline_slot()``; non-blocking path via ``_try_admit()``/``_release_slot()``).
"""
from __future__ import annotations

import math
import threading


class PipelineStats:
    def __init__(self, capacity: int, seed_service_seconds: float = 50.0,
                 ema_alpha: float = 0.2):
        self.capacity = max(1, int(capacity))
        self._ema_alpha = ema_alpha
        self._lock = threading.Lock()
        self._active = 0
        self._waiting = 0
        # Seeded so the very first queued caller still gets a sane ETA before any
        # real pipeline has completed to feed the EMA.
        self._service_ema = max(0.1, float(seed_service_seconds))

    # -- wait-counter (a caller blocked/polling for a slot) -------------------
    def enter_wait(self) -> None:
        with self._lock:
            self._waiting += 1

    def leave_wait(self) -> None:
        with self._lock:
            self._waiting = max(0, self._waiting - 1)

    # -- active-counter (a caller holding a slot) -----------------------------
    def acquire_active(self) -> None:
        with self._lock:
            self._active += 1

    def release_active(self, duration: float) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            if duration and duration > 0:
                a = self._ema_alpha
                self._service_ema = (1.0 - a) * self._service_ema + a * float(duration)

    # -- estimate -------------------------------------------------------------
    def estimate_eta(self) -> float:
        """Rough seconds until a queued caller gets a slot.

        ``0`` if a slot is free right now; otherwise
        ``ceil(waiting / capacity) * service_ema``. Approximate by design — it
        uses the current total number of waiters, not the caller's exact
        position (we deliberately don't track FIFO order).
        """
        with self._lock:
            if self._active < self.capacity:
                return 0.0
            waves = math.ceil(self._waiting / self.capacity) if self._waiting > 0 else 1
            return waves * self._service_ema

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "active": self._active,
                "waiting": self._waiting,
                "capacity": self.capacity,
                "service_ema": round(self._service_ema, 1),
            }
