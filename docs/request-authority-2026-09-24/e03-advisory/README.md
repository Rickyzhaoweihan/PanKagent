# E03 advisory identity interpretation

Implemented on the existing `Ringo` branch after the user clarified that Python
helpers must have lower authority than Claude on interpretation. No branch was
created. This candidate has not been deployed.

## Behavior

- An unmatched retained identity proof no longer throws an E03 planning exception.
  It becomes a non-blocking warning and the proposed tasks reach normal preparation.
  An unused suggestion can be omitted without consuming repair turns.
- Claude can map a user abbreviation to a verified canonical lookup with different
  wording. The backend authenticates the typed identity evidence, preserves the
  original lookup phrase, records Claude's interpretation separately, and binds
  that interpretation to the current question. It does not claim the abbreviation
  is itself a recorded synonym.
- A missing literal-name match is guidance to resolve an expanded name, not a
  reason to drop the requested entity constraint. No additional model, stage or
  retry allowance was added.
- Database existence, typed IDs, release/schema identity, query execution checks
  and requested constraints remain checked. A model guess is not converted into
  a database existence proof. Historical failed runs retain their original state.
- Tab 01 calls E03 advisory and clarifies Claude's interpretation authority in the
  existing M04-tool/D02 components. No component moved; connector geometry and
  tabs 02–04 are unchanged. The exported tab was visually inspected.

## Validation

The 33 focused tests pass, including canonical lookup → abbreviation selection,
proof tampering, changed question, missing database IDs, non-blocking unused advice,
diagnostic propagation, raw question context and formatter input boundaries.
The full offline run has 2,783 passes, 231 subtest passes, five skips and the same
19 baseline failures; no new failure was introduced.

The first live replay passed E03 but exposed a subsequent lost-constraint/query
validation failure. After correcting generic abbreviation-recovery guidance,
“What is T1D?” passed in two successive live replays: one complete disease node,
zero edges, no donors, and a supported definition/provenance answer. Streamed
answer reconstruction matched. The final replay took 18.964 seconds and cost
$0.0459378. These are targeted results, not a full performance acceptance claim.

All live calls use the existing cumulative $10 ledger. Runs and full model/query
traces remain in service-owned operations storage. No paid allowance was reset.
See the accompanying aggregate reports and `checks.json` for the retained evidence.

The final independent checks remain below release acceptance:

- HPAP ND samples returned the complete reference membership (5,480 sample IDs),
  but answer generation timed out and stored deltas did not reconstruct the final
  fallback answer. Retrieval passed; streaming/answer acceptance did not.
- The CFTR pancreas splicing-QTL question retained complete coloc and GWAS
  evidence and produced a partial answer. Its QTL-detail task still failed, so the
  required full-scope example is not accepted.
- Performance controls remain pending. No serving service was restarted.

Final ledger snapshot: **$6.634773 settled**, **$0.213457 reserved**, **$3.151770
remaining**. One timed-out provider call lacks settled usage; its reservation was
kept intact, not cleared or treated as free. This turn added approximately
$0.373367 settled cost plus that reservation.

E03 is no longer a deployment blocker by itself. The broader release still needs
its remaining correctness, streaming and performance gates; this change does not
reclassify unrelated failed checks as successful.
