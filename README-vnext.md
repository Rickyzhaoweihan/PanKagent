# PanKagent vNext (isolated development service)

This application adds a graph-first, confirmation-gated workflow alongside the
existing PanKagent entrypoints. It does not import their executable configuration
or replace their deployment. Architecture and dated acceptance reports live in
[PanKgraph_codex](https://github.com/RingoMao/PanKgraph_codex/tree/main/docs/pankagent-vnext).

## Run and test

Python 3.11+ is required. The deployed environment uses Python 3.13. `requirements-vnext.lock` records
its exact runtime packages; `requirements-vnext.txt` also includes test tooling.

```sh
python3 -m venv .venv-vnext
.venv-vnext/bin/pip install -r requirements-vnext.txt
.venv-vnext/bin/python -m pytest tests_vnext -q
# Load private deployment configuration into this shell first.
.venv-vnext/bin/python -m uvicorn pankagent_vnext.app:create_app --factory \
  --host 127.0.0.1 --port 8794 --workers 1 --no-access-log
```

Open `/demo`. Submit a question to resolve its entities and retrieve validated
initial graph evidence. Review the biological plan alongside that preview, edit
the question or switch off the related-evidence check, then confirm to write the
answer. A revision creates a new plan and invalidates confirmation of the old one.
Fresh matching preview evidence is reused. The graph answer appears before
independently completed literature perspectives.
The demo renders scientific Markdown tables, lists and citations with safe DOM
construction; model-produced HTML is never executed.

## Schema-selected answer guidance

`pankagent_vnext/prompts/answer_style.md` restores the original running agent's
rigorous answer conventions: direct answers, tables for structured evidence,
exact values and concise interpretation of the current evidence. It adapts the
format to the question instead of imposing a fixed essay. Planning and streamed
synthesis remain one Claude call each, with the existing 1,600-token output cap.
The old `follow_up_questions` field was separate from `summary`; questions are
not appended to the vNext graph-answer stream. A final presentation contract
keeps a skill's older templates from expanding or overriding the answer format.

The [BIM bundle](pankagent_vnext/answer_skills/SKILL.md) pins the source repository,
original files, normalized JSON and checksums. `answer_skills/manifest.json`
declares explicit aliases and node/edge/composite predicates. The router loads
and verifies it once at startup, scans the full bounded answer graph before
context sampling, and selects only matching guidance. Functional interpretation
requires exact assay feature names; clinical guidance requires recorded clinical
fields. It does not add retrieval tools or infer missing evidence.

There are no routing model calls, network requests or per-request file reads.
A bounded 128-profile LRU caches trusted compiled guidance by schema, recognized
features and clinical field names, without caching questions or evidence values.
At most 18,000 guidance characters are admitted as whole rules; omissions are
reported. Unknown types retain generic evidence interpretation. Multimodal type
co-occurrence selects cautions but does not establish a connected mechanism.

`evidence.answer_profile` and the additive `answer_profile` SSE event record the
bundle/source/style identity, selected rule IDs, matched types, omitted rules,
sampling and routing time. The static style and matched guidance are separate
cacheable Claude prefixes, followed by the concise presentation contract.
`/metrics` includes answer-skill latency and cache
hits. Replaying a run retains its original profile without invoking a model.

To add or revise a skill, follow the bundle's maintenance instructions, update
the manifest and tests, then restart only the isolated service. A corrupt bundle
fails startup. Benchmark local selection without inference:

```sh
.venv-vnext/bin/python -m pankagent_vnext.benchmark_answer_router
```

## Configuration

`pankagent_vnext/config.py` contains defaults, not secrets. Use a mode-600 env file
outside Git. Required deployment inputs are:

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | Existing protected Claude credential |
| `CYPHER_API_TOKEN` | Cypher API team token |
| `PANK_VNEXT_STATE_DIR` | Private sessions, events and budget ledger directory |
| `PANK_VNEXT_GRAPH_IDENTITY_FILE` | Private verified graph identity manifest |
| `PANK_VNEXT_NEO4J_URI` | Explicit RL Bolt endpoint, default `bolt://127.0.0.1:12687` |
| `PANK_VNEXT_NEO4J_DATABASE` | Default `pankgraph` |
| `PANK_VNEXT_NEO4J_USER`, `PANK_VNEXT_NEO4J_PASSWORD` | Database credentials if enabled |
| `PANK_VNEXT_GRAPH_VERSION` | Expected release identity, default `PanKgraph_08_04` |
| `PANK_VNEXT_MODEL` | Default `claude-sonnet-5`; priced challenger `claude-haiku-4-5-20251001` |
| `PANK_VNEXT_BUDGET_USD` | Persistent development cap, default `10` |
| `PANK_VNEXT_CYPHER_URL` | Default `http://127.0.0.1:23917` |
| `PANK_VNEXT_LITERATURE_URL` | Default `http://127.0.0.1:8102` |
| `PANK_VNEXT_CORPUS_VERSION` | Current configured identity `hirn-mixed-current` |
| `PANK_VNEXT_SOURCE_POLICY` | Current `mixed`; do not claim paper-only filtering |
| `PANK_VNEXT_OPERATOR_TOKEN` | Optional operator authorization for detailed health/metrics |

Identity manifests contain `graph_version`, `neo4j_uri`, `database`, sorted
`labels`, sorted `relationship_types`, their `schema_sha256`, and `anchors`
(`label`, `property`, `value`, `count`). They also record
`database_auth_enabled` and `database_role_enforced`. Verify these against the
intended release before enabling queries. Schema and anchors detect pairing
errors but are not a full dataset content digest. A failing identity gate blocks
graph queries; there is no fallback to port 8687.

The current shared Community database has authentication and database read-only
mode disabled. The adapter uses lexical guards, schema/constraint checks,
`EXPLAIN`, READ transactions and timeouts, but cannot provide database-enforced
read-only permissions. Detailed health exposes that limitation. A restricted
database deployment remains a promotion prerequisite.

## HTTP contract

| Endpoint | Contract |
|---|---|
| `POST /v2/plans` | `{question, session_id?, include_context?: true}` → 202; prepare plan and initial evidence |
| `GET /v2/plans/{id}` | Retrieve a plan, validated preview and state |
| `POST /v2/plans/{id}/revise` | `{question, include_context?: true}` → replacement plan/run in the same session; unconfirmed plans only |
| `POST /v2/plans/{id}/confirm` | Idempotent; returns the original run |
| `GET /v2/runs/{id}` | Durable state, initial preview, final graph evidence, available literature |
| `GET /v2/runs/{id}/events` | SSE; resume with `Last-Event-ID` or `after` sequence |
| `POST /v2/runs/{id}/cancel` | Stop new work and attempt active-call cancellation |
| `GET /health/live` | Event-loop/process liveness |
| `GET /health/ready` | Required dependency and runtime admission readiness |
| `GET /health/components` | Cached operator health observations |
| `GET /metrics` | Prometheus-format operational metrics |

Events contain `version`, `run_id`, `session_id`, `sequence`, UTC `timestamp`,
`stage`, `status`, `elapsed_ms`, `type` and `payload`. Persisted SSE IDs prevent
reconnects from starting duplicate inference. Two-second heartbeats describe
current activity without completion percentages. Terminal events include
completed, partial, failed, cancelled, interrupted and superseded outcomes. Service restarts
preserve answers and mark abandoned active work interrupted; they do not replay
billable work automatically. Awaiting-confirmation plans remain reviewable.
Legacy saved plans without initial previews require an explicit revision before
confirmation; refreshing their page does not initiate new model or graph calls.

`preview_step` events contain validated step outcomes; `plan_ready` includes the
saved preview. The separate preview deadline defaults to 45 seconds. Failed,
empty and truncated evidence remain visible. Confirmation reuses successful
outcomes for up to 300 seconds only when the plan, graph configuration and
dependency inputs match. Failed steps may be attempted once at confirmation.
Set `PANK_VNEXT_PREVIEW_TIMEOUT` and `PANK_VNEXT_PREVIEW_TTL_SECONDS` to configure
these bounds. No answer synthesis or literature request runs before confirmation.

Graph evidence includes stable nodes/edges, rows, executed queries, validation,
provenance, graph version and truncation. Failed/empty steps remain visible. Limits
apply to materialized graph results across steps (2,000 nodes, 5,000 edges,
1,000 rows and 2 MB); the public aggregate plus per-step disclosures can make the
serialized run larger than the graph materialization bound. This is a development
API, with no public authentication or production routing installed.
Every JSON response and individual SSE frame has a separate hard 8 MiB ceiling.
Oversized delivery preserves execution IDs/status and bounded answer previews,
but explicitly marks omitted evidence and delivery as partial/truncated.

## Health and spending

Local dependencies are probed every 30 seconds, Claude model access every five
minutes. Polling health does not initiate inference. Actual inference outcomes
are separate from reachability/model lookup; optional inference canaries are not
enabled. Provider-wide status is separate from account-specific access. HIRN
failure reduces literature capability without disabling graph-only readiness.

SQLite reservations atomically bound directly controlled Claude spending across
processes. Actual usage includes prompt cache writes/reads. Interrupted or
ambiguous requests retain their reservation; do not clear it merely to restore
capacity. The budget does not cap upstream HIRN spending, which the current API
does not expose per request. No answer cache is enabled initially; Claude prompt
caching is used. Literature adapter identity includes endpoint, contract, corpus
and source policy for safe future caching.

## Deployment and rollback

`deploy_vnext/manage.py` starts/stops only an owned vNext PID, verifies its command
and working directory, and checks port availability. It never kills a different
listener. Run as the development service account; defaults point to the isolated
`/var/local/serviceuser/projects/pankagent-vnext` checkout and matching private
configuration/state directories. Use `status`, `start`, or `stop`. Stopping vNext
is the rollback; no current agent, tunnel, HIRN process or database is restarted.
The optional systemd unit is a template, not an installed service.

## Evaluation

```sh
python -m pankagent_vnext.evaluate --questions /private/questions.json \
  --answers /private/answers.json --output /private/eval-heldout.jsonl \
  --concurrency 2
```

This evaluates all `split=test` questions, sends questions (not gold answers) to
planning, compares node/edge membership against gold, and resumes existing IDs.
It measures planning plus graph retrieval, not streamed synthesis or literature.
Use a new output file when changing code, model or graph identity. Complete cohort
and pathway targets require full recall in addition to the standard 0.9 F1 gate.
Keep detailed evidence/results outside Git; publish aggregate acceptance reports.

The literature adapter supports current `hirn-agent-v1`. Its version gate rejects
unknown contracts. `PANK_VNEXT_LITERATURE_API_VERSION`, endpoint and corpus settings
provide the migration boundary; add a validated adapter when the clean API's
actual contract is available. A new corpus requires contract tests and cache
identity changes; renaming a corpus does not enforce upstream source filtering.
