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
