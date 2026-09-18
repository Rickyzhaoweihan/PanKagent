# Independent PanKgraph health dashboard

The collector runs independently. Its supervisor manages only its own process.
The unified demo release adds a fixed results proxy and frontend navigation link;
the collector never restarts agent, results, Cypher, databases or production.

## Run and access

Use the verified frozen demo virtualenv (FastAPI, uvicorn, httpx already installed).
Run as serviceuser from a dedicated release containing `pankgraph_health/`:

```sh
python -m pankgraph_health.supervisor start
python -m pankgraph_health.supervisor status
python -m pankgraph_health.supervisor stop
```

Default bind: **127.0.0.1:8796**. UI:
`/pankgraph-vnext/health-dashboard/`. The service uses existing results Basic
credentials; agent operator credentials are read only by the collector from the
protected agent environment. Optional frontend delivery credentials are kept in
owner-only `PANK_HEALTH_STATE_DIR/frontend-auth.json`, containing a single
`authorization` field with the existing demo Basic header. Never commit this file.
The credential is used only for the fixed results frontend origin.

Defaults: `/var/local/serviceuser/.config/{pankagent-vnext,pankgraph-results}/runtime.env`
and `/var/local/serviceuser/.local/state/pankgraph-health`. Only these environment
path overrides exist: `PANK_HEALTH_AGENT_ENV`, `PANK_HEALTH_RESULTS_ENV`,
`PANK_HEALTH_STATE_DIR`. No request accepts an upstream URL. Tokens/hashes stay
out of API output, browser assets, history and logs.

Normal access is the frontend's **System health** link at
`http://127.0.0.1:18795/pankgraph-vnext/health-dashboard/`. The results service
proxies a fixed set of GET routes to the collector with existing demo authentication.

Independent access can use a separate SSH forward 18797 → remote8796 and the existing
`deploy_results/local_demo_proxy.py` with `--port 18798 --upstream-port 18797`
and the existing protected demo access file. Open
`http://127.0.0.1:18798/pankgraph-vnext/health-dashboard/`.
Keep the proxy and tunnel running. No public nginx route is activated.

## Contract and coverage

- Read-only `api/snapshot`, `api/history?hours=1..168`, `api/incidents`,
  `api/metrics`. All detailed routes require Basic authentication.
- `GET /health/live` is available to a direct loopback supervisor only and
  reports collector freshness. Forwarded callers need normal authentication.
- Every30s: existing agent/results live/ready/components/metrics, Cypher gateway
  and two existing replica health routes, functional API health, actual frontend
  HTML delivery. The main JS asset is checked on change and every5min.
- Agent component snapshots cover Neo4j, Claude model access, HIRN, provider
  status, queue/storage/budget. Separate actual-operation timestamps preserve
  the distinction between API access and completed inference.
- Existing result observations cover layout, synthesis, queries, resources,
  source observations and plots. Idle components may be unknown; monitoring does
  not execute scientific queries, regenerate graphs, download source data,
  or run paid model canaries to manufacture a fresh success.
- A missing/malformed/unauthorized/unsupported upstream response is not healthy.
  Responses are size bounded; HTTP calls time out after5s, cycles after20s.
  Collection runs independently from API reads; no overlapping collection loops.
- History retains at most7days/20,160 compact samples. Incident history is bounded
  to7days and5,000 closed records. Two matching polls confirm a state/recovery.
  Operation-only unknown status does not trigger incidents. No external messages
  or notification subscriptions are configured.
- Source histories are process-local; dashboard SQLite survives dashboard restarts.
  Unobserved wall-clock intervals are displayed as gaps/unknown, never uptime.
- The supervisor restarts only its own dashboard child on crash or a persistent
  stale/unresponsive collector, with bounded backoff. It never repairs/restarts
  application services. This is one-host crash recovery, not high availability.
  Host-reboot activation/systemd installation is not claimed by `start`.

## Validation and rollback

`python -m pytest tests_health -q` and `node --check pankgraph_health/web/dashboard.js`.
Tests use local synthetic responses, malformed/denied/timeouts, auth boundaries,
retention/incidents, freshness and secret-field filtering; no live inference.

Dashboard rollback: stop this supervisor only; close its dedicated proxy/tunnel.
Preserve its SQLite history and credentials for inspection. For whole-demo rollback,
follow [the unified release runbook](../deploy_reliability/README.md). Keep old release
directories and start from the selected immutable package instead of overwriting code.
