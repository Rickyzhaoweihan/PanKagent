# Separate viewer evidence from answer inputs

The renderer reads `GET /v2/runs/{id}/graph?phase=preview|final` through the
existing authenticated results service. Normal run snapshots, formatter inputs,
answer generation, SSE and saved answers are unchanged. The graph endpoint uses
canonical saved results, never formatter identity samples.

`viewer_evidence.py` selects `answer_step_ids` when present, retains verified
supporting paths, and excludes intermediate populations from renderer step
batches. Without explicit targets, successful independent steps remain separate.
Failed siblings preserve partial status. Stable G citations and originating step
provenance remain attached. Display limits remain a renderer concern.

The results loader uses this endpoint for agent runs only. Its existing agent
answer path copies the saved answer without synthesis. Graph membership is
independent of the aggregate-only answer policy. Preview cannot borrow final
answer text or final evidence.

An ID-only backend producer can declare `graph_identity_membership` with
`graph_version`, `sampled: false`, `complete` and `typed_ids` entries containing
`id` and `entity_type`. Missing annotations are read with a parameterized exact-ID
lookup against the verified release. No neighborhood expansion occurs. Missing,
failed or truncated hydration remains explicit; it cannot manufacture a complete
count. A scalar aggregate without membership is unavailable for graph annotation
and requires a producer-side membership query with the original predicates.
There is no PostgreSQL query producer in the current agent; this contract supports
one without guessing IDs from aggregate rows.

For the reported Stage 3 run, donor annotations already exist and require no new
query. The separate missing source predicate is outside this change. Formatter
completeness caveats are also unchanged.

Deployment stages independent overlays on the currently owned agent and results
releases, preserving the results service's newer layout/reliability code. Only
three agent files and two results files change. `deploy_viewer/stage.py` rejects
unexpected source before applying the results overlay, and records parent release,
Git commit and changed-file hashes. Existing frontend and state are retained.

## Acceptance (2026-09-23)

- Targeted viewer, results API/projection, chain algebra, answer synthesis,
  oversized-context and runtime tests pass. Byte and AST assertions protect the
  existing formatter and answer-generation paths.
- A broad results-suite run encounters 56 failures; the exact same failed test
  set reproduces on untouched commit `8c8a771`. These are not claimed fixed here.
- The exact saved Stage 3 run produces 44 donor nodes, identical membership,
  unchanged saved answer, and an optimized layout with zero overlaps.
- A live read-only typed-ID lookup recovered annotations for three existing IDs.
- Live authenticated results creation returned ready with all 44 donor nodes and
  exactly the saved answer. No paid external model calls were made.
- Browser inspection stalled; rendered browser acceptance remains unverified.
  API, exact membership and real layout acceptance were completed separately.

Private staging, backup, validation and deployment records are stored under
`/db/pankagent-vnext-private/operations/viewer-evidence/`. Activation uses the
ownership manager separately for 8794 and 8795. Health ownership and production
are preserved; rollback starts the parent releases without restoring old state.
