# Cohort scope and same-sample query fixes — 2026-10-03

Implemented on existing `Ringo`, source commit `0a2e8c6e41c45dd1d65010007373d13ed993e0dd`. **Not deployed.** The frontend, dev service, original 67-question fixture and separate landing-page draft are unchanged. No new agent branch or model stage was created.

## Failures and repairs

The screenshot question was: “How many PLN scRNAseq samples from T1D stage 3 HPAP donors available?”

1. **Clinical phrases misread as source filters.** A source detector treated the first word after “samples from” as a dataset source, so “T1D” conflicted with the correctly resolved HPAP restriction. Source/tissue checks now recognize verified clinical phrase spans, preserving HPAP and stage while continuing to reject unknown sources, tissues and explicit conflicting filters. Multiword recorded sources and original-request ownership remain verified.
2. **Ordinary cohort queries represented as strict paths.** The saved failure was `missing_path_anchor_identity`. An equivalent isolated donor → sample ← tissue topology can now use the existing same-sample template. Its allowed topology and required identity/predicate roles live in `query_patterns.json`. Normalization preserves every predicate and original-path provenance. Required tissue identity, donor predicates and runtime authorization must be verified. Execution accepts only the compiled template and its parameters; a different GPU query cannot drop a join or broaden the population. Genuine paths, dependent roles and unsupported constraints retain their strict route. The original failed model proposal was not saved; offline tests initially used a representative proposal. The new paid runs also exercised actual model-generated paths.
3. **Recovery changed the request.** The former retry suggestion appended “return only complete connected paths.” The recovery response now retains the original question and uses the existing retry action, without inventing an additional constraint.
4. **Literal assay mistaken for pairing.** The positive control exposed a separate bug: the substring `multiom` made the recorded label `snMultiomics` imply a paired-assay request. Runtime-recorded labels and verified aliases now remain literal filters unless separate pairing/component wording is present. Explicit paired RNA/ATAC, separate assays, exclusions and capability expansion retain their distinct meanings.

Database-specific topology guidance is confined to the four-schema pack. The schema loader checks node types, directed edges and prerequisite ownership. Legacy extraction fixtures are no longer a second editable source for new query guidance.

## End-to-end results

Real Sonnet 5.5 calls used an isolated process based on deployed commit `53481a3`, with only these targeted fixes ported in. This preserves the deployed literature implementation and does not activate the previously gated flexible-introduction work. The canonical implementation remains on Ringo with schema 2.3.1; the isolated deployed-base patch used schema 2.2.1. Source hashes are recorded in the accompanying JSON.

| Question | Verified graph result | First preview | API cost |
|---|---|---:|---:|
| Exact screenshot question, scRNAseq | 0 matching samples | 10.43 s | $0.050034 |
| Same question with snMultiomics | 13 matching samples from 12 donors | 9.68 s | $0.120586 |

Both retained all four typed restrictions, used the ordinary template, exhausted the database cursor without truncation, and reused preview evidence after confirmation. Counts were independently checked against a read-only aggregate query on PanKgraph_08_04. Each returned sample in the positive control had both a matching donor and tissue witness; counting deduplicated sample identities. PLN resolves to the graph's reviewed pancreaticosplenic lymph-node proxy, UBERON_0015865. Exact scRNA-seq is not silently broadened to multiomics.

The positive run has a complete graph answer and overall status `partial` solely because literature was intentionally disabled in the isolated harness. Live literature and browser rendering were not tested. The two test fixtures have empty node/edge reference lists; these do **not** pass automatically. Explicit filter, membership, witness, count and completion assertions determined the results reported here.

The first development pass already answered the exact screenshot question, but its snMultiomics control failed before execution and exposed the literal-assay bug above. That failed attempt and its cost are retained, not overwritten. The harness initially needed evaluator import/custom-case grouping repairs; completed paid attempts were recovered rather than resubmitted by accident.

## Regression evidence and budget

- Final canonical suite: **3,129 passed, 34 failed, 5 skipped**, plus 231 passing subtests. All 34 failures also occurred in the untouched baseline. This is not a clean full-suite release pass.
- Untouched baseline: 3,025 passed, 39 failed, 5 skipped. Four extra baseline failures require Git metadata missing from the archive; one timing failure did not recur. These differences are not claimed as feature improvements.
- Targeted deployed-base compatibility tests: **159 passed**, with one introduction-specific test intentionally deselected because that feature is absent from this base.
- Offline lifecycle tests cover scRNAseq alias, literal scRNA-seq and literal snMultiomics, each with nonempty and verified-empty results, plus changed parameters, disconnected tissue joins, duplicate records and confirmation reuse. Genuine paired requests, unknown entities/sources, exclusions and missing proofs have separate checks.
- The full 67-case paid suite was not repeated in this targeted round. No claim of general release readiness is made.

New API spend was **$0.309933**, including initial development attempts and final checks. The existing $20 round ledger now records **$19.641171 spent**, **$0.142740 reserved** for the earlier unknown-outcome call, and **$0.216089 available**. No allowance was reset and no outstanding reservation released.

## Artifacts and deployment boundary

`cohort-scope-fixes-20261003.json` contains aggregate assertions, costs, queries and source hashes. Full run/audit records remain private under `/db/pankagent-vnext-private/operations/cohort-fixes-20261003/`; the shared budget ledger remains under `operations/flexible-intros-20260930/budget/`. Local logs and regression comparison are in the project home's `Research/Reports/pankagent-targeted-fixes-20261003/`.

The workflow diagram and identical project handoff copy retain module IDs, connectors, Error/Schema annotations, model labels and inactive/not-deployed captions. Provenance points to the source commit and this report. The rendered diagram was visually inspected; interactive native-app inspection is not claimed.

Dev remains at `53481a3` on Sonnet 5.5. The earlier introduction candidate remains inactive with its documented quality gaps. Deploying these fixes is a separate step; do not replace live wholesale with the canonical branch and accidentally activate the gated introduction work or remove the deployed literature change.
