# Design — `POST /plan/stream` (streaming plan with queue ETA)

**Date:** 2026-06-01
**Status:** approved design → ready for implementation plan

## Context / Problem

`POST /plan/start` runs the planner fan-out (~50 s under load) inside a dispatch thread that blocks on
`with _pipeline_semaphore:` (a `threading.BoundedSemaphore(MAX_CONCURRENT_QUERIES=30)`). The load test
(`loadtest_results/20260601T183859Z/REPORT.md`) showed that above ~30 concurrent users, requests sit in
that queue for minutes (median wait ~340 s at 200 users). Today the client gets **no feedback** during
that wait — the HTTP request just hangs until it completes or the proxy times out.

We want a streaming variant, `POST /plan/stream`, that:
1. keeps the connection alive with heartbeats while the request waits in the queue and while it is
   processing (like `/chat/plan/confirm/stream`), and
2. once the wait becomes non-trivial, tells the user **roughly how long** they'll wait ("service is
   busy — estimated wait ~3 min").

## Goals

- Streaming NDJSON endpoint that survives proxy idle timeouts during BOTH the queue wait and the work.
- A **rough ETA** for the queue wait, surfaced only after the wait exceeds a threshold.
- Same result contract as `/plan/start` (creates + persists a `PlanSession`, returns a `PlanResponse`),
  so the client can go on to `/plan/confirm`.

## Non-goals (explicitly dropped)

- **No "N users in front of you" / exact queue position.** That would require replacing the semaphore
  with a FIFO-ordered admission structure; deemed not worth the complexity/risk. ETA tolerates
  approximation, so aggregate counters suffice.
- **No load-shedding / rejection.** The endpoint always waits; it never returns "too busy, try later."
  (Can be added later if needed.)

## Design

### 1. Instrument the existing gate (additive, no behavior change)

Add a thread-safe `PipelineStats` (new small module `pipeline_stats.py`, or a class near
`_pipeline_semaphore` in `server.py`) holding:
- `capacity` = `MAX_CONCURRENT_QUERIES`
- `active` — slots currently held
- `waiting` — callers currently blocked/polling for a slot
- `service_ema` — exponential moving average of pipeline durations (admit→release), seeded at
  `PLAN_STREAM_SEED_SERVICE_SECONDS` (default **50**, from the load test); EMA factor ~0.2.

**Exact counter primitives** (all under one `threading.Lock`; each does exactly one transition, so no
path double-counts):
- `enter_wait()` → `waiting += 1`
- `leave_wait()` → `waiting -= 1`
- `acquire_active()` → `active += 1`
- `release_active(duration)` → `active -= 1`; fold `duration` into `service_ema`
- `estimate_eta() -> float`

Blocking context manager (drop-in for the 8 `with _pipeline_semaphore:` sites):
```
@contextmanager
def pipeline_slot():
    stats.enter_wait()
    _pipeline_semaphore.acquire()      # blocks
    stats.leave_wait(); stats.acquire_active()
    t0 = monotonic()
    try:
        yield
    finally:
        stats.release_active(monotonic() - t0)
        _pipeline_semaphore.release()
```

Non-blocking primitive for the streaming path:
```
def try_admit() -> bool:               # NOTE: does not touch `waiting`
    if _pipeline_semaphore.acquire(blocking=False):
        stats.acquire_active()
        return True
    return False

def release(duration):
    stats.release_active(duration)
    _pipeline_semaphore.release()
```
The streaming caller manages its own wait-count: one `enter_wait()` before the poll loop, one
`leave_wait()` the instant `try_admit()` succeeds (or on disconnect). So `enter_wait`/`leave_wait` and
`acquire_active`/`release` are each balanced on every path. Blocking behavior at the 8 existing sites is
identical; we just gain live stats that reflect **all** pipeline load (chat + plan + confirm), so the
ETA isn't fooled by traffic on other endpoints.

### 2. ETA formula

```
estimate_eta():
    if active < capacity:           # a slot is free now
        return 0
    return ceil(waiting / capacity) * service_ema
```
Approximate by design (uses current total waiters, not exact position). Presented to the user rounded
to a friendly unit (e.g. "~3 min").

### 3. `POST /plan/stream`

- **Request body:** identical to `PlanStartRequest` — `{question, rigor=true, use_literature=true}`.
- **Response:** `StreamingResponse`, `media_type="application/x-ndjson"`, one JSON object per line.
- **Wire protocol** (events):

  | event | when | data |
  |---|---|---|
  | `heartbeat` | every `TICK` s while waiting, **for the first 60 s of waiting**, and every ~15 s while processing | `{ts}` |
  | `queued` | every `TICK` s while waiting, **only after 60 s elapsed waiting** | `{estimated_wait_seconds, message}` |
  | `processing` | once, when a slot is acquired | `{}` |
  | `result` | on success | full `PlanResponse` (incl. `session_id`) |
  | `error` | on failure | `{status, detail}` |

  Example: short wait →
  ```
  {"event":"heartbeat","ts":...}        (×N, <60s)
  {"event":"processing"}
  {"event":"heartbeat","ts":...}        (during the ~50s work)
  {"event":"result","data":{...}}
  ```
  Long wait →
  ```
  {"event":"heartbeat","ts":...}        (first 60s)
  {"event":"queued","data":{"estimated_wait_seconds":175,"message":"Service is busy — estimated wait ~3 min."}}
  ... (repeats every TICK s until admitted) ...
  {"event":"processing"}
  {"event":"result","data":{...}}
  ```

