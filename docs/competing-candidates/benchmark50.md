# Fifty-question workflow comparison

`tests_vnext/fixtures/acceptance/workflow50.json` contains all 14 concrete current landing examples (16 displayed occurrences before deduplication and configured substitutions), 20 existing regression questions, and 16 questions selected from the historical replay corpus. It preserves the selected wording and records source hashes/IDs. The two follow-ups have explicit parent cases. It contains questions and reference-query definitions, not database records or credentials.

This compares the original workflow (`PANK_VNEXT_COMPETING_CANDIDATES=0`) against the implementation at `5cd7234` with the flag on. Both arms run the same code, model (`claude-sonnet-5`), schema, graph release, constraints, formatter and resource limits; the flag isolates the execution change. This is not a GPT-versus-Claude comparison. No service is deployed or restarted.

The first live pass uses one run per question per arm, 100 attempted runs if the budget permits. Cases run sequentially; which arm runs first alternates by question. This limits cross-arm database/GPU contention. Planning caches are disabled, query caches reset before each question, and entity indexes warmed equally. Provider prompt caching remains active and usage is recorded; the alternating order reduces, but cannot eliminate, cache and temporal effects. One repetition is exploratory evidence, not a statistically stable latency ranking.

## Run

Use an isolated directory owned by the service account, with `umask 077`. The environment file stays private. The cumulative ledger **must already exist**, and its authorized ceiling must not be reset or raised without a new budget instruction.

```sh
python scripts/acceptance/workflow50.py \
  --root /protected/evaluation/run1 \
  --env /protected/runtime.env \
  --ledger /protected/existing-authorized-ledger \
  --ceiling 10 --references-only

# Same arguments without --references-only runs both arms.
```

The runner freezes independent Neo4j references before any model calls. It refuses changed manifests or overwriting an existing comparison. `--limit` permits a smoke subset, which must never be reported as the full 50-question result. Raw plans, full backend evidence, answer text, events and audit records stay in the protected output directory. `summary.json` updates as each attempt finishes. A budget stop is recorded as incomplete. After the process has stopped, `--resume` explicitly continues only unfinished attempts, checking the manifest, graph/schema identity, model and runtime settings. It retains the original budget snapshot and records each continuation. Changing `--ceiling` requires user authorization; the ledger is never reset. A process lock prevents concurrent writers using this runner version.

## What is measured

- First usable preview, settled preview, and end-to-end answer latency. The candidate arm is allowed to settle before the latest plan ID is confirmed; this measures the extra work that early user confirmation could cancel.
- Verified preview availability and retention through settlement; exact reference membership and completeness for cases whose requested scope has an exact oracle.
- Query generation requests, EXPLAIN checks, downstream database reads, metadata reads and their durations. GPU-service internal executions are not counted by these downstream counters; that service performs its own execution/selection.
- Settled API cost, outstanding reservation bounds, stage-specific input/output/cache usage, and assistance claims. These are the repository's configured token-price estimates, not a separately reconciled provider invoice. GPU/database infrastructure cost is unpriced and reported as work.
- Candidate selections, replacements, conflicts, preview versions, and stream reconstruction.

Successful-only timing tables include their denominators and matched-pair counts. First preview means publication, not a guarantee that confirmation will succeed. Matched timing comparisons require both arms to reach a confirmable settled preview; failed confirmations are excluded from that timing subset. Costs include failed attempts; a missing answer is not a cheap successful answer. Missing or unexecuted evidence cannot pass a verified-empty check. Larger membership is never rewarded. The predeclared ambiguous-source clarification case is scored separately from exact membership; a generic planning failure is not a successful clarification.

Open-ended frontend questions also have supporting-evidence references, marked separately from exact-answer oracles. For example, a lead-QTL question does not require reporting every credible-set variant. Interpretation questions and the bounded OCR question require answer review against the case rubric, the original question and returned evidence. Exact membership alone does not establish answer quality or valid causal language.

Two old reference scopes were corrected before freezing: clinical ND/T1D comparison uses `diabetes_type`, not disease provenance; ductal enrichment uses the recorded ductal identities rather than an unrestricted anatomical endpoint reference. Do not compare the resulting scores directly with earlier benchmark scores that used different references.

## Local checks

```sh
python -m pytest -q tests_vnext/test_workflow50_benchmark.py
```

The checks cover complete landing-example coverage, parent ordering, typed endpoint extraction, rejection of unexecuted empty results, and fair treatment of failed attempts in timing/cost summaries. Acceptance still requires the live results and answer review.

The initial pilot at `79f9720` exposed an undefined assistance-schema validator and false conflicts caused by collect-versus-native graph row shapes. Commit `5cd7234` fixes both, with regression tests. Retain the pilot separately and include its spend; do not pool its outcomes into the corrected 50-question comparison.

## Offline answer viewer and scoring corrections

```sh
python scripts/acceptance/workflow50_report.py /protected/evaluation/run1 /local/report/comparison.html
```

This creates a standalone paired-answer viewer plus `scored-summary.json` and `scored-cases.json`. An optional adjacent `review-annotations.json` adds human-readable qualitative review. Keep raw graph records and credentials outside Git; only sanitized aggregate/per-case metrics and reviews belong in the benchmark report.

The initial live harness passed parent session IDs with `include_context=false`. Its two contextual cases are invalid comparisons and must be repeated with context enabled; the reporter excludes those attempts automatically while retaining their cost separately. The runner now enables context only for declared follow-ups and requires the parent run. It also captures the terminal state after a rejected confirmation; the offline reporter recovers that state from persisted events for older artifacts without rewriting them. These are harness corrections, not changes to the tested backend.
