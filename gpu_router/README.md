# gpu_router — stable tunnels + load balancer for the cypher-writer vLLM fleet

The `cypher-writer` model runs on **5 GPUs**: 2× H100 + 2× L40s on the ARC
cluster, plus 1× L40s local to this box.

| Backend (local) | GPU | Where | Reached via |
|---|---|---|---|
| `127.0.0.1:7000` | H100 | `lh0304` (cluster) | SSH `-L` tunnel |
| `127.0.0.1:7001` | H100 | `lh0304` (cluster) | SSH `-L` tunnel |
| `127.0.0.1:7002` | L40s | `lh0303` (cluster) | SSH `-L` tunnel |
| `127.0.0.1:7003` | L40s | `lh0303` (cluster) | SSH `-L` tunnel |
| `127.0.0.1:8002` | L40s | this box | already local |

The **router** listens on `:8010` and balances across all 5. The app points at
it with `VLLM_PORT=8010` (so both `text2cypher_agent.py` and `text2sql_agent.py`,
which call `http://localhost:${VLLM_PORT}/v1`, go through the router). The cluster
servers themselves are managed separately under
`/nfs/turbo/umms-drjieliu/usr/rickyhan/port_forwarding/` (SLURM + self-healing monitor).

## Pieces
- `tunnel_supervisor.py` — opens ONE ssh to `lighthouse.arc-ts.umich.edu` holding
  all four `-L` forwards (one connection = one Duo approval), authenticates with
  the password from `../.env` + a **Duo push** (you approve on your phone), then
  auto-reconnects on drops with backoff.
- `load_balancer.py` — FastAPI/uvicorn proxy on `:8010`. **Weighted
  least-connections** (H100 ports weight 2, L40s weight 1) with active `/health`
  checks that drop/restore backends, SSE streaming passthrough, and one retry on
  a different backend.
- `watchdog_client.py` — cron-driven (flock + two-strike) restart of either
  component if it dies (modeled on `../watchdog/watchdog.py`).
- `gpu_config.py` — shared config, all from `../.env`.
- `run_tunnel.sh`, `run_router.sh`, `stop.sh`, `status.sh` — launchers.

## Setup
1. Install deps into the **app's** Python env (the one that runs `server.py`,
   i.e. `/db/usr/rickyhan/envs/agent`):
   ```bash
   /db/usr/rickyhan/envs/agent/bin/python -m pip install pexpect
   # fastapi / uvicorn / httpx / python-dotenv are already present in that env
   ```
2. Add to `../.env` (and keep it `chmod 600`):
   ```
   SSH_USER=rickyhan
   LIGHTHOUSE_HOST=lighthouse.arc-ts.umich.edu
   SSH_PASSWORD=********
   DUO_OPTION=1
   VLLM_PORT=8010
   ROUTER_PORT=8010
   ROUTER_BACKENDS=127.0.0.1:7000:2,127.0.0.1:7001:2,127.0.0.1:7002:1,127.0.0.1:7003:1,127.0.0.1:8002:1
   MAX_CONCURRENT_QUERIES=20
   ```
3. Make sure the local L40s vLLM is up on `:8002` and the cluster fleet is up
   (`port_forwarding/status.sh` on the cluster).

## Run
```bash
# Launchers must use the app's interpreter (base python3 lacks uvicorn/pexpect):
export GPU_ROUTER_PYTHON=/db/usr/rickyhan/envs/agent/bin/python

bash gpu_router/run_tunnel.sh     # then APPROVE the Duo push on your phone
bash gpu_router/run_router.sh
bash gpu_router/status.sh
```

Stop: `bash gpu_router/stop.sh [router|tunnel|all]`.

## Keep it alive (cron, this box)
```cron
* * * * * GPU_ROUTER_PYTHON=/db/usr/rickyhan/envs/agent/bin/python /usr/bin/flock -n /db/usr/rickyhan/PanKLLM_implementation/gpu_router/watchdog.lock \
    /db/usr/rickyhan/envs/agent/bin/python /db/usr/rickyhan/PanKLLM_implementation/gpu_router/watchdog_client.py \
    >> /db/usr/rickyhan/PanKLLM_implementation/gpu_router/logs/watchdog.log 2>&1
```
The watchdog never kills a *live* tunnel supervisor (it self-reconnects); it only
restarts a component whose process has died. **A tunnel restart re-triggers a Duo
push** — you must approve it on your phone.

## Caveats
- **Duo + no SSH keys ⇒ every reconnect needs a phone tap.** Keepalives keep the
  single connection alive for long stretches, so taps are rare (real network
  drops / login-node restarts). For truly unattended operation, switch to SSH
  public-key auth on lighthouse.
- `SSH_PASSWORD` is plaintext in `.env` (gitignored) — keep `chmod 600 .env`.
- Capacity: 5 × `max_num_seqs=32` ≈ 160 generation slots. With
  `MAX_CONCURRENT_QUERIES=20` and ~5 candidate Cypher calls per query (~100
  concurrent generations) there's headroom; the real ceiling at higher
  concurrency is the Anthropic API, not vLLM. Tune `MAX_CONCURRENT_QUERIES` (and
  maybe `PLANNER_CANDIDATES` in `main.py`) after a load test.
```
```
