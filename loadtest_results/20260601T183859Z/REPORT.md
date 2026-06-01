# Pressure Test Report — `/plan/start` (plan-before-confirm)

**PanKgraph AI Assistant — concurrent-load characterization**

| | |
|---|---|
| **Run ID** | `20260601T183859Z` |
| **Date (UTC)** | 2026-06-01 18:38:59Z → ~19:05Z (~26 min wall) |
| **Target** | `http://localhost:8001` (live server, PID 894330, up ~1.7 days at start) |
| **Git commit** | `b59a8bc` (gpu-fleet-and-concurrency) |
| **Harness** | `loadtest_plan_start.py` (async `httpx`, single-burst-per-level) |
| **Endpoint under test** | `POST /plan/start` **only** — `/plan/confirm` and `/plan/revise` were never called |
| **Concurrency gradient** | 20 → 50 → 100 → 150 → 200 (single burst each, 20 s cooldown between) |
| **Total requests** | 520 |
| **Outcome** | **520/520 succeeded (100%)**; throughput ceiling ~0.5 req/s; latency grows linearly above the 30-concurrency cap |
| **Test footprint** | Fully scrubbed — persistent stores returned to exact baseline |

---

## 1. Executive Summary

The service was driven from 20 to 200 simultaneous "users," each issuing one `/plan/start`
request (the planner fan-out: question-clean → 5 candidate plans → translate → execute
Cypher/SQL against Neo4j/PostgreSQL → return plan for review, **without** the confirm-stage
Sonnet format/reasoning pipeline).

**Headline findings:**

1. **Zero failures at every level.** All 520 requests returned a valid, parseable plan — no HTTP
   errors, no timeouts (within the 600 s budget), no error responses. The server degrades by
   **queueing, never dropping**.
2. **Throughput is capped at ~0.5 requests/second** and is flat from C=100 upward. Adding more
   concurrent users buys **no additional throughput** — only longer queues.
3. **Latency scales linearly with concurrency above the cap** — a textbook queueing curve.
   Median time-to-plan: **41 s @ C=20 → 391 s @ C=200**. A good predictive model is
   **W ≈ 2 × C seconds** for C ≥ 30.
4. **The throughput bottleneck IS the GPU fleet — all 5 GPUs were compute-saturated during active
   load.** Router logs show the weighted balancer held every backend at equal relative load
   (`inflight/weight ≈ 7`), and the one GPU we could directly measure (local L40S) ran a **bimodal
   99%-or-0% utilization — never partial**: pegged whenever there was work, idle only between waves.
   Since the *lowest-allocated* GPU was saturated-when-working and load was balanced, **all five were
   saturated**. GPU work is amplified by **5 candidates × ~11.6 vLLM calls/request** (the cypher
   refinement loop). The 30-slot `_pipeline_semaphore` sits *on top* of the saturated fleet and
   shapes the latency curve (knee at C≈30), but the fleet is the throughput gate.
   *(Note on metrics: an earlier draft mis-stated the fleet as "~30% utilized" by using
   `max_num_seqs`=160 as the denominator. That is wrong — an L40S compute-saturates at ~7 concurrent
   sequences, far below 32, so sequence-slot occupancy is not a utilization measure. See §9.1/§10.)*
5. **Plan quality held up under load** — the planner kept routing chain-shaped questions to
   `chain` plans and the rest to `parallel`, with realistic record volumes; no collapse to garbage.
6. **Server responsiveness to even `GET /health` degraded sharply** under peak load (26 of 27
   health probes exceeded 10 s at C=200) — partly a genuine dispatch-saturation signal, partly a
   client-side measurement artifact (see §9 caveats).

**Bottom line:** the service is **robust but capacity-bound** — comfortable at ~30 concurrent
users (~50 s latency), and any spike beyond that is absorbed safely but with minute-scale waits.

---

## 2. Methodology

### 2.1 What was tested (and what was not)

- **Tested:** `POST /plan/start` — the "plan before confirm" path. Server-side this runs the full
  planner fan-out and **executes** the generated Cypher/SQL against Neo4j/PostgreSQL (it fills the
  per-step record counts shown in `plan_markdown`), then returns the plan for review.
- **Not tested:** `/plan/confirm` (the Sonnet rigor format/reasoning pipeline + answer cache) and
  `/plan/revise`. The harness source contains no reference to those endpoints. This was a
  deliberate scoping decision — the goal was to pressure the planning + retrieval path in isolation.

### 2.2 Endpoint contract

