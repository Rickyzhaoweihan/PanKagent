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
