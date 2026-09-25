# Workflow comparison — 2026-09-25 (partial live evaluation)

**Keep the original workflow enabled for now.** The competing-candidate flow produces an earlier first preview, but takes longer to settle, costs more, and loses usable evidence in a demonstrated annotation-conflict case. The feature flag remains off; no dev deployment or frontend changes were made.

## Coverage and limits

The repo now contains a [50-question suite](../../tests_vnext/fixtures/acceptance/workflow50.json): all 14 concrete current landing examples, 20 existing regression questions, and 16 historical questions. Every case has provenance and an independent reference query or an explicit manual-review rubric. All 50 reference preparations completed without model calls or reference errors.

The corrected process attempted all 100 arm/question combinations. **40 questions have valid paired tests**, including all 14 current examples. Four follow-up attempts used a defective harness context setting; 15 attempts were blocked by the cumulative API budget. Those 19 attempts are excluded from workflow comparisons, not counted as model failures. One additional candidate run (beta-cell DEGs) is valid but unpaired because the original arm's formatter was budget-blocked. Ten questions therefore still need valid completion. This is not a completed 50-question live benchmark.

Both arms use the same Sonnet 5 planner/formatter and graph release. The baseline is the existing pipeline with the flag off; the candidate uses the flag on at backend commit `5cd7234`. One run per arm/question, alternating order, isolated session stores, planning/query caches disabled/reset, shared warmed entity inventory, provider prompt caching retained. Literature was disabled equally. A terminal `partial` status alone is not a graph failure. See [methodology](benchmark50.md).

## Comparable results

All quality/cost/work rows use the same 40 valid paired questions. Timing rows use the 30 questions with confirmable settled previews in both arms. Time excludes browser rendering and user think time; confirmation occurs after candidate settlement. Early confirmation could cancel work that this benchmark intentionally allowed to finish.

| Measure | Original | Competing candidates |
|---|---:|---:|
| Published previews | 32/40 | 31/40 |
| Confirmable previews and written answers | 32/40 | 30/40 |
| Exact reference membership **and completeness** | 11/25 | 9/25 |
| Median first preview, matched 30 | 14.16 s | 10.78 s |
| Median settled preview, matched 30 | 14.16 s | 16.47 s |
| Median written-answer time, matched 30 | 22.45 s | 25.90 s |
| Mean planning/assistance API cost per question | $0.0507 | $0.0612 |
| Mean formatting API cost per question | $0.0316 | $0.0291 |
| Mean total API cost per question | $0.0823 | $0.0903 |
| Total API cost, same 40 questions | $3.2926 | $3.6107 |
| Backend evidence reads | 53 | 94 |
| GPU generation requests | 69 | 118 |

The new flow's first preview is about 24% faster, but settlement is 16% slower, written answers 15% slower, and API cost 10% higher in this pass. It makes 77% more downstream evidence reads and 71% more GPU requests. GPU-service internal database work is not included in these read counters; infrastructure cost is not priced.

API costs are configured token-price estimates, not provider-invoice reconciliation. Failed attempts are included in per-question cost. Formatting means are per attempted question: the new arm produced fewer answers, so its lower formatting subtotal is not evidence of a cheaper formatter. The formatter and model are unchanged.

Exact-reference scoring is intentionally stricter than a useful overview: bounded GO overviews that disclose their limit do not pass a full-membership reference. Fifteen paired questions have supporting/manual or clarification scoring rather than an exhaustive membership score. Both arms fail the one predeclared ambiguous-source clarification case. These scores are not a single measure of biological answer correctness.

## What changed answer quality