- **Request** (`PlanStartRequest`): `{"question": str, "rigor": true, "use_literature": true}`.
- **Response** (`PlanResponse`): `{session_id, plan_markdown, plan_json, use_literature, error}`.
  `plan_json` carries `{plan_type, interpreted_question, reasoning, steps[]}`. The retrieved data
  is **not** returned inline; per-step record counts are surfaced in `plan_markdown` as
  `**N records**`, which the harness parses for the "records / non-empty" signal.

### 2.3 Load model

**Single burst per level:** for each C in {20, 50, 100, 150, 200}, fire C requests simultaneously
(C connections open at once via `httpx.Limits(max_connections=C+8)`), await all, record, cool down
20 s, advance. This gives clean "C concurrent users" semantics and makes each level an independent
measurement. Per-request timeout was 600 s — deliberately generous, because at high C the tail
requests legitimately wait minutes in the queue, and that wait **is the signal** (not a failure).

### 2.4 Query pool

23 real questions (from the experience buffer, rollout logs, and the query-planner prompt examples),
tagged by the code path they exercise and drawn round-robin so every level gets an even category mix:

| Category | # in pool | Exercises |
|---|---|---|
| `simple_kg` | 4 | single-entity KG lookups |
| `parallel_kg` | 4 | independent multi-hop KG merged in parallel |
| `chain_kg` | 4 | sequential entity-ID-flow chains |
| `cross_source_genomic` | 3 | PostgreSQL genomic coordinates + KG |
| `functional_data` | 2 | REST functional-data steps |
| `donor` | 2 | donor-node clinical metadata |
| `literature` | 2 | literature-leaning biological questions |
| `empty_result` | 2 | deliberate no-data (nonexistent SNP, sparse fGSEA) |

### 2.5 Server-side sampling

During every level, background tasks sampled (~5 s cadence): GPU via `nvidia-smi`
(util / mem / power), `GET /health` latency, and the `server.log` byte range appended during the
level (scanned for `ERROR`/`Traceback`/`Exception`).

### 2.6 Data hygiene (no-logging requirement)

The synthetic traffic was kept out of persistent state. The harness recorded every `session_id`
it created and, on completion, scrubbed exactly those rows from `logs/sessions.sqlite`
(`plan_sessions` + `events`) and filtered them out of `logs/plan_sessions.jsonl`. The experience
buffer / `query_log.jsonl` and the answer/literature caches are **confirm-only** and were never
touched. `server.log` was left intact (it is the live process's stdout fd; rewriting it would
corrupt the running server, and it is a transient, frequently-rotated log).

**Verification:** baseline before = 434 `plan_sessions` / 1824 `events` / 2119 jsonl lines;
after scrub = **434 / 1824 / 2119** (exact). Scrub removed 520 `plan_sessions`, 520 `events`,
520 jsonl lines; post-scrub verify confirmed **0** test rows remaining.

---

## 3. Environment & Server Configuration

| Parameter | Value | Source |
|---|---|---|
| `MAX_CONCURRENT_QUERIES` | 30 | `server.py` default — bounds `_pipeline_semaphore` |
| `DISPATCH_THREADS` | 46 (30 + 16) | async→sync offload pool |
| `PLAN_START_CANDIDATES` | 5 | per-request planner fan-out |
| Per-request GPU fan-out | ~5 vLLM cypher generations (+ Claude orchestration calls) | one per candidate |
| Per-request DB fan-out | ~15–50 Neo4j/PostgreSQL queries | 5 candidates × 3–10 each |
| vLLM | `cypher-writer`, `max_num_seqs=32`, `gpu_mem_util=0.9` | GPU fleet (router on :8010) |
| Neo4j | ADA KG, `bolt://localhost:8687`, 5.4M nodes | |
| PostgreSQL | `pankgraph` db, 4 entity tables | |

Each `/plan/start` therefore holds one of 30 pipeline slots for its full duration (~50 s under
load) while fanning out to ~5 GPU calls and dozens of DB queries.

---

## 4. Results — Headline Table

