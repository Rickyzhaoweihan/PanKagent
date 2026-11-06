# Model-owned query decisions (2026-11-06)

The `model-owned-v2` contract replaces the scope-policy guards introduced on
2026-11-05. The model owns interpretation, candidate choice, search revision,
evidence selection, and answer assessment. Tools provide schema, execution,
identity/provenance and retrieval facts. Schema checks remain technical checks,
not scientific validation.

Removed: mandatory all-candidate IN filters, lexical source-family routing,
Cartesian source coverage, earlier-query relaxation bans, qualifying-ID relevance
rules, prefix exclusion, forced second finish, and permanent diagnostic exclusion.
Schema source descriptions remain descriptive, not mandatory search recipes.

`query.purpose` records the initial working purpose. `select_results` accepts a
new purpose and optional rationale without rewriting the executed query. Replace
selection works in enrichment too; old graph records are not silently pinned.
The exact execution cache is independent of working purpose. `finish.status` is
model-assessed and separate from `retrieval_status` and retrieval checks.

`inspect_result` accepts only graph kind, query ID, offset and limit <=30.
`inspect_evidence` accepts exact owned object/table arguments; without a ref it
lists the query's owned descriptors. Illegal fields receive concrete feedback.
Schema index documents case-sensitive literal string operators. No automatic
filter rewriting, identity merging or graph mapping is added.

Only confirmed identity-field conflicts are rejected by identity validation.
Multiple candidates, source-inventory provenance and question keywords do not
constitute a technical rejection. Directed endpoint validation, scalar types,
read-only transport and exact evidence ownership remain enforced.

Deployment configuration supports `agent_max_model_calls` (default 48) and
`agent_deadline_s` (default 300). Exhaustion is an operational interruption with
retained checkpoints, never a model-declared scientific failure or empty result.
Explicit cancellation remains available. Stage events reflect actual tools.

Outgoing context compacts old tool outputs and, only when necessary, removes
whole old exchanges from the model input with an explicit recovery notice. Full
audit messages and evidence remain intact. `list_results` exposes retained query
handles, exact patterns, pagination and selections; graph/evidence readers recover
records. Original questions and complete recent tool-call/result pairs remain.
This is a context resource policy, not scientific selection or a finish decision.

New formatter requests receive the original question, selected graph, selection
rationales, model assessment, independent retrieval checks and versioned schema
interpretation. The formatter is instructed to correct assessment/evidence
conflicts. Historical schema-agent-v1 uses its schema formatter path; older
saved results retain the legacy adapter. Cross-version execution context is
rejected while historical GETs remain readable.

## Validation

Run `python -m unittest discover -s tests_schema -q` from `source/native`.
42 focused tests cover renamed types, model candidate choices and selection
revision, technical identity conflicts, independent branches, source alternatives,
reader contracts, source rules, same-partner compilation, context identity,
cache reuse, resource interruption and separate answer/retrieval status.
The unchanged legacy suite has 36 passes and its previously documented failure
`test_declared_unfulfilled_condition_cannot_be_complete`.

Real-model acceptance artifacts stay private under
`/db/pankagent-vnext-private/operations/model-owned-20261106/`.
Early candidate failures are retained, including an overlong planner summary
with invented pathway names; it is not scientific acceptance. Generic concise
fact-grounded finish instructions replaced that behavior, without entity-specific
prompt cases or semantic rejection scripts. The independent formatter corrected
the preserved bad summary against actual graph records.

A controlled, renamed-schema recovery fixture supplies two recorded initial
queries: a positive membership and a case-sensitive empty substring search.
Subsequent actions are from the real model. It selects the prior diagnostic as
requested evidence with a rationale and answers from the actual membership.
These initial controlled actions are explicitly marked in the trace.

The paired code/schema/config rollback never restores session or budget state.
Only the owned dev 18864 process is eligible for restart; production, 8794/8795,
graph data and frontend design are outside this change.