1. **New-flow regression: sample/tissue conflict.** The template candidate retrieves the correct sample plus tissue annotations; the GPU candidate returns the same donor/sample scope without those annotations. Comparing the entire graph as membership creates a conflict and withdraws useful sample evidence. Original answers the count/tissue request; new retains donors but loses the sample answer. Compare primary task membership separately from required annotation coverage before activation.
2. **New-flow confirmation failure.** PLEKHM1 signal investigation publishes a partial preview but confirmation returns 409/`preview_revalidation_required` with `checked_evidence_expired`. Original preserves the useful QTL branch. Displayed preview counts alone conceal this failure.
3. **Shared M09 defect: CFTR/ADCY3 intersection.** Both return zero shared partners, while the independent reference has seven. Both atomic result sets already contain those exact seven overlapping Gene IDs. Undirected interaction retrieval is combined using a directional `target` selector. Extra candidate generation does not fix this selector failure; original discloses unknown scope, while new overstates the zero.
4. **Shared M04 failures.** Stage/cohort negation, mixed-case source phrasing and connected-path questions often fail before either M06 route starts. Separate stochastic plans can fail differently under the same M04 code. Do not attribute every end-to-end difference to the new coordinator.
5. **Mixed scientific phrasing quality.** New is clearer on the CFTR one-versus-rest enrichment contrast in one example; original is clearer on lead-variant status and detection-versus-enrichment distinctions in others. Several summaries confuse log scales, median versus pooled percentages, or partial versus exhaustive annotation retrieval. No consistent prose-quality win is established by this single pass.
6. **Membership is not summary accuracy.** In the unpaired beta-cell DEG run, new retrieves all 969 reference genes, but incorrectly calls all of them downregulated. Full records contain **596 upregulated and 373 downregulated**. This serious synthesis error is recorded separately and is not used as a paired workflow win/loss because the original formatter was budget-blocked.

All 41 questions with at least one valid run (40 paired questions plus the unpaired beta-cell case) have qualitative review entries; reviews inspect scope, requested evidence and important numerical/interpretive claims, not an external revalidation of every biological statement. [Per-case reviews](benchmark50-reviews-2026-09-25.json) and [metrics](benchmark50-case-metrics-2026-09-25.json) preserve the distinctions.

## Budget and incomplete work

- Corrected comparison attempts: **$7.2775** settled, including excluded attempts.
- Initial pilot: **$0.7226** settled, plus an unresolved **$0.1739 reservation bound** retained conservatively.
- New work in this task: about **$8.00 settled**; earlier work in the existing ledger was $1.6795.
- Cumulative ledger: **$9.6795 settled + $0.1739 held**, leaving **$0.1466** under the authorized $10 ceiling. That remainder cannot reserve the required model calls. The request to raise the cumulative ceiling to $12 has not been approved.

The pilot found a missing assistance-schema-validator import and false collect-versus-native-row conflicts. Those two defects were fixed before the corrected comparison, with regression tests; the pilot is not pooled into outcome metrics. The follow-up context bug and failed-confirmation snapshot bug were corrected in the harness/scorer afterward without changing the tested backend. Original raw artifacts are retained, and excluded attempt costs are not erased. The runner now stops after an observed API reservation failure rather than continuing budget-blocked attempts.

Remaining case keys: `followup`, `history-000005`, `history-000054`, `history-000067`, `history-000071`, `history-000074`, `history-000076`, `history-000077`, `history-000105`, `history-001394`. Nineteen arm/question attempts need valid completion. Fresh follow-up tests must include/replay their intended parent context, not inherit a session polluted by the invalid no-context follow-up. Preserve already-valid outcomes and do not pick the best of repeated runs. No additional paid evaluation or activation is authorized by this report.

## Reproduction and next decision

Use [the runner](../../scripts/acceptance/workflow50.py), [offline viewer/scorer](../../scripts/acceptance/workflow50_report.py), frozen manifest and the same graph/model settings. `--case-keys` supports a bounded fresh subset and requires explicit parent cases; `--resume` preserves completed attempts of a stopped run. They never reset the cumulative ledger. Raw plans, graph records, events and database output stay outside Git; the [result manifest](benchmark50-results-2026-09-25.json) includes hashes of retained artifacts.

Before enabling the new flow: fix annotation coverage versus primary-membership conflicts, confirmation revalidation, and undirected combination selectors; then replay affected cases plus the unfinished suite. Preserve the original as the default during that work. Broader repeated timing tests are needed before claiming a stable speed ranking.