| C (users) | reqs | ok | fail | ok % | rps | wall (s) | p50 (s) | p90 (s) | p95 (s) | p99 (s) | max (s) | mean steps | non-empty % | server_log "err" |
|----:|----:|----:|----:|----:|----:|----:|----:|----:|----:|----:|----:|----:|----:|----:|
| 20  | 20  | 20  | 0 | 100 | 0.39 | 50.9  | 41.3  | 43.3  | 45.7  | 49.8  | 50.9  | 2.1 | 85 | 0 |
| 50  | 50  | 50  | 0 | 100 | 0.44 | 114.1 | 103.2 | 104.5 | 109.0 | 113.4 | 114.1 | 2.2 | 76 | 0 |
| 100 | 100 | 100 | 0 | 100 | 0.52 | 192.4 | 185.2 | 185.3 | 185.3 | 191.6 | 192.4 | 2.2 | 80 | 0 |
| 150 | 150 | 150 | 0 | 100 | 0.53 | 285.2 | 277.7 | 277.8 | 277.8 | 282.0 | 285.2 | 2.2 | 78 | 0 |
| 200 | 200 | 200 | 0 | 100 | 0.51 | 396.3 | 391.2 | 391.3 | 391.4 | 393.3 | 396.3 | 2.2 | 78 | 5† |

† The 5 "errors" at C=200 are **benign** — see §8.

---

## 5. Latency Analysis

### 5.1 Distribution per level (seconds)

| C | min | p25 | p50 | p75 | p95 | max | stdev |
|----:|----:|----:|----:|----:|----:|----:|----:|
| 20  | 41  | 41  | 41  | 41  | 46  | 51  | 2.2 |
| 50  | 103 | 103 | 103 | 103 | 109 | 114 | 2.3 |
| 100 | 185 | 185 | 185 | 185 | 185 | 192 | 1.1 |
| 150 | 278 | 278 | 278 | 278 | 278 | 285 | 0.8 |
| 200 | 391 | 391 | 391 | 391 | 391 | 396 | 0.5 |

The distributions are **extraordinarily tight** (stdev < 2.3 s on medians of hundreds of seconds).
This is the fingerprint of near-perfect FIFO queueing: because every request in a burst starts
together and the server drains them at a fixed rate (30 at a time), they all experience essentially
the same total time. The slight spread is just service-time jitter in the final wave.

### 5.2 Queueing model (why latency is linear)

The data fits a simple closed-form. With a concurrency cap **N = 30** and a per-request service
time **S ≈ 48–57 s** (measured directly at C=20, where all 20 run at once with no queue):