- **Admission via async try-acquire (does NOT hold a dispatch thread while queued):**
  ```
  async def _gen():
      wait_start = monotonic()
      stats.enter_wait()
      admitted = False
      try:
          # run the quick question-clean off the event loop
          question = await loop.run_in_executor(None, clean_user_question, raw_q)
          while not stats.try_admit():          # non-blocking; counts itself via enter_wait()
              waited = monotonic() - wait_start
              if waited < HEARTBEAT_THRESHOLD:   # 60 s
                  yield line({"event":"heartbeat","ts":time.time()})
              else:
                  eta = stats.estimate_eta()
                  yield line({"event":"queued","data":{
                      "estimated_wait_seconds": round(eta),
                      "message": _busy_message(eta)}})
              await asyncio.sleep(TICK)          # ~5 s
          stats.leave_wait()                     # admitted; try_admit already did acquire_active()
          admitted = True
          yield line({"event":"processing"})
          t0 = monotonic()
          # heavy work in executor, heartbeats every 15s
          fut = loop.run_in_executor(None, run_plan_start, question, use_lit)
          while True:
              try:
                  result = await asyncio.wait_for(asyncio.shield(fut), timeout=15)
                  break
              except asyncio.TimeoutError:
                  yield line({"event":"heartbeat","ts":time.time()})
          resp = _finalize_plan_session(raw_q, question, rigor, result, ...)   # shared helper
          yield line({"event":"result","data": resp.model_dump()})
      except Exception as e:
          yield line({"event":"error","status":500,"detail":str(e)})
      finally:
          if admitted:
              stats.release(monotonic() - t0)
          else:
              stats.leave_wait()                 # disconnect/error while queued
  ```
  Notes:
  - `try_admit()` increments `active` on success, so the slot is held from admission through `release`.
    The heavy `run_plan_start` is called **without** `pipeline_slot()` (no double-acquire).
  - Client disconnect raises `CancelledError`/`GeneratorExit` in `_gen`; the `finally` releases the slot
    or `leave_wait()`s so counters never leak.
  - `clean_user_question` and `run_plan_start` run in the executor so the streaming generator never
    blocks the event loop (and thus never stalls other connections' heartbeats).

### 4. Shared finalize helper (de-dup with `/plan/start`)

Refactor the session-creation tail of `plan_start` (lines ~1113–1160: build `PlanSession`, format
markdown, append chat history, persist under `_sessions_lock`, `_log_plan_event`, build `PlanResponse`)
into `_finalize_plan_session(raw_question, question, rigor, result, start_time) -> PlanResponse`. Both
`/plan/start` and `/plan/stream` call it. `/plan/start` keeps its blocking `with pipeline_slot():`.

### 5. Config (env)

- `PLAN_STREAM_TICK_SECONDS` (default **5**) — poll/emit cadence while waiting.
- `PLAN_STREAM_HEARTBEAT_THRESHOLD_SECONDS` (default **60**) — below this elapsed wait, emit plain
  heartbeats; at/above, emit `queued` + ETA.
- `PLAN_STREAM_SEED_SERVICE_SECONDS` (default **50**) — initial `service_ema` before real samples land.

## Files touched

- `server.py` — add `PipelineStats` + `pipeline_slot()`/`try_admit`/`release`/`estimate_eta`; replace the
  8 `with _pipeline_semaphore:` sites; refactor `_finalize_plan_session`; add `@app.post("/plan/stream")`;
  add endpoint to the `/` index + startup banner.
- (optional) `pipeline_stats.py` — if we keep `PipelineStats` in its own module.
- `tests/` — unit tests for `PipelineStats` counters + `estimate_eta` (pure, no server needed).

## Verification

1. **Unit:** `PipelineStats` — wait/active counter transitions, EMA update, `estimate_eta` (free slot →
   0; full + W waiters → `ceil(W/cap)*ema`).
2. **Manual, no load:** `curl -N -X POST localhost:8001/plan/stream -d '{"question":"..."}'` → expect a
   few `heartbeat` lines, `processing`, then `result` with a `session_id`. No `queued` events (no wait).
3. **Manual, forced wait:** set `MAX_CONCURRENT_QUERIES=1`, fire 3 concurrent `/plan/stream` curls →
   the 2nd/3rd should show `heartbeat` for 60 s, then `queued` events with a non-zero
   `estimated_wait_seconds`, then `processing` → `result`.
4. Confirm counters return to `active=0, waiting=0` after all finish (add a tiny debug log or assert).
5. Confirm a client disconnect mid-wait doesn't leak `waiting` (kill a curl, check the counter).

## Risks / notes

- **try-acquire fairness:** the async poller competes with blocking `pipeline_slot()` acquirers; Python
  semaphores aren't strictly FIFO anyway, so this is acceptable (ETAs are fuzzy). No starvation in
  practice because slots free roughly every `service_ema/capacity` (~1.7 s).
- **No double-acquire:** the streaming path acquires once via `try_admit()` and must NOT wrap
  `run_plan_start` in `pipeline_slot()`.
- **Counter leaks:** every exit path (success, error, disconnect) must `release()` or `leave_wait()` —
  covered by the `finally`.
- Lock in `PipelineStats` is a leaf lock (no nested locks), so it does not interact with the
  `_sessions_lock → session_store` lock order.
