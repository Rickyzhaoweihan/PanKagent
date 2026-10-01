# Flexible introductions: implementation and validation

Candidate implementation: `0f737194fce3a85b7977938f5573fe58c0781de5`, on the existing `Ringo` branch. **Not deployed.** The frontend, live dev service and planning/confirmation API shapes are unchanged. There is no additional agent fork, inventory task, or model stage.

## What changed

M03/M04/D02 record model-selected phrase roles against the original request before scope compilation. An illustrative example may be removed from derived scope; actual restrictions and verified bindings remain subject to validation. Later compilation uses the recorded effective scope and cannot silently restore the removed example. False example decisions that collide with explicit restrictions are rejected. Alias/identity proof comes from server-owned verified data, with token-boundary checks to prevent short aliases matching unrelated words.

Ordinary introduction requests retain verified identity and recorded description while the existing planner chooses relevant exploration. The automatic twelve-check expansion remains only for explicit comprehensive profiles. An empty plain introduction may recover through the verified identity draft and normal preparation; explicit requested evidence and unresolved filters cannot be discarded this way.

M06–M08 use existing templates and Cypher generation for zero or more genuine filters, including scalar outputs. Sample-only queries need no automatic donor join. Projection validation distinguishes requested endpoints from population restrictions, and donor classification guards apply to the actual property owner. Read-only, schema, binding, scope, completeness and execution limits remain in force.

M09–M12 retain full backend records and compute category values, deduplication, missingness and counts in Python. Formatter input separates lean schema definitions from live evidence. Shared context allocation now preserves smaller evidence branches within the existing overall size limit instead of failing on a temporary per-branch minimum. These changes do not guarantee that every model-generated sentence is correct; observed failures are reported below.

Database-specific fields, relationships, meanings and new generation/interpretation guidance remain in the four-schema pack, version 2.3.0. The generic backend consumes that guidance. The pack hash is `ac4c0c5c21974d2a759128a1622a70bc5a7b9f4fd8fc016beaf31b96e3980eca`.

## Test set and method

`tests_vnext/fixtures/acceptance/workflow57.json` retains its compatibility filename but now contains 67 questions. Q01–Q57 and all their references are structurally unchanged; Q58–Q67 append the approved general introductions. Separate core/extra nodes and edges remain. Category, cardinality, scope and schema assertions supplement membership checks; empty lists never pass automatically. New references were independently queried against PanKgraph_08_04; the preserved older references were not all reverified this round. The separate 18-question landing-page draft is unchanged.

Both paid arms use `claude-sonnet-5-5`, the existing `Qwen/Qwen3.8-27B` Cypher service, warm identity discovery, per-case query-cache reset, disabled plan cache, and `include_context=true`. Runs execute through the existing in-process API: preview, confirmation and synthesis. Scalar-only cases successfully use the same interfaces. Literature is unavailable in both arms, so an otherwise useful graph answer may have overall status `partial`. Timings exclude browser rendering. Database work below counts retrieval calls, not database CPU or all schema-discovery reads.

Baseline is the active dev source `ce7b61134a9f6ceb8048798fad58700f50b439bc`. The candidate was frozen before its final replay. Its evaluation archive SHA256 is `dda131b1867fc9dc300989ef2961dbce688f399d7dc2a394061ff2eb878b47c4`, application SHA256 `6db1feb66d25c41311105a608f891c42240520c011535515ce088254981556fc`. Intermediate tuning runs and repeats are retained separately, never substituted into first-attempt totals.

## Offline results

Final suite: **3,030 passed, 34 failed, 5 skipped, 231 subtests passed**. All 34 failure IDs reproduce on the unmodified baseline; no new offline failures. This is not a clean-suite claim. New tests cover phrase-role pairs, alias boundaries and verified bindings, actual tissues/assays/exclusions, identity recovery, scalar deduplication/missingness, empty targets, timeouts/truncation, property ownership, context allocation, and budget stops.

## Original 57: first-attempt comparison

| Measure | Deployed baseline | Candidate |
|---|---:|---:|
| Required reference coverage | 47/57 | 47/57 |
| Written answers | 50/57 | 50/57 |
| Median successful preview | 6.27 s | 7.25 s |
| Total API cost | $4.1398 | $4.6809 |
| Planning cost | $1.7892 | $2.0840 |
| Formatting cost | $2.3346 | $2.5969 |
| Query-repair cost | $0.0160 | $0.0000 |
| Retrieval calls | 86 | 95 |

The candidate costs 13.1% more in this replay and is slower by the overall successful-preview median. On the 49 cases where both produce a preview, medians are 6.21 versus 7.54 seconds; the median within-question difference is +0.41 seconds. These are single paired replays, not stable latency estimates. Average total cost per attempted question is $0.0726 versus $0.0821. Average planning cost is $0.0314 versus $0.0366, and formatting cost is $0.0410 versus $0.0456, across all 57 attempts including failures.

Q42 gains required-reference coverage and Q53 loses it. A baseline repeat succeeds on Q42 and fails Q53 preparation, so the change cannot be credited/blamed from a single comparison. Q53 also produced an incorrect sample-linked subset in the intermediate candidate; the requested population includes donors without samples. This remains a release concern regardless of attribution.

Required-reference coverage is not answer accuracy. Q52/Q55 produce verified empty answers and Q57 follows clarification, which the node/edge coverage number does not count as passes. Q12 can answer the core colocalization question while missing additional required variant/membership references. Answer review also found wrong optional GWAS role assignment (Q16), numerical prose errors (Q18/Q30), and denial of an executed overlap test (Q19). Repeated-check outcomes are recorded separately below.

## Ten new questions: first-attempt final-candidate review

