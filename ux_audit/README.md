# Protected UX audit tooling

This package supports an **agent-run usability audit**, not participant testing. It does not modify scientific prompts, learn global user preferences, clear official caches or inject production faults.

Run commands from this repository with the service virtual environment. Source histories, manifests, credentials, captures and generated task cards belong in a protected, non-Git directory (directory 0700, files 0600). The scripts print aggregate status only. Do not export bulk production histories or raw captures into a documentation repository.

- `collect_history.py`: read-only selected history inventory, recorded revision links and source hash. Selection line numbers are specific to the 2026-09-06 audit snapshot; review them before using another log. Actor identities remain unknown.
- `freeze_manifest.py`: write-once manifest with 40 distinct task goals, exact recorded revision sequences and ten reserved tasks. Conventional and recovery interventions are explicitly labeled. It refuses to overwrite a manifest.
- `run_pairs.py`: serial API replay with persisted request state/hashes and protected budget checks. It is a companion to browser testing, not a matched browser benchmark. Official pending plans use `/plan/revise`; uncertainty after POST is not blindly retried. Inspect `--help` for narrowly scoped resumption modes.
- `build_bundle.py`: generate forty private Markdown/JSON cards and a sanitized review summary. Missing observations stay unscored; failure text is not counted as a validated preview or grounded graph answer. The sanitized summary omits raw questions/revisions/responses but includes authored reviewer notes—review those notes before publication.
- `recovery_checks.py`: disposable integration checks with fake scientific dependencies and zero upstream calls. Fixture success does not establish scientific or browser acceptance.

Example assembly (no inference):

```sh
.venv/bin/python ux_audit/build_bundle.py --root "$PANK_AUDIT_DIR"
```

`PANK_AUDIT_DIR` must be a protected directory containing `manifest.json`, `reviews.json`, and `cases/`. Use the original frozen manifest for replay. Review notes contain a classification, six optional 0–3 dimensions, observable reasons/references, source-review limitations and blockers. Reserved tasks and dependent inspection tasks are excluded from improvement selection; never retune on their captured results.

## Additive journey logging

The existing lifecycle request/response shapes remain compatible. Revision requests accept optional `revision_instruction`, `revision_mode` (`instruction`, `replacement_question`, `legacy_replacement`) and `event_source` (`user`, `audit_replay`, `synthetic_fault`). Audit/synthetic request provenance requires operator authentication. These fields record what was sent; they do **not** repair planner semantics or silently reinterpret legacy clients.

Run audit metadata persists original question, parent IDs, revision index, prior options and plan hash, deployment hashes, requested options and confirmed plan hash. Parent run snapshots preserve before/after filters and previews. Existing run/event records preserve evidence, query outcomes, citations, errors, cancellation, cache reuse and answer sections. Legacy missing metadata remains unknown.

`POST /v2/runs/{id}/interactions` and `GET /v2/runs/{id}/audit` require the operator token. The authenticated results gateway proxies only the interaction POST; it never exposes the audit snapshot. `POST /api/results/{id}/interactions` uses the results application's existing authentication. No new visible controls are added.

Interaction events accept UUID event/page IDs, one of four fixed kinds, a restricted reference string and browser timestamp/elapsed time. They do not accept free-form questions, resource URLs or literature frames. Duplicate IDs are idempotent; records are bounded to 32 KB and 1,000 events per run/result. Store failures return an observable unavailable status and increment diagnostics without failing an otherwise valid answer. Browser events are observed per page/target; a true page reload can produce a new display observation and is not a duplicate delivery.

Task-local provider hooks persist budget reservation/settlement references for both agent and conventional synthesis. Do not treat a reservation as actual expenditure or release uncertain usage automatically. The shared $10 cap is cumulative; original-service/HIRN costs are separately unattributed. Conventional browser/audit provenance is mapped through protected audit captures; per-event explicit actor metadata remains a documented gap.

## Checks

```sh
.venv-vnext/bin/python -m pytest tests_vnext/test_audit.py tests_vnext/test_ux_runner.py tests_vnext/test_ux_bundle.py -q
.venv-vnext/bin/python -m pytest tests_vnext tests_results -q
```

The 2026-09-06 baseline report lives in the PanKgraph project-home repository under `docs/pankgraph-vnext-results/ux-audit-2026-09-06/`. Scientific failure findings are backlog items for a subsequent candidate, not changes made during baseline measurement.
