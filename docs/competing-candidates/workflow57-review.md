# Reviewed 57-question comparison

The editable fixture is `tests_vnext/fixtures/acceptance/workflow57.json`.
It retains the user's 57 standalone questions and sequential IDs. No frontend,
backend runtime, deployment configuration, or original benchmark result changed.

Core contains mandatory question evidence; extra contains optional relevant
exploration. Broad gene-set questions retain defining functions and the requested
annotation families in core, with other verified annotations in extra. Explicit
all-membership, top-10 and OCR-overlap tasks retain their full target sets.
Q34 preserves the user's exhaustive five-partner HLA-DRA core.
Q12 includes the GWAS lead and both QTL leads named by the ADCY3 coloc records.

| Case | Core nodes before | Core nodes now | Extra nodes now |
|---|---:|---:|---:|
| Q37 shared partners and annotations | 512 | 21 | 491 |
| Q38 ADCY3 pathways | 70 | 9 | 61 |
| Q39 TCF7L2 GO domains | 51 | 4 | 47 |
| Q40 CTLA4 functions/processes | 14 | 5 | 9 |
| Q41 PTPN22 GO | 58 | 5 | 53 |

## Comparison protocol

`scripts/acceptance/workflow57.py` reuses the original two-runtime comparison
runner with competing candidates disabled/enabled, the same Sonnet 5 model,
fixed limits, disabled literature, independent sessions and query caches, and
alternating arm order. It changes evaluation only, not application behavior.
Run against isolated processes, not by activating either workflow on dev.

The scorer checks mandatory node/edge inclusion and explicit relevant properties,
reports optional evidence separately, and never fails an answer solely for
additional nodes. It records both selected and atomic evidence, so combination
loss can be distinguished from retrieval failure. Core coverage alone is not
scientific correctness: scope, ranking, counts, wrong claims and distracting
exploration require per-answer review. Timing and cost include failed attempts;
paired-success latency is reported separately.

Q17 has no defined genomic radius or verified 50-peak reference. Q57 uses the
unresolved source name nPAP. Neither may pass based on an empty target list.
Q52 and Q55 have previously verified empty requested memberships; they require
executed relevant empty evidence and accurate reporting, not vacuous set matching.
Keep all 57 in the overall denominator; show reference-gap and verified-empty
categories separately. Eighty percent requires at least 46 usable answers.

The user authorized a new $10 Claude budget and a separate $10 GPT budget.
The completed run used $9.517674 for the two Claude arms and $1.483354 for
57 blinded advisory GPT-6 Sol reviews, with no pending reservations.
See `workflow57-results.md` and its machine-readable summary for results.
A new paid replay still requires an explicitly authorized remaining budget.

During evidence review, optional lead memberships (Q13–Q16) and cell/disease
context in broad introductions (Q42/Q44/Q45) moved from core to extra.
The graph-verified union is unchanged. Both arms were rescored equally offline;
`workflow57-core-refinement.json` records every change. Frozen scores remain
available. Failed/conflicted retained payload is excluded from verified coverage.

Example, after authorization and deployment of the test files to an isolated directory:

```sh
python scripts/acceptance/workflow57.py \
  --root /path/to/new/private-run \
  --env /path/to/protected/runtime.env \
  --ledger /path/to/authorized/ledger \
  --ceiling AUTHORIZED_CUMULATIVE_CEILING
```

The root and ledger paths must be persistent, owner-only locations. Never reuse
or overwrite earlier raw runs, reset their ledger, or pick the best repeated
attempt. Reference auditing is separate from paid execution; `--references-only`
is intentionally rejected because the inherited legacy checks are empty.