- **Throughput ceiling:** λ_max = N / S ≈ 30 / 57 ≈ **0.53 req/s** — matches the observed plateau.
- **Wait time (Little's Law):** W ≈ C / λ_max ≈ C / 0.5 = **~2 s per concurrent user**.

Predicted vs. observed p50:

| C | model (2·C s) | observed p50 |
|----:|----:|----:|
| 20  | 40  | 41  |
| 50  | 100 | 103 |
| 100 | 200 | 185 |
| 150 | 300 | 278 |
| 200 | 400 | 391 |

The model tracks within ~7%. **Interpretation:** above the 30-slot cap, each additional
simultaneous user adds ~2 seconds to *everyone's* wait. The "knee" of the latency curve is at
C ≈ 30 (= `MAX_CONCURRENT_QUERIES`); below it, latency is flat at the service time (~50 s);
above it, latency = queue depth × service-rate.

---

## 6. Throughput Analysis

Effective throughput (completed requests / level wall-time):

```
C=20  → 0.39 req/s     (below the cap: only 20 of 30 slots used)
C=50  → 0.44 req/s
C=100 → 0.52 req/s     ← ceiling reached
C=150 → 0.53 req/s     ← plateau
C=200 → 0.51 req/s     ← plateau
```

Throughput rises until the 30 slots are saturated (somewhere between C=20 and C=100), then flat at
**~0.5 req/s ≈ 30 plans/minute ≈ 1,800 plans/hour**. This is the hard sustained ceiling of the
current single-instance + current-GPU-fleet configuration for the plan-before-confirm path.

---

## 7. Per-Category Behavior (planner correctness under load)

Pooled across all levels (latency is dominated by queue position, so it is identical per category;
the informative signals are **plan type** and **record volume**):

| Category | n | ok | plan types chosen | mean records |
|---|----:|----:|---|----:|
| `chain_kg` | 92 | 92 | chain ×92 | 3,665 |
| `cross_source_genomic` | 68 | 68 | parallel ×46, chain ×22 | 1,490 |
| `parallel_kg` | 92 | 92 | parallel ×92 | 758 |
| `simple_kg` | 92 | 92 | parallel ×92 | 488 |
| `literature` | 44 | 44 | parallel ×44 | 1,659 |
| `functional_data` | 44 | 44 | parallel ×22, chain ×22 | 26 |
| `donor` | 44 | 44 | parallel ×44 | 22 |
| `empty_result` | 44 | 44 | parallel ×44 | **0** (as designed) |

**Key point:** even at 200 concurrent users the planner kept making *correct structural choices* —
every `chain_kg` question produced a `chain` plan, the deliberate `empty_result` questions correctly
returned 0 records, and heavy categories (chain, literature, cross-source) pulled large record sets.
Load did not degrade planning quality.

### Plan-type and step-count distribution (all 520)

- **Plan types:** `parallel` ×384, `chain` ×136.
- **Steps per plan:** 0 → 66, 1 → 136, 2 → 156, 3 → 45, 4 → 87, 5 → 8, 7 → 22.
  (The 66 zero-step plans = the deliberate empty-result queries plus genuine data-gap questions;
  this matches the 76–85% non-empty rate.)

---

## 8. Reliability

**520 / 520 requests returned a valid plan (100%).** No HTTP non-200s, no client timeouts, no
`error` fields, across all five levels including the 200-user burst.

The `server_log "err" = 5` flagged at C=200 are **false positives** from the substring scan: they
are `plan_test_time_result` stream-event lines in which **one of the 5 internal planner candidates**
carried an embedded `"error": "Traceback…"` string. The request still succeeded via the other
candidates — this is the test-time-scaling redundancy working as designed, and is precisely why the
client-observed success rate stayed at 100%. They are **not** request-level failures.

---

## 9. Resource Utilization

### 9.1 GPU fleet — the whole picture (from router logs)

The cypher-writer "fleet" is **5 vLLM instances behind the weighted load-balancer on :8010**
(`gpu_router/load_balancer.py`), only one of which is on this box:

```
ROUTER_BACKENDS = 7000:w2 (H100, remote) , 7001:w2 (H100, remote) ,
                  7002:w1 (L40s, remote) , 7003:w1 (L40s, remote) ,
                  8002:w1 (L40s, LOCAL)
```

The app (`server.py`) sends every Cypher generation to `:8010`, which routes by weighted
least-connections (`inflight / weight`). Reconstructing `gpu_router/logs/router.log` for the test
window (14:38:59–15:06:30 local) gives the **actual per-GPU load** — no estimation:

| Backend | GPU | weight | requests | share | peak inflight / cap (32) |
|---|---|---:|---:|---:|---:|
| 127.0.0.1:7000 | H100 (remote) | 2 | 2,109 | 34.9% | 14 / 32 |
| 127.0.0.1:7001 | H100 (remote) | 2 | 1,915 | 31.7% | 13 / 32 |
| 127.0.0.1:7002 | L40s (remote) | 1 | 714 | 11.8% | 7 / 32 |
| 127.0.0.1:7003 | L40s (remote) | 1 | 664 | 11.0% | 7 / 32 |
| 127.0.0.1:8002 | **L40s (local)** | 1 | 638 | 10.6% | 7 / 32 |
| **fleet total** | | **7** | **6,040** | 100% | **48 / 160 (~30%)** |

**Two facts establish that all five GPUs were saturated during load:**

**(a) The balancer equalized relative load.** Weighted least-connections minimizes `inflight/weight`,
and at peak it held every backend at the same ratio:

| backend | GPU | weight | peak inflight | **inflight / weight** |
|---|---|---:|---:|---:|
| 7000 | H100 | 2 | 14 | 7.0 |
| 7001 | H100 | 2 | 13 | 6.5 |
| 7002 | L40s | 1 | 7 | 7.0 |
| 7003 | L40s | 1 | 7 | 7.0 |
| 8002 | L40s (local) | 1 | 7 | 7.0 |

Load was balanced proportional to weight — the local L40S getting the fewest requests reflects its
low weight, **not** idleness.

**(b) The measured L40S utilization is bimodal — 99% or 0%, never partial.** Per-level util samples
on the local L40S (the 40–90% bucket is empty at every level):

| C | samples | at ≥90% | at 40–90% | at <40% (idle/drain) | mean |
|----:|----:|----:|----:|----:|----:|
| 20  | 9  | 3  | 0 | 6 | 33% |
| 50  | 13 | 5  | 0 | 8 | 38% |
| 100 | 18 | 11 | 0 | 7 | 60% |
| 150 | 24 | 17 | 0 | 7 | 70% |
| 200 | 27 | 20 | 0 | 7 | 73% |

Whenever there was work the L40S was at **99%**; the sub-40% samples are the idle drain at each
level's tail. The "mean 73%" is the time-average of *saturated-during-the-wave* + *idle-between-waves*,
**not** spare capacity.

**Conclusion:** the lowest-allocated GPU was saturated whenever working, and the balancer kept all
five at equal relative load → **the whole fleet was compute-saturated during active processing.**
The three L40S instances (identical hardware, identical inflight 7) were definitively pegged; the two
H100s carried the most traffic (66.6% vs a 57.2% weighted expectation — they clear faster, so attract
more) at inflight 13–14 and were clearing it just fast enough to keep pulling work. No backend ever
went unhealthy.

- **11.6 vLLM calls per `/plan/start`** (6,040 / 520) — far above the nominal 5 candidates: the
  `cypher_validator` refinement loop iterates several times per candidate, multiplying GPU load.
- ⚠️ **Two metric corrections.** (1) The harness `nvidia-smi` saw only the local L40S (index 0,
  ~10% of traffic) — its 99% is representative of the L40S tier but had to be combined with router
  data (above) to characterize the fleet. (2) An earlier draft called the fleet "~30% utilized"
  using 48 of 160 sequence *slots* — **wrong denominator**: an L40S compute-saturates at ~7
  sequences, not its `max_num_seqs`=32 limit, so slot occupancy is not utilization. (Memory is pinned
  at ~41.7 GB / 46 GB per instance by `gpu_memory_utilization=0.9`.)

### 9.2 Server responsiveness (`GET /health`)

| C | probes | succeeded (<10 s) | mean latency (ms) | max (ms) |
|----:|----:|----:|----:|----:|
| 20  | 9  | 7 | 2,911 | 10,013 |
| 50  | 13 | 7 | 5,561 | 17,501 |
| 100 | 18 | 6 | 6,691 | 10,013 |
| 150 | 24 | 6 | 7,663 | 10,013 |
| 200 | 27 | **1** | 9,641 | 10,013 |

`/health` does not acquire the pipeline semaphore, yet its latency climbed steadily and at C=200
only **1 of 27** probes returned within the 10 s sampler timeout. This indicates the server's async
dispatch / event loop is heavily contended at peak — even trivial endpoints starve.

⚠️ **Caveat:** this metric is partly a **client-side artifact**. The sampler coroutine shares one
asyncio event loop with up to 200 in-flight request coroutines; event-loop starvation on the client
inflates the measured `/health` latency (and the 10,013 ms ceiling is the sampler's own 10 s timeout
plus scheduling delay). The *trend* (worse as C rises) is real and directionally meaningful, but the
absolute values overstate pure server-side `/health` latency.

---

## 10. Bottleneck Analysis & Root Cause

Two layers interact:

1. **Throughput gate — the GPU fleet.** Per §9.1, all five GPUs were compute-saturated whenever
   there was load (balanced `inflight/weight ≈ 7`; bimodal 99%/0% util on the measured L40S). GPU
   demand is amplified by **5 candidates × ~11.6 vLLM calls/request** — the cypher-generation +
   refinement loop. This is what fixes the sustained ceiling at ~0.5 req/s.
2. **Latency shaper — the 30-slot cap.** `_pipeline_semaphore = BoundedSemaphore(30)` admits at most
   30 pipelines; the 70/120/170 excess requests at C=100/150/200 block here, which is what makes
   latency linear in C (§5.2). Per-request wall-time (~50 s) also includes Claude orchestration
   (~6 Sonnet calls) and Neo4j (~15–50 queries), but those run *concurrently across* the 30 admitted
   requests, so they shape latency more than the throughput ceiling.

Throughput = (work the saturated fleet can clear) ≈ 0.5 req/s, and W ≈ C / 0.5 (§5.2).

**The binding throughput constraint is GPU cypher-generation capacity, saturated during active load.**
Decisive evidence: the lowest-allocated GPU (local L40S) was at 99% whenever working, and the
balancer held all backends at equal relative load — so the fleet had **no** headroom during a wave.
Consequently **raising `MAX_CONCURRENT_QUERIES` alone would not raise throughput** — it would pile
more inflight onto already-pegged GPUs and lengthen the queue. To lift the ceiling you must reduce
GPU work per request and/or add GPU capacity.

---

## 11. Capacity Recommendations

The fleet is the throughput limiter (saturated during load), so the levers are **reduce GPU work per
request** and/or **add GPU capacity** — raising the admission cap alone does **not** help.

1. **Cut GPU work per request (biggest, cheapest win).**
   - **`PLAN_START_CANDIDATES = 5 → 2–3`.** GPU load scales ~linearly with candidates; halving fan-out
     roughly doubles throughput. A/B the plan-quality impact (the 5-way scoring exists to pick the
     best non-empty plan).
   - **Tighten the cypher refinement loop.** ~11.6 vLLM calls/request is high — the `cypher_validator`
     is iterating several times per candidate. Lowering the max-iteration count / raising the accept
     threshold cuts GPU calls directly.
2. **Add GPU capacity, weighted toward the slow tier.** Three of five GPUs are L40S and the L40S tier
   saturates at ~7 sequences; adding H100s (or more L40s) behind the :8010 router raises the ceiling
   ~linearly. For a 200-users-at-30 s-target SLA (λ ≈ 6.7 req/s ≈ **13× today**) the practical path is
   *fewer candidates × more/faster GPUs* together.
3. **Raise `MAX_CONCURRENT_QUERIES` only *after* (1)/(2).** Once per-request GPU cost drops or the
   fleet grows, the 30-cap becomes the next limit and should be raised to match — but raising it while
   the fleet is saturated only lengthens queues. Also watch Anthropic limits (each request ≈ 6 Sonnet
   calls).
4. **Add load-shedding** — past a max queue depth, return `503`/`Retry-After` instead of letting a
   user wait 6+ minutes (median 391 s at C=200). Protects perceived reliability with an honest signal.
5. **Current safe operating point:** ~**30 concurrent users** at ~50–60 s latency (the knee). Plan
   autoscaling/shedding around here until fan-out and fleet size are retuned.

---

## 12. Caveats & Limitations

- **Harness GPU sampling = single visible device** (the local L40S, index 0) — ~10% of fleet
  traffic. The fleet-wide truth was recovered post-hoc from `router.log` (§9.1); future runs should
  poll the router's `/health` (per-backend `inflight`) instead of local `nvidia-smi`.
- **`/health` latency inflated** by client event-loop contention (§9.2); directional, not absolute.
- **`server_log` error scan** is a substring match (`ERROR`/`Traceback`/`Exception`) and counted
  benign in-payload candidate errors (§8); it is a coarse signal.
- **Single-burst model** measures independent "C concurrent users" snapshots with cooldowns — it
  does **not** model sustained arrival rates, ramp behavior, or mixed confirm traffic.
- **Cold-vs-warm:** the server had been up ~1.7 days; no cold-start effects were measured.
- **No confirm stage** was exercised; real end-to-end user latency (plan → confirm → answer) is
  higher than these plan-only numbers.

---

## 13. Reproduction & Artifacts

**Re-run:**
```bash
python3 loadtest_plan_start.py                 # full gradient 20,50,100,150,200 (auto-scrub)
python3 loadtest_plan_start.py --dry-run       # quick validation (levels 2,5)
python3 loadtest_plan_start.py --levels 10,30  # custom gradient
python3 loadtest_plan_start.py --no-scrub      # keep the test footprint
python3 loadtest_plan_start.py --scrub-only loadtest_results/<run-id>   # re-run cleanup
```

**Artifacts** (`loadtest_results/20260601T183859Z/`):

| File | Contents |
|---|---|
| `summary.md` / `summary.csv` | per-level headline metrics |
| `requests.jsonl` | one line per request (level, query, category, latency, status, plan_type, n_steps, total_records, session_id) |
| `gpu_samples.csv` | GPU time series (ts, level, util, mem, power) |
| `health_samples.csv` | `/health` probe time series |
| `REPORT.md` | this report |

---

## 14. Conclusion

The plan-before-confirm path is **operationally robust** — it withstood a 10× concurrency sweep up
to 200 simultaneous users with **zero failures**, degrading gracefully via FIFO queueing rather
than errors. It is **firmly capacity-bound at ~0.5 req/s**, and the limiter is the **GPU
cypher-generation fleet**: router logs show all five backends held at equal relative load and the
measured L40S pegged at 99% whenever working, so the fleet was compute-saturated during every wave
(no headroom). GPU demand is amplified by 5 candidates × ~11.6 vLLM calls per request. The 30-slot
pipeline cap sits on top and shapes the latency curve (+~2 s per concurrent user beyond the cap;
391 s median at C=200), but it is not the throughput gate. The highest-leverage fixes therefore
**reduce GPU work per request** (cut candidate fan-out 5→2–3; tighten the cypher refinement loop)
and/or **add GPU capacity**; raising `MAX_CONCURRENT_QUERIES` helps only after those, and adding
GPUs alone (without trimming fan-out) is the expensive path. The current comfortable operating point
is **~30 concurrent users at ~50 s latency**.
