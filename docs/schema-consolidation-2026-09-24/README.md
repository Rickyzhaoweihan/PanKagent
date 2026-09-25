# Schema consolidation candidate — acceptance incomplete

This is a reviewable candidate on the existing `Ringo` branch, **not an accepted
or deployed release**. The serving dev backend, production and results service
have not been replaced. Formatter output methods and rendering are unchanged.

## Implemented

- Four canonical JSON modules and a metadata manifest under
  `pankagent_vnext/agent_schemas/packs/pankgraph/`. Old consumer envelopes are
  derived compatibility views; the overlapping editable files are removed.
- Formal contracts, reference validation, immutable pack digest, and populated
  synthetic node/edge prototypes. General KG-standard remains v0.3.0 at
  `414c2732b5c8a50ba3a4d18561cfe12785a8b535`.
- Read-only extraction profiled all 19 node types, 24 relationship types and
  542 declared properties of `PanKgraph_08_04`, with no unknown definitions or
  failed profiles. Reviewed declarations and generated observations remain
  separate. The final artifact passed a whole-payload privacy check against
  9,180 protected IDs, including relationship annotations and endpoints.
- All 87 source entries from the three BIM files at
  `40cb7f5b08a2082a4f67ae7198591d92fa0c175d` have stable rules, applicability and
  coverage references. Original source bytes/checksums remain in the existing
  answer-skills bundle. Interpretation never authorizes extra clinical filters.
- Shared M04 schema inspection and property-value tools expose linked database,
  query and interpretation knowledge. Reviewed aliases retain provenance and
  still require live identity verification. D01 remains advisory.
- Scope repairs distinguish a stage label from diagnosis, reconcile a duplicate
  tissue display name with the verified tissue-ID relationship, and diagnose
  undefined combination roles before execution. Follow-up guidance names the
  actual saved population binding. Unspecified tissue is optional annotation
  on the same sample, not a required membership join.
- Tab 01 uses the four-schema numbering; M04 and its tools have Schemas 1–4.
  The attached draw.io file preserves retained geometry and tabs 02–04 exactly.

## Frozen evidence and regression gates

The current frontend source is `xuteng/react` commit
`b0a0cb99828d559fa6dc471cdf504cad2aecd095`: 16 displayed entries, 14 distinct
concrete questions after configured substitutions. The fixtures retain source
digests and every occurrence. No placeholder is submitted as a gene or SNP.

The regression manifest contains 51 correctness cases and 24 controls (eight
questions, three repetitions). Full typed memberships are kept in protected
service-owned storage, not this repository. Independent read-only references
were refreshed for 37 cases; frontend supported-answer review is an additional
requirement, not replaced by an answer-length check.

The actual deployed baseline (`6d7340d`, documentation head `4c723c7`) ran all
24 controls through plan → preview → confirm → answer. `baseline-controls.json`
contains only public questions and aggregate metrics. Per-run audit events cover
both planning and streaming synthesis and reconcile to **$1.272040** settled cost.
The existing cumulative $10 ledger was not reset: its balance after baseline is
**$2.662702 spent, $0 reserved, $7.337298 remaining**.

Baseline failures remain failures:

- The stage-1 paraphrase returned a false zero in all three repetitions.
- Exact sample/tissue wording failed in two repetitions.
- One saved-donor follow-up failed its binding check.
- Two shared-partner intersections failed despite usable parent evidence.

CFTR enrichment membership is evaluated using the requested enrichment relation's
endpoints. Extra independent detection evidence is retained but is not falsely
treated as part of that requested population. All 24 baseline streamed answers
reconstructed exactly from their deltas.

The final broad offline suite has **2,766 passing tests, 231 passing subtests,
5 skipped tests and 19 substantive pre-existing baseline failures**;
their assertions have not been weakened. Five additional failures in the archived
baseline require Git history, which `git archive` intentionally omits. See the
saved failure list and final offline summary for exact results.

Live read-only tool checks also passed: ABCC7 and CFTR resolve to the same
verified gene; PLN resolves to the reviewed tissue node; sample-code lookup
returns PLN, PLN_H, PLN_T and PLN_B, separately from that tissue display name.
These checks used no model calls and do not substitute for final-answer replays.

## Outstanding before acceptance or deployment

The previous automatic approval review blocked live model replays pending
explicit data-use approval. That command did not run. On 2026-09-24 the user
confirmed that PanKgraph content is public and explicitly authorized Claude
validation and dev deployment. This approval blocker is resolved; the existing
ledger and correctness/performance gates still apply.

Release checks:

1. Stage the final reviewed candidate snapshot in private validation storage.
2. Run all correctness cases and all 24 controls with the existing ledger.
3. Inspect every frontend answer against its retrieved evidence; compare full
   donor/sample memberships, tissue relationships and linked coloc signals.
4. Exercise revision narrowing/widening/replacement, independent branch failure,
   saved-run refresh, citations and streamed reconstruction.
5. Compare matched successful controls with `compare_performance.py`: median
   latency allowance max(20%, two seconds); mean settled model cost allowance
   20%. Report newly successful cases separately. One bounded matched rerun may
   investigate a transient variance; unexplained regressions still block release.
6. Only after every gate passes, deploy the dev backend/schema bundle via the
   ownership-aware manager with an immutable rollback release. Preserve production,
   sibling services, sessions and the existing cumulative budget.

Missing approvals, unfinished tests, exhausted budget, or an unresolved core
question are not acceptance. No correctness or performance improvement is claimed
for the candidate until these live gates are completed.

Private audit root (server only):
`/db/pankagent-vnext-private/operations/schema-consolidation-20260924/`.
Full baseline runs, model traces, sessions and reference memberships stay there
with service-user ownership and restricted permissions.
