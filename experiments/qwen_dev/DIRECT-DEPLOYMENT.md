# Server-to-server deployment and recovery

Verified 2026-10-24. The Mac is not a runtime transit host. Administrative SSH can be performed from any authorized machine; a future U-M login/allocation may still require Okta.

## Actual routing

```text
existing dev.pankgraph.org frontend (xuteng/react, unchanged UI)
  /pankgraph-vnext/api/* -> jieliu3 nginx -> results gateway 8795
  -> agent facade 8794 -> native candidate 18864
  -> jieliu3 loopback 33924
  <- reverse SSH initiated by Lighthouse compute node
  -> compute-node loopback 23924 (vLLM pank-candidate)
```

The reverse forward runs on the SAME compute node as vLLM, not on a login node or Mac. The previous GPU/login/Mac/jieliu relay is not required. All application/model ports remain loopback-only; existing authentication is retained.

| Component | Active location / owner |
|---|---|
| Model and relay | runbomao, /home/runbomao/pank-qwen-direct on Lighthouse |
| Candidate | serviceuser, /db/pankagent-vnext-private/operations/lighthouse-direct/dev-candidate |
| Agent 8794 | releases/20261014-communication-v1-staged-v3/backend |
| Results 8795 | releases/20261019-qwen-default-gateway/backend |
| Release root | /var/local/serviceuser/projects/pankgraph-demo |

`dev_integration.py` belongs to the 8794 release, not the candidate package. `qwen_dispatch.py` belongs to the DIFFERENT 8795 release. Its legacy `haiku` routing label means the current 8794 facade; it is not a claim that new requests use Haiku. New admissions route to that facade. Existing qwen-prefixed history and explicit legacy /api/qwen routes remain on the old handler (18827); existing simple history retains its old handler. Do not remove historical services or overwrite old state. Request ownership checks can return 503 when a historical owner is unavailable; do not bypass ownership or rewrite request IDs.

Frontend remains wangyiqunumich/pank_frontend, xuteng/react, commit 96fa3a5dbddb2b151e0f45f7d95befd0fe080ac4. Amplify job 850 succeeded without UI changes. This directory is backend deployment source, not a replacement frontend branch.

## Provisioning and secrets

Use umask 077, private directories 0700 and files 0600 (executable scripts 0700). Keep credentials out of Git and command output. Lighthouse api-key and jieliu3 operations/state/api-key must match. candidate_api reads ../state/api-key; native client defaults are overridden there to 33924 and pank-candidate. Never use its library default 33920 for this deployment.

The dedicated RSA key jieliu-tunnel-rsa stays on Lighthouse. Its public key is installed in runbomao's authorized_keys on jieliu3 with this restriction prefix:

```text
restrict,port-forwarding,permitlisten="127.0.0.1:33924",permitopen="127.0.0.1:1",command="/bin/false" ssh-rsa PUBLIC_KEY pank-qwen-27970923-direct-relay-rsa
```

This disables shell, PTY, agent/X11 forwarding and user rc. Only the specified remote listener is allowed; local forwarding is limited to loopback port 1. The deployed OpenSSH rejected permitopen="none"; use the verified restriction above. Seed jieliu-known-hosts from an independently trusted host key and use StrictHostKeyChecking=yes. Do not accept an unverified scan or copy private keys through Git.

Stage deployment/direct-relay.sh, launch.sh and preflight.py in the Lighthouse root. launch.sh uses the existing read-only vLLM 0.24 environment and pinned checkpoint; do not pip-install into the shared environment. Its private tools/ overlay must contain executable ptxas, ptxas-blackwell, cuobjdump and nvdisasm compatible with that environment, with the TRITON paths and private caches specified in launch.sh. These dependencies are prerequisites, not bundled binaries. Check disk space and allocation resources before starting.

## Start or recover within an existing allocation

On Lighthouse, read job-id, verify scontrol show job ownership, RUNNING state, assigned node and remaining time, then inspect squeue --steps for that exact job. Current recorded allocation is 27970923 on lh0304; model step .17 and relay step .22 are historical identifiers to recheck, not permanent constants.

