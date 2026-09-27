# Original-workflow evaluation — 2026-09-26

This is an isolated backend evaluation, not a deployment. The original first-valid-result workflow is retained. The removed competing-candidate workflow and model split are not restored.

## Reproducibility and limitations

- Baseline code: `2994f1783e536265a3272378179a09cb2527f92a`.
- First improved replay: `6ce8333`, frozen independently from subsequent fixes.
- Follow-up runtime: `4849d0d`, schema pack 2.1.1. Only its selected follow-up/transfer cases establish paid end-to-end evidence for this revision.
- Graph: `PanKgraph_08_04`; runtime model: `claude-sonnet-5`; same GPU service; literature disabled equally. Each case uses a warm inventory and reset query cache.
- Both complete arms are baseline-first, not alternated. The manifest, per-case audit, token usage and timings are preserved. Timing is descriptive, not an isolated causal estimate.
- OpenAI `gpt-6-sol` performs blinded advisory review only. Core coverage, correct empty outcomes, prose quality and unresolved scope are separate; an advisory `usable` label is not a release verdict.
- Q17/Q57 require unresolved scope decisions. Q52/Q55 are evaluated as expected-empty references; nonempty prerequisite populations do not invalidate an empty final target.
- The 32 transfer questions are frozen scope-preserving paraphrases. They test wording robustness, not unseen-entity generalization. Runtime code never reads gold fixtures.

## Offline and database checks

The initial focused suite passed 427 tests plus 38 subtests. Follow-up focused tests passed 320 plus 29 subtests. Three formatter/viewer historical source guards passed locally. The broad suite is not all green: 19 failures reproduce unchanged on the frozen baseline and are listed in `baseline-failures-20260926.json`; git-history guards require the local repository rather than the remote source archive.

The interval-overlap query returned 85 edges and 87 nodes, exactly matching an independently written complete Neo4j reference. The follow-up also passed the full GraphAdapter execution/EXPLAIN path. This proves that retrieval case, not general end-to-end coverage.

## Remaining engineering work

The implementation supplies reusable interpretation, template, acceptance and numerical-fact improvements, but does not make every reference question work. Scope/role planning remains the principal boundary for shared-partner tasks and donor/assay combinations. Formal GO domain vocabulary can still be resolved as an ontology root. Formatter prose can still conflate independent evidence-branch counts despite receiving authoritative facts. These are distinct from Cypher generation failures.

Database declarations newly introduced or migrated by this patch live in the four canonical schemas. The documented untouched legacy knowledge inventory still needs parity-preserving migration; this is not a claim that all historical Python knowledge has been eliminated.

Live activation remains a separate decision. The 46/57 supported-answer target, preservation of prior successes, and repeat/transfer performance gates must be reviewed against the completed results below.

## Full frozen 57-case comparison

| Measure | Original | First improved |
|---|---:|---:|
| Core covered | 32/57 | 38/57 |
| Verified empty requested target | 1 | 0 |
| Preview available | 37 | 43 |
| Advisory usable answers | 32 | 38 |
| Matched preview median (34 cases) | 8.78 s | 5.49 s |
| Matched preview p95 | 28.76 s | 20.05 s |
| Mean API cost per attempted question | $0.0793 | $0.0756 |
| Planning cost per attempted question | $0.0473 | $0.0389 |
| Formatting cost per attempted question | $0.0313 | $0.0358 |
| GPU generation calls | 85 | 14 |
| Database retrieval attempts | 69 | 77 |

Mean cost includes failed attempts and queries that never reached formatting. Across the 34 matched preview cases, mean total API cost was $0.07480 versus $0.07458: effectively unchanged. Reduced planning work paid for more formatting. API cost excludes GPU hosting and Neo4j capacity. More distinct database reads were performed, despite fewer GPU requests.

Core gains: **Q07,Q08,Q09,Q10,Q20,Q31,Q35,Q46**. Core regressions in this first full replay: **Q18,Q45**. The original correctly empty **Q55** also regressed to preparation failure. These are single replay observations; repeated controls were not completed. The follow-up corrects some causes but cannot erase this initial result.

The advisory score of 38/57 is not a verified scientific pass rate. In particular, the review flags branch-count conflation in Q34, unsupported domain assignment in Q40, omitted ductal-subtype evidence in Q26, incomplete assay coverage in Q50, and sample-dependent donor counting in Q53. Core presence alone would miss several of these problems.

