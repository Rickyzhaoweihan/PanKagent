# Load test — /plan/start (plan-before-confirm)

```
{
  "base_url": "http://localhost:8001",
  "levels": [
    20,
    50,
    100,
    150,
    200
  ],
  "request_timeout_s": 600.0,
  "cooldown_s": 20.0,
  "rigor": true,
  "use_literature": true,
  "dry_run": false,
  "scrub": true,
  "git_commit": "b59a8bc",
  "started_utc": "20260601T183859Z",
  "query_pool_size": 23
}
```

| C | reqs | ok | fail | ok% | rps | wall_s | p50 | p90 | p95 | p99 | max | steps | nonempty% | gpu_util_max | health_max_ms | log_err |
|---|------|----|------|-----|-----|--------|-----|-----|-----|-----|-----|-------|-----------|--------------|---------------|---------|
| 20 | 20 | 20 | 0 | 1.0 | 0.393 | 50.9 | 41303.2 | 43305.6 | 45736.5 | 49833.9 | 50858.2 | 2.1 | 0.85 | 99.0 | 10012.8 | 0 |
| 50 | 50 | 50 | 0 | 1.0 | 0.438 | 114.1 | 103188.4 | 104475.4 | 109018.8 | 113398.8 | 114100.4 | 2.2 | 0.76 | 99.0 | 17500.8 | 0 |
| 100 | 100 | 100 | 0 | 1.0 | 0.52 | 192.4 | 185243.2 | 185270.2 | 185278.2 | 191551.6 | 192378.8 | 2.2 | 0.8 | 99.0 | 10013.0 | 0 |
| 150 | 150 | 150 | 0 | 1.0 | 0.526 | 285.2 | 277700.3 | 277766.9 | 277783.9 | 282001.3 | 285211.0 | 2.2 | 0.78 | 99.0 | 10013.3 | 0 |
| 200 | 200 | 200 | 0 | 1.0 | 0.505 | 396.3 | 391184.1 | 391345.3 | 391371.8 | 393316.7 | 396276.2 | 2.2 | 0.78 | 99.0 | 10013.3 | 5 |

Latency is wall-clock per request **including queue wait** behind the server's `MAX_CONCURRENT_QUERIES=30` cap — rising p95/p99 above C=30 is backpressure, not error.