Only if the corresponding component is absent, run from /home/runbomao/pank-qwen-direct, replacing VERIFIED_JOB_ID and VERIFIED_NODE with the verified allocation values:

```sh
umask 077
nohup srun --overlap --ntasks=1 --cpus-per-task=8 --jobid=VERIFIED_JOB_ID --nodelist=VERIFIED_NODE bash ./launch.sh </dev/null >>logs/qwen-model.log 2>&1 &
# Run separately after confirming the model is healthy; never duplicate a model.
nohup srun --overlap --ntasks=1 --cpus-per-task=1 --jobid=VERIFIED_JOB_ID --nodelist=VERIFIED_NODE bash ./direct-relay.sh </dev/null >>logs/direct-relay.log 2>&1 &
```

The relay uses flock, SSH keepalives (15 seconds, three misses), ExitOnForwardFailure and a ten-second reconnect loop. It has no Mac control socket. If 33924 is occupied, identify its owner before acting; do not kill unrelated SSH masters or the old 33920 service. A transient disconnect requires no model restart. For a persistent failure inspect logs/direct-relay.log, authorized key restrictions, trusted host key and destination availability. Stop only the verified relay step when replacement is necessary; never cancel the allocation/model to repair forwarding.

For model failure, inspect logs and GPU/compiler activity before restarting. Compilation can take time. Start models only through srun inside the authorized allocation. Never run GPU/model workloads on a login node. The allocation's sleep-infinity keeper preserves the reservation after a model exit, but does not restart the model or extend the reservation.

At expiry obtain a new authorized single-H100 allocation, verify its node, update job-id and start model plus relay there. The recorded allocation expires 2026-10-29 22:53:30 America/Detroit. No automatic renewal is provided. Mac independence does not remove Slurm expiry, host availability or authentication requirements.

## Candidate and gateway recovery

As serviceuser, use the existing 20261014 backend .venv/bin/python and the candidate directory as cwd to run `-m uvicorn candidate_api:app --host 127.0.0.1 --port 18864`, with stdin detached and logs in private operations storage. First check the existing listener/process; never start a duplicate. Preserve workflow-state and calls. No paid formatter is invoked.

For gateways use each component's OWN backend release and existing manager:

```sh
.venv/bin/python -m deploy_reliability.manage agent status --release ABS_AGENT_BACKEND
.venv/bin/python -m deploy_reliability.manage results status --release ABS_RESULTS_BACKEND
```

Use the same manager's stop/start actions only for the verified owned component when required. Back up the exact module before updating, keep auth/results paths and history dispatch, and verify no active request is interrupted. Do not replace the legacy simple_agent package with this candidate package. Do not restart unrelated services or restore old databases/state snapshots.

Rollback: restore the saved pre-change dev_integration.py and/or qwen_dispatch.py in their respective releases and restart only that owned service. Retain new candidate histories for diagnosis. Private backups are operations/lighthouse-direct/backups/20261023T195527Z-dev-qwen and backups/20261023-hosted-gateway. Recheck the private deployment manifests for the exact paths/hashes before restoring.

## Verification

From jieliu3, authenticate GET http://127.0.0.1:33924/v1/models using state/api-key without printing it: require pank-candidate. GET 18864/health must return 200. Then use the normal 8795 plan -> confirm -> run/result chain (public route requires existing authentication), verify SSE/recovery/history and ready graph projection. A health response alone is insufficient.

Recorded evidence: deliberately terminating only the dedicated relay SSH child was followed by automatic reconnect and authenticated model discovery. A subsequent real two-phase PTPN22 query through 8795 completed with 58 graph nodes and 88 edges and ready projection. Evidence is private state/direct-relay-verification.json. This proves operational execution, not scientific correctness (the answer's GO count must not be equated with all graph nodes). The HLA-DRA screenshot question also completed with explicitly partial retrieval. On 2026-10-24 a fresh jieliu3 model identity check and candidate health check passed. No Mac transit is used by these checks.
