# Original-workflow backend improvements

Baseline: `2994f1783e536265a3272378179a09cb2527f92a`. This change retains the original preview/confirmation lifecycle and first-valid-result execution. It does not deploy a service, alter frontend code, or restore competing candidates.

## Module changes

| Boundary | Change | Guard |
|---|---|---|
| M03 | Coalesce inventory refreshes; a caller timeout no longer cancels the shared refresh. | Expired inventories are never returned; release identity is rechecked before publication; shutdown cancels refresh. |
| M04 | Carry current recorded-value tool observations into final preparation; expose relevant structural recipes and reviewed population groups. | Tool facts cannot authorize filters; matching release/pack required; searched values are not a complete inventory. |
| M04 | Distinguish statistical phrases from gene aliases, preserve clinical negation, avoid interpreting sample grammar as tissue. | Explicit unknown tissue and negative-source tests retained. |
| M04 | Do not automatically copy a unique focus identity into an independently worded partner task. | Existing role, dependency and request-proof validation remains authoritative. |
| D7/M06a | Schema-owned template/cache-first routing; bounded GPU fallback. | The forced GPU participation override now obeys schema 2. |
| M06a | Parameterized same-assembly gene-body/peak overlap with independently verified anchor and cell identities. | Exact structural-template guard; no regulatory relationship inferred; no arbitrary radius; normal EXPLAIN/materialization limits. |
| M06 dependencies | Separate referenced GWAS and QTL lead identity extraction. | Full recorded values, bounded IDs, runtime identity lookup; malformed references fail explicitly; primary coloc remains independent. |
| D8/M08 | Suppress identical admissible reads; check execution completion and materialized endpoints. | Verified empty accepted; rejected/partial reads cannot become exhaustive claims; existing path checks retained. |
| M09/M11 | Full-record numeric facts, distinct typed node counts, branch counts, decimal top-k, paired comparisons. | Facts precede compaction; ties/zeros/missingness retained; partial facts explicitly describe only the retrieved subset; every comparison preserves record identity/context. |

The partner/path and donor/sample families reuse existing bounded-path, typed-dependency and deterministic-combination operators. No additional planning agent or unrestricted cloud Cypher writer was added. Roles and recipes are now visible to the planning tool context rather than only the GPU context.

## Four-schema ownership

- **Schema 1** owns the migrated measurement, numeric, categorical, coordinate and runtime-inventory declarations.
- **Schema 2** owns routing, interval retrieval and referenced-entity recipes.
- **Schema 3** owns interpretation, statistical terminology, reviewed ductal-population meaning and requested numerical operations.
- **Schema 4** owns result checks, repair guidance and the retained release-scoped structural budget hint.

Cross-reference validation checks numerical operation fields, measurement fields, interval field owners/endpoints and referenced-entity recipes. Compatibility views remain generated in memory. `graph_patterns.py` consumes schema 2 directly; no independently editable `graph_patterns.json` is introduced.

The existing measured three-branch interaction/annotation budget allocation is preserved as a schema-4 hint, with its historical provenance. It has no entity IDs, but its exact structural shape is deliberately narrow. A generic learned fan-out policy is not claimed here.

Legacy knowledge still embedded in untouched modules (not newly introduced): genomic-region metadata query, ranking-contract field mapping, signal grammar/aliases, semantic-registry assay parsing and specialized bounded-path drafting. These require separate parity-preserving migration; this patch does not claim a wholesale rewrite of those components.

## Validation artifacts

- `tests_vnext/test_incremental_original.py`: precision, empty/partial acceptance, duplicate-read suppression, role binding, property-tool provenance, cancellation/refresh, clinical grammar, referenced leads and interval guards.
- `tests_vnext/fixtures/acceptance/workflow57.json`: unchanged reviewed 57-case suite.
- `tests_vnext/fixtures/acceptance/workflow57-transfer32.json`: frozen scope-preserving paraphrases with inherited reference provenance. This tests wording transfer, not unseen-entity generalization. The runtime never reads either fixture.
- `scripts/acceptance/original_workflow.py`: isolated single-arm runner, frozen manifests, cumulative budget ledger, interruption marker, per-case audit and accounting. An interrupted unrecorded attempt blocks automatic resubmission rather than risking duplicate charges.
- `scripts/acceptance/original_judge.py`: blinded advisory OpenAI review with a separate capped ledger; model opinions are not an automatic acceptance verdict.
- `original-workflow-20260926.drawio`: updated page 01. Error/Schema annotations, cell IDs, connections and geometry preserved; model descriptions are adjacent and dark purple.

Direct Neo4j replay of the overlap template returned 85 edges and 87 nodes without truncation, exactly matching independently formulated overlap membership. This establishes retrieval correctness for that replay, not end-to-end planning success.

The focused regression set passed 427 tests and 38 subtests. Three historical formatter/viewer source guards also passed locally. Broad-suite failures are being compared with the frozen baseline and are recorded separately from changed-path tests.

## Evaluation boundaries

This round has explicit **$10 Claude and $10 OpenAI cumulative limits**, stored in new round-specific ledgers. No earlier ledger is reset. Original and improved agents run sequentially, with the same model, graph, warm inventory and disabled literature. Query caches reset per case. The fixed baseline-first order is a limitation; repeated alternating controls are subject to remaining budget.

Core coverage, usable supported answers, verified empty outcomes and clarification are separate measures. Q17/Q57 remain unresolved-scope references; Q52/Q55 require correct verified-empty behavior. A larger result never establishes improvement. The 46/57 supported-answer target and latency/cost guardrails are release gates, not presumed outcomes.

No live activation is included. The paid evaluation report must establish the actual outcome and identify any unmet gates before a deployment decision.
