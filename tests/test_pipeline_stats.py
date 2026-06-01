"""Unit tests for PipelineStats (pure, no server / external deps)."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline_stats import PipelineStats


def test_free_slot_eta_is_zero():
    s = PipelineStats(capacity=30, seed_service_seconds=50)
    # active < capacity -> a slot is free -> no wait
    assert s.estimate_eta() == 0.0
    s.acquire_active()
    assert s.estimate_eta() == 0.0  # 1 < 30, still free


def test_full_eta_uses_ema_and_waiters():
    s = PipelineStats(capacity=30, seed_service_seconds=50)
    for _ in range(30):
        s.acquire_active()           # now full
    # one waiter -> ceil(1/30)=1 wave -> ~1 service time
    s.enter_wait()
    assert s.estimate_eta() == 50.0
    # 60 waiters total -> ceil(60/30)=2 waves -> ~2 service times
    for _ in range(59):
        s.enter_wait()
    assert s.estimate_eta() == 100.0


def test_counter_transitions_balance():
    s = PipelineStats(capacity=2, seed_service_seconds=10)
    s.enter_wait(); s.leave_wait()
    s.acquire_active(); s.acquire_active()
    snap = s.snapshot()
    assert snap["active"] == 2 and snap["waiting"] == 0
    s.release_active(5); s.release_active(5)
    assert s.snapshot()["active"] == 0


def test_counters_never_go_negative():
    s = PipelineStats(capacity=4)
    s.leave_wait()                   # underflow guarded
    s.release_active(0)              # underflow guarded
    snap = s.snapshot()
    assert snap["waiting"] == 0 and snap["active"] == 0


def test_service_ema_updates_toward_samples():
    s = PipelineStats(capacity=1, seed_service_seconds=50, ema_alpha=0.5)
    # active must be >= capacity for estimate_eta to use the EMA
    s.acquire_active()
    s.enter_wait()
    assert s.estimate_eta() == 50.0
    s.release_active(10)             # ema = 0.5*50 + 0.5*10 = 30
    s.acquire_active()               # full again
    assert math.isclose(s.estimate_eta(), 30.0)
    # zero/negative durations are ignored (don't poison the EMA)
    s.release_active(0)
    s.acquire_active()
    assert math.isclose(s.estimate_eta(), 30.0)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
