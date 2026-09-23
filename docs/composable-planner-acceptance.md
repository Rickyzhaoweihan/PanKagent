# Composable planner acceptance — 2026-09-23

Implementation branch: `codex/composable-planner`; baseline `4f2d0d3`.
Changes affect planning, grounding, query dispatch, typed backend result operations,
and formatter input preparation. Formatter output generation and writing contracts
are unchanged. An incidental aggregate metadata serialization fix prevents string
schema entries from being treated as relationship records.

## Verified

- Agent regression suite: 2,683 passed, 5 skipped, 231 subtests; one known baseline
  results-service lease fixture was deselected after reproduction on untouched baseline.
- Stage 3 HPAP donor query: 44 IDs, exact equality with independent reference query.
- Stage 3 plus positive clinical type 1 diabetes: 44 IDs, exact reference equality.
- Stage 3 plus negative clinical type 1 diabetes: zero IDs, exact reference equality.
  Source, recorded stage and clinical polarity remain separate predicates.
- Parallel INS detection/enrichment plus marker revision: completed preview and answer.
- Removing Stage 3 restriction: 192 HPAP donors, including 148 absent from the old
  result; exact equality with the unrestricted source reference query.
- Live five-node chain with repeated Gene roles and shared Reactome intermediate:
  two complete query fragments and one join; exact ordered-ID tuple equality with
  a direct reference query. GPU generation invoked; validated templates executed.
- Stage 3 and chain streamed deltas exactly reconstruct their saved final answers.
- Synthetic six-node joins and eight-node draft compilation preserve connectivity;
  joins cannot silently omit ancestor fragments or accept missing path witnesses.
- Typed intersection/union/difference/filter, final projection, incomplete exclusion,
  release mismatch, cross-type ID ambiguity, per-query identity-only input and short
  full-evidence siblings have regression coverage. Formatter samples cannot drive
  backend computation. The synthesis method is compared byte-for-byte with baseline.

The unrestricted five-node investigation exceeds existing retrieval limits and is
explicitly blocked; no incomplete population is reported as complete. Those limits
remain in force. Browser rendering was not revalidated; frontend source is unchanged.

## Validation budget and private evidence

Paid validation used its own owner-only state and persistent $20 ledger on isolated
port 8894. Total settled spend: **$0.748804**, 44 paid calls, zero outstanding
reservations at completion. GPU generation is outside this paid-model ledger.
No validation calls changed the existing shared budget ceiling.

Private run records, exact query outputs and reference comparisons remain on the
service host under `/db/pankagent-vnext-private/operations/composable-planner/`.
Donor-level records and credentials are not included in Git. A five-database online
backup was created with existing deployment tooling before the dev replacement.

Deployment uses the ownership-aware manager for agent 8794 only. Existing SQLite
state, original frontend, results 8795, health 8796 and production are retained.
Rollback uses the previous release's ownership manager; no database rollback is
required, and no direct process-ID signaling is used for live services.
