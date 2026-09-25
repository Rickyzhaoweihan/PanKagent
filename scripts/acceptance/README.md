# PanKgraph acceptance replay

Run only in protected service-owned storage (directory 0700, files 0600). These scripts read the configured databases and spend from the **existing cumulative** validation ledger; do not create a fresh ledger to reset the authorized ceiling. They never deploy a service.

Required environment:
- `PANK_ACCEPTANCE_ROOT`: protected output directory, outside Git.
- `PANK_ACCEPTANCE_ENV`: protected existing runtime environment file.
- `PANK_ACCEPTANCE_BUDGET_STATE`: existing cumulative validation ledger directory.

Use the deployed virtualenv and run `frontend.py 0 1 ... 13`, `hpap.py 0 1 ... 9`, and `runtime.py followup nd comparison`. Refresh `tests_vnext/fixtures/acceptance/frontend.json` using the catalog extraction script before freezing a release. The fixture records source commit and every displayed occurrence, including concrete substitutions.

`frontend.py` runs real grounding, planning, query execution and answer synthesis. Inspect supported conclusions against the retained evidence; answer length alone is not acceptance. `hpap.py` compares full distinct ID memberships to independently authored Neo4j references before synthesis. `runtime.py` exercises plan/confirm, saved-run refresh and delta reconstruction; literature is deliberately unavailable, so a partial terminal status alone is expected. All backend records and model traces remain private. Runtime membership checks must also be compared to the HPAP references before release.

An incomplete retrieval, unresolved scope, or planning rejection is not a passing count. Generalization is covered offline by a renamed type/property fixture, not another deployed KG. Optional PostgreSQL OCR mappings remain disabled until separately verified.

Use `runtime_membership.py` after the runtime replay to compare follow-up and comparison populations. Use `revisions.py` for preview-stage narrowing, widening and replacement through the existing API; it prints counts and membership matches, never IDs.

## Consolidation regression suite

`regression.py` runs the frozen `tests_vnext/fixtures/acceptance/regression.json`
manifest through plan, preview, confirm and answer at concurrency two. Use
`PANK_ACCEPTANCE_SUITE=correctness` for the 51 question cases and `controls` for
eight controls repeated three times. Use a new protected output directory for
each run. `PANK_ACCEPTANCE_CASES` optionally selects explicit case keys for a
bounded diagnostic rerun; such a subset cannot satisfy the full release gate.
The script refuses a missing cumulative budget ledger. It obtains settled cost,
usage and model durations from per-run audit events, including streamed answers.

`references.py` refreshes the 37 independent membership references without any
model calls. Reference memberships stay private. The 14 concrete frontend cases
also require a recorded supported-answer review against their returned evidence;
nonempty prose or a successful HTTP status is not a pass.

`compare_performance.py baseline-summary.json candidate-summary.json report.json`
checks matched successful controls: median latency may increase by at most the
larger of 20% or two seconds; mean settled model cost by at most 20%. It blocks
lost successful controls and excludes failed baselines from cost comparisons.
Release review must additionally confirm all 24 controls are present per version,
review every correctness case, and verify revisions and independent branch failures.
Budget exhaustion or missing approval leaves acceptance incomplete, never passed.