Seven answers are fully grounded and within expected scope: **Q58, Q60, Q61, Q63, Q64, Q65, Q66**. Q60 and Q66 disclose optional evidence failures/truncation; those do not invalidate the useful introduction. Q64/Q65 are accurate but too dense for a short introduction.

| Case | Outcome / remaining issue |
|---|---|
| Q58 | All 14 recorded sample categories; illustrative scRNA-seq does not narrow the query. |
| Q59 | No usable answer: generated scalar projection omitted DISTINCT and hit the 1,000-row limit. Scope interpretation is correct. |
| Q60 | Grounded general overview distinguishes schema descriptions from live evidence; optional failed checks disclosed. |
| Q61 | All eight independently verified tissue IDs/seven names present; tissue versus cell/region categories distinguished. Manual pass, strict automatic category assertion remains false because the query projected IDs/names without the category property. |
| Q62 | Accurate donor/sample source slices, but incomplete for an all-PanKgraph source question; whole-scope assertion fails. |
| Q63 | Both HPAP and stage-3 restrictions retained; 3,064 samples, 43 sample-linked donors and 14 categories independently recompute. |
| Q64–Q66 | Verified INS, MDA5/IFIH1 and T1D introductions with relevant graph evidence. |
| Q67 | Useful CFTR overview, but falsely describes metadata omitted by context compaction as unavailable in retrieved data. Grounding review fails. |

Strict automated coverage is 5/10; it deliberately leaves manual scope/schema assertions unresolved. Manual review does not rewrite that score. Nine answers were emitted; seven meet the grounding/scope review above. New-question cost is $1.0281, with median successful preview 15.47 seconds and 40 retrieval calls. Broader exploration adds work; it is not a speed optimization. These ten new cases were evaluated on the candidate; no paired baseline gain percentage is claimed for them.

## Paired controls, repeats and budget

All four independently reviewed held-out controls pass:

| Control | Verified behavior |
|---|---|
| Original screenshot wording, “like scRNAseq…” | All 14 recorded modality labels; no assay restriction. |
| Spleen, “like scRNA-seq” | Spleen retained; six complete modality labels, 1,991 samples. |
| Spleen, “only scRNA-seq” | Both restrictions retained; complete verified empty result. The frozen spleen proof contains no scRNA-seq. |
| Excluding scRNA-seq | Exclusion retained; 13 categories, 8,908 unique samples; all category counts recompute. |

Automatic held-out score is 3/4 because the empty case requires manual execution proof; the score was not overwritten. These four controls cost $0.2757. They verify scope handling, not every possible broad-question wording.

On one final-candidate repeat each, Q16 avoids the wrong optional GWAS branch, Q18 corrects its numerical directions, Q30 avoids the incorrect ratio, Q53 returns the full 96-donor population, and Q59 uses DISTINCT and returns all 14 categories. Those successes show variability; they do not erase the first-attempt failures. Q53 keeps its 95-donor sample context separate from the 96-donor primary population.

Baseline repeats are clean on Q16 and Q30, but Q18 makes the same four-versus-five arithmetic error seen in the candidate's first attempt. Both versions again deny the executed coordinate-overlap check on Q19. This is a reproducible shared synthesis/provenance weakness. The small paired sample did not establish a reproducible new-version loss in these cases; it also does not establish general reliability. The six candidate repeats cost $0.6480 and four baseline quality repeats cost $0.4548; earlier Q42/Q53 baseline repeats remain separate.

The **entire round spent $19.331238**, including development attempts, the intermediate 67-case pass, final replays and repeats. **$0.142740 remains reserved** for a call whose provider outcome is unknown. Total charged against the $20 allowance is **$19.473978**, leaving **$0.526022** available. No further paid calls were made. Old ledgers were not changed, and the unresolved reservation was not released. All model API spend in this round was through Sonnet 5.5; existing GPU service calls have no added cloud-model API charge here.

All 57 original cases, ten additions, four held-out controls and the ten final discrepancy repeats completed. The clarification continuation for Q57, larger repeated statistical evaluation, live literature integration, browser interaction and interactive native diagram inspection remain unverified. No activation is authorized by this report.

Aggregate measurements are saved in `flexible-introductions-20260930.json`. Detailed immutable reports, manual case reviews, references and rendered diagram are retained in the project home's `Research/Reports/pankagent-flexible-intros-20260930/`; raw run/audit records and the shared budget ledger remain in the service user's protected `/db/pankagent-vnext-private/operations/flexible-intros-20260930/` directory.

## Workflow diagram

`original-workflow-20260926.drawio` matches the project handoff `pankagent-tab01.drawio` byte-for-byte, SHA256 `09fa88d16340d105eb5b81bffdb2cee3cbe0be1c9790d032c422b8ad3cca18a8`. All 106 cells, 48 connectors, module IDs, geometry, execution panels and Error/Schema annotations are retained. LLM blocks retain “LLM API”; adjacent purple model descriptions show `claude-sonnet-5-5` and verified GPU configuration.

Native draw.io CLI PNG export was visually inspected for clipping/connectors/annotations. Interactive native-app inspection timed out and is not claimed. `diagram-provenance.json` records the hashes and candidate/not-deployed status.

## Release decision and next work

**Keep this candidate inactive.** Required-reference coverage alone is unchanged, but quality and reliability gaps remain. Before a deployment decision, address scalar truncation repair through the existing shared allowance; whole-graph source scope; donor population preservation; and formatter claims/arithmetic based on complete evidence. Preserve optional verified work and existing limits. Re-evaluate the affected pairs on a frozen candidate with a newly explicit budget. Do not infer success from a larger result or one passing repeat.
