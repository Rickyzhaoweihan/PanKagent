# Advisory language validation — 2026-10-03

The wording “samples from pancreatic lymph node (PLN)” was incorrectly classified as an unknown dataset source after the model had correctly resolved HPAP, stage 3, PLN and snMultiomics. Four lexical checks for cohort/source/tissue/assay phrases now produce advisory interpretation warnings, not plan rejection. The optional wording guidance lives in validation.json, one of the four schemas. Other schema packs need no such configuration.

Warnings reach the existing formatter through requested_scope. Verified empty results with interpretation warnings also use the formatter instead of unconditional absence wording. No additional LLM stage was added. Explicit bound predicates, read-only queries, schema validity, parameter values, connected-path evidence and execution limits remain checked. This is a targeted removal of phrase-based vetoes, not a claim that every remaining legacy KG-specific validator has been generalized.

## Validation

- Canonical focused suite: 91 passed; same suite against the actual deployed-base patch: 91 passed.
- Full suite: 3,137 passed, 34 failed, 5 skipped, 231 subtests passed. Failure names exactly match the pre-change 34 failures; no new failures.
- Mocked model lifecycle verifies the exact screenshot wording, three assay spellings, zero/nonzero results, preview, confirmation, evidence compaction and formatting. Genuine filter values and connected sample roles remain enforced.
- Real graph replay: scRNA-seq returns zero; snMultiomics returns 13 distinct samples, including the expanded failed wording. All four predicates remain bound. The failed wording now carries advice instead of E04.
- Deployed agent and results return valid JSON readiness. Public saved-run API returns JSON HTTP 200. No fresh paid LLM generation or browser interaction was repeated for this patch; API spending for this change is $0.

## Deployment

Only five backend files were applied to the previously deployed cohort-fix release. The full Ringo introduction candidate remains inactive. Model remains claude-sonnet-5-5; frontend, results service and persistent state were preserved. A new online backup was verified before activation. Agent PID: 2657026.

Code commit: `f8cfa1e1e471fcc3781505bcf4c6014c93abc4b2`. Live release: `/var/local/serviceuser/projects/pankgraph-demo/releases/20261003-language-advice-f8cfa1e/backend`. Live schema: 2.2.2; canonical inactive candidate schema: 2.3.2.

Application SHA256: `f4d091123bdf9d119ec8f7e7a1c1e9096c3e5cf6b295942d02b98c08323f3a5c`. Schema SHA256: `10b5e4d4b83c0c9738c7007cf89c5d8487e8036463e8865d36513fe365128f18`.

Private staging, activation and graph-replay evidence: `/db/pankagent-vnext-private/operations/language-advice-20261003/`. Rollback release: `20261003-cohort-fixes-0a2e8c6/backend`.