## Failure ownership and next priorities

| Boundary | Cases | Observed failure / next repair |
|---|---|---|
| M03/M04 request roles | Q11,Q12,Q39,Q52 | Instruction/statistical/domain words collide with entity aliases. Q11/Q12 were targeted by the separate follow-up; formal GO domains and exclusion vocabulary remain. |
| M04 scalar bindings | Q18,Q45 | LLM singleton arrays were not scalar property values. Follow-up normalizes only fields with verified scalar ownership. |
| M04/M06 dependency roles | Q37 | Shared-partner preparation fails focus-scope validation before execution. Needs a verified multi-anchor task/dependency contract, not relaxed global filters. |
| M04 annotation/population scope | Q26,Q42,Q50 | Partial subtype/assay families or no executable identity-only task. Reviewed recipes are available but do not reliably determine the proposed task. |
| M04 clinical/assay authority | Q51,Q54,Q55,Q56 | Source/stage/diagnosis and assay scope are incorrectly dropped or treated as conflicting. These must be corrected by owner-aware intent compilation, preserving immutable request authority. |
| M06–M08 coloc/overlap | Q12,Q14,Q19 | Full signals and their respective leads must remain distinct; overlap query needs its exact verified join. Follow-up checks retrieval separately. |
| M11/M12 answer use of evidence | Q14,Q34,Q36,Q40,Q53 | Formatter misses or misstates facts despite returned evidence. Add focused structured fact-use checks before claiming prose correctness; do not add another unrestricted writer. |
| Reference scope | Q17,Q57 | Unspecified interval/source remains unresolved. Current generic failure is weaker than the planned targeted clarification behavior. |

**Release gates are not passed.** This round demonstrates useful retrieval/speed changes, but not 46/57 supported answers, preservation of all prior successes, or broad wording robustness. Keep the change on the review branch.

## Separate follow-up: final runtime revision

Eight preselected follow-up cases were run on `4849d0d`: **7/8 core-covered**. Q11,Q14,Q18,Q19,Q56 now cover their core; Q01/Q07 retain core. Q12 returns the primary coloc answer but still misses required reference membership. This subset is not a new 57-case score, and combining the best outcome from multiple attempts would overstate performance.

Answer review still finds errors: Q01 denies the schema's lead-signal convention; Q11 miscounts a credible-set group (six instead of seven); Q18 says 5/7 higher non-diabetic medians, while the records support 4/7 with one tie. Q14,Q19,Q56 have no material error flagged by advisory review, but that does not substitute for a complete manual acceptance review. These observations specifically show that providing deterministic facts does not yet guarantee that formatter prose uses them correctly.

Follow-up API cost: **$0.6801 Claude**. All eight attempts are retained, including the failure. The full final revision still requires an equally controlled complete replay before deployment.

## Transfer and final budget

The hard budget stopped the transfer run after **6/32** preselected paraphrases: T01,T06,T13 core-covered; T10,T11,T14 did not. Thus **3/6 core coverage** on the attempted subset. This small, deliberately prioritized subset is not an overall generalization estimate. The remaining 26 paraphrases and repeated controls were not run. No final-revision full57 replay was purchased.

Final settled accounting: **Claude $9.862252/$10; OpenAI $1.971788/$10**, with zero pending calls/reservations in both ledgers. The runner stopped with $0.137748 Claude remaining rather than beginning another uncertain-cost question. OpenAI allowance cannot be transferred into the Claude ceiling. These are application-ledger cost estimates, not an independently reconciled provider invoice.

## Deliverables and decision

Runtime changes, four-schema changes, focused tests, the frozen 32-question transfer fixture, isolated runners, compact case results, and updated draw.io are saved on `codex/workflow57-review`. The full evidence archive stays in the local ignored report directory, not Git. Frontend and live services are unchanged.

**Keep this revision for review; do not activate it yet.** The full-arm speed/retrieval gains are useful, but the preserved-success and supported-answer gates remain unmet. Prioritize clinical/scope task compilation and structured formatter fact-use checks before adding more Cypher alternatives or another agent. Legacy schema migrations and the uncompleted transfer/repeat/manual acceptance work are explicitly outstanding.
