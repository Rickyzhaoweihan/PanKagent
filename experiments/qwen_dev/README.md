# Qwen dev agent deployment — 2026-10-23

This directory records the source deployed behind the existing PanK dev authentication/results gateway. It uses the evaluated coverage/ambiguity-check Qwen candidate, without the experimental fast prompt. Frontend files are unchanged.

The candidate runs on loopback 18864. The existing agent gateway on 8794 forwards new plans and `simple-qwen-` / `plan-qwen-` identities to it. Existing saved simple-agent history continues through the existing handler. The results service on 8795 is unchanged. `dev_integration.py` is the deployed gateway module and depends on the existing release's `simple_agent.comparison` and legacy server; do not install the candidate package over that legacy package.

## Runtime

Run `candidate_api:app` with the existing backend virtualenv from this directory, as serviceuser, on 127.0.0.1:18864. The directory is deployed under the private lighthouse-direct operations directory. `../state/api-key` supplies the model credential; no credentials are committed. Runtime directories require mode 0700 and files 0600 (`umask 077`). The model is `pank-candidate` at loopback 33924, forwarded to the authorized single-H100 allocation. Availability depends on that allocation and the SSH relay remaining alive; this is a dev deployment, not a permanent inference service.

The native transport retains tool messages and history. The original seven tools and two agent loops remain. No parameter repair, automatic filters, additional agent stages, or enrichment deadline is introduced. The answer renderer is deterministic and does not make a paid formatter call. Presentation uses existing backend components. Schema metadata is pinned in schema_pack.json; no query results or user history are committed.

## Verified after deployment

The exact reported question, “What pathways and interaction partners connect HLA-DRA to antigen presentation in T1D?”, completed both phases through port 8794. It produced a ready result with 241 nodes and 292 edges, explicitly marked partial retrieval. This verifies execution and result delivery, not exhaustive scientific correctness. Preparation took about 67.6 seconds and the final run elapsed time was about 103.5 seconds.

Result projection and readback, SSE event replay, repeated confirmation without changing persisted state, cancellation before admission, and an existing completed historical run read all passed. The previous gateway source and saved simple-agent history were backed up before restarting only the owned agent service. No database restoration or migration was performed.

## Rollback

Restore the saved pre-change dev_integration.py in the active backend release and use the existing deploy_reliability manager to restart only the owned agent on 8794. Do not roll back databases or erase either workflow history. Keep the private candidate state for inspection. The deployment manifest and backup location remain in private operations state, not Git.
