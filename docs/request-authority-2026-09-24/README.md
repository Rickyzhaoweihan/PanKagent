# M04 request authority candidate

Candidate on the existing `Ringo` branch, based on `93b8982`. Not deployed and
not accepted for release: live correctness found blockers; performance remains pending.
See [the live validation report](live-validation/README.md).

## Changes

- `request_context.py` supplies server-owned original raw wording, current raw
  input, explicit revision and the derived effective question to every Claude
  inference entry point: planning and repair turns, revision interpretation,
  Cypher repair, plan verification and streaming-answer preparation.
- Context is isolated between concurrent investigations, saved with plans/tasks,
  and restored for confirmation/answer generation. Legacy correction history
  retains raw wording independently of canonical corrected text. Missing
  historical raw fields remain null and explicitly unavailable.
- M04 instructions and D02's existing `record_plan` decision make the current
  request authoritative. Explicit revisions supersede removed conditions.
  No extra model, inference call, stage or retry allowance was added.
- Tool outputs separate verified facts, advisory suggestions and diagnostics.
  Optional `advisory_decisions` records recognized rule IDs and their disposition;
  local draft task signatures additionally record whether the selected plan
  retained or replaced them. These audit fields do not control execution.
- Disease identity alone no longer activates donor processing or permits an
  unrequested mandatory `HAS_DONOR` join. Entity-only tasks remain independent
  of donor siblings. Genuine donor/sample predicates and checks remain active.
  Entity lookups receive an entity display group, rather than a donor label.
- New request metadata is excluded from retrieval fingerprints so a wording or
  literature-preference revision can still reuse an unchanged checked query.
  Constraints, dependencies and other executable scope remain fingerprinted.
- Formatter changes only prepare inputs and account for the additional context
  in the existing byte budget. `synthesize` is AST-identical to the parent;
  formatter output methods, presentation contracts and streaming are unchanged.

## Validation

See `checks.json` for the exact offline results, baseline comparison, formatter
method comparison and diagram hashes. Tests capture actual Claude gateway
request payloads using mock transports, including planning repair and streaming.
They also exercise real task preparation, revision/confirmation persistence,
legacy correction, concurrent requests, input compaction and disease Cypher
validation. No model-generated response is treated as live scientific evidence.

Two existing preview tests now compare executable fields separately from the
new per-request audit metadata. They still assert unchanged planner/query call
counts, preview reuse and literature preference. The existing 19 baseline
failures were not weakened or reclassified.

## Diagram

Only `plan` (M04), `claude-tools` and `scope` (D02) labels/tooltips/metadata were
edited in tab 01 of `../schema-consolidation-2026-09-24/pankagent-current-io-payload.drawio`.
The latest project-home diagram was read and backed up first, then updated in
place. All geometry and connectors are unchanged; tabs 02–04 are byte-identical.
The exported tab was visually inspected for text fit.

## Outstanding release gate

No paid calls were made during implementation. On 2026-09-24 the user explicitly
confirmed that PanKgraph content is public and authorized its use in Claude
validation and dev deployment. The earlier data-use approval blocker is resolved.
The existing cumulative validation ledger remains authoritative and is not reset.

Run the existing frozen frontend/HPAP correctness manifest and
matched performance controls with the existing ledger, inspect supported answers
and full protected memberships, and compare latency/cost against the frozen
baseline. Do not deploy before those gates pass. Production, serving dev, results
service, sessions and runtime budget state remain unchanged by this candidate.
