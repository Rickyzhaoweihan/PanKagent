# Deployed dev agent — source of record

This directory records the agent actually serving dev on **2026-11-04**.
It is on PanKagent's **Ringo** branch. Start here for the deployed Qwen agent;
the repository-root legacy planner is not the current dev query engine.

## Entrypoints and ownership

| Source directory | Live role | Bind |
| --- | --- | --- |
| `source/native` | `candidate_api:app`, Qwen query workflow and OpenAI formatter | loopback 18864 |
| `source/gateway_agent` | `pankagent_vnext.app:create_app`; `dev_integration.py` dispatches new Qwen requests and preserves historical runs | loopback 8794 |
| `source/gateway_results` | `pankgraph_results.app:create_app`; authenticated results/download gateway, direct dev-origin CORS | loopback 8795 |

The native source is copied from
`/db/pankagent-vnext-private/operations/lighthouse-direct/dev-candidate`.
Gateway-agent source is from release `20261014-communication-v1-staged-v3/backend`;
gateway-results source is from `20261019-qwen-default-gateway/backend` under
`/var/local/serviceuser/projects/pankgraph-demo/releases/`.
Both dependency trees are retained because the native formatter/presentation code
imports the first release while the public results gateway runs the second.
They must not be conflated or replaced with the repository-root packages blindly.
Every included file is byte-identical to its deployed source; SHA-256 values are
in `source-manifest.json`. The capture includes Python, required schemas/prompts,
package assets and available requirement files, not a server filesystem backup.

## Current agent configuration

- Native application: `source/native/candidate_api.py`.
- Model endpoint: server-local `http://127.0.0.1:33924/v1`, alias `pank-candidate`.
  `candidate_api.py` overrides the historical defaults in `native_client.py`.
- Query workflow: two loops, retained history and native seven-tool protocol;
  coverage/ambiguity cross-check enabled, experimental fast prompt disabled.
- `simple_agent/core.py` owns tool definitions and loop behavior;
  `transport.py`, `resolver.py`, and `baseline/remote_bridge.py` own execution.
- `simple_agent/workflow.py` owns plan/confirm, events, cancellation and saved runs.
- Formatter: `simple_agent/openai_formatter.py`, `gpt-6.1-sol`, low reasoning,
  streaming, `store=False`, evidence-only input with versioned answer skills.
- The candidate sets `SIMPLE_AGENT_NATIVE=1`: tools/presentation execute on the
  server, not through a Mac SSH subprocess. The server-local model forward must
  already be operational; these sources do not establish a new GPU allocation.
- Model key is read from the existing private `state/api-key`; provider/database
  configuration is read from serviceuser's private `runtime.env`. Neither is in Git.
- `calls/` and `workflow-state/` remain private runtime directories. Do not commit
  them, recreate history, or move state when restoring code.

## Restore and future development

This is the exact deployed implementation, **not a new portable installer**.
Existing absolute release/config references are preserved intentionally so the
capture does not silently alter behavior. A relocation requires explicit path
configuration and integration testing. Use Python 3.11+ and the existing reviewed
runtime/dependency locks; a source checkout alone does not provision databases,
credentials, model weights, network forwarding or persisted state.

For restoration, first verify owned listeners, back up source under the private
`/db/pankagent-vnext-private` tree (0700 directories, 0600 files), and compare the
manifest. Map each source directory only to its matching live root. Preserve all
private state and secrets. Do not restart services or replace the root packages
merely by pulling Ringo. Restart only an explicitly intended owned service after
idle/rollback checks, then verify authenticated plan/confirm/events/results.
This sync itself did not restart or modify any server process.

Future agent changes should update this implementation and its deployment
provenance together, rather than leave fixes only on the server. Frontend remains
in the separate `wangyiqunumich/pank_frontend` `xuteng/react` branch.

## Validation and known issue

- All captured Python parsed successfully; source hashes verified.
- Credential-pattern and literal credential-assignment checks found no secrets.
- Native offline suite: `cd source/native && python3 -m unittest discover -s tests -v`.
  **37 tests executed: 36 pass, 1 existing failure**:
  `test_declared_unfulfilled_condition_cannot_be_complete` expects `partial`, but
  the deployed implementation returns `complete`. This discrepancy is retained
  and reported, not hidden by weakening the test or silently changing live code.
- No paid model call, database mutation, query replay or live deployment was
  performed for this source synchronization. A captured implementation is not
  evidence of scientific correctness.
