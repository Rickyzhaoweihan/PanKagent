# PanKagent backend repair — approved implementation contract

Approved September 21, 2026. This is the implementation contract, not a claim
that the repairs or deployment have passed acceptance.

## Baseline and ownership

Start from tested agent/results commit `4ddc90dca43a69c148231a19808547323ac6d679`.
The isolated branch is `codex/backend-audit-repair-20260921`; its PR targets
`codex/dev-functional-axis`. Promotion to `Ringo` is separate. Preserve the
existing dirty checkout, production, shared graph services and historical state.

PanKagent owns planning, evidence projection, answer synthesis, its HIRN client,
Functional Data interpretation and dev access integration. Upstream HIRN ranking,
retrieval, refinement and synthesis belong to the separate
[HIRN handoff](hirn-api-handoff-2026-09-20.md). Do not implement ssGSEA, named-gene
exclusions, new datasets, or the future health domain in this repair.

## Required behavior

- Preserve canonical entities, source, tissue, cohort, stage, diagnosis, assay
  and measurement constraints through execution. Independent GWAS/QTL/coloc
  checks must not depend on an unrelated empty branch. Never broaden scope to
  recover from failure. Preserve follow-up identities and summarize existing
  answers without inventing graph work. Allow one bounded malformed-plan repair.
- Assign stable evidence IDs and distinguish executed-empty, dependency-skipped,
  failed, truncated and context-omitted states. Preserve complete-record facts,
  relation examples and provenance when compacting evidence.
- Render schema-constrained factual blocks from canonical facts. Validate counts,
  comparisons, entity roles, assay/cohort context and evidence references before
  displaying answer text; progress events remain live. Invalid synthesis gets a
  deterministic evidence summary with an explicit limitation, not another judge
  call. Version the interpretation bundle and dependent caches.
- Represent aggregate-only intent explicitly and remove individual identifiers
  from synthesis and user-visible evidence/results/downloads for that request.
- Keep selected-donor inventory distinct from finite contributors, including
  counts per timepoint. Preserve existing donor-count meaning, trace values,
  units, filters and stimulus intervals. The audited case must retain all 50
  means while distinguishing five selected from three contributing donors.
- Support confirmable literature-only plans; requested literature proceeds
  independently of partial graph work. Preserve exact question/history and
  bounded correlation hashes. Allowlist upstream error categories without
  exposing raw traces or changing the current HIRN request body contract.
- Add no GWAS resource alias without verifying its exact object/source mapping.
  Preserve explicit unavailable resources when no mapping can be established.
- Verify the existing dev gate covers agent, tools and health; remove redundant
  app password challenges only in that gated dev configuration. Preserve direct
  backend and operator protections and production authentication.

## Acceptance and compatibility

Keep endpoints, historical results, sessions, confirmation/cancellation and
idempotency. New metadata is additive/versioned; absent historical metadata means
unknown, not a new verification. Saved reads must not trigger inference.

Start with sanitized offline regression fixtures from the September 20 audit:
cohort scope/privacy, contributor counts/missingness, skipped GWAS, stable IDs,
interaction evidence omission, numerical direction, source classifications,
PLEKHM1 entity roles, assay distinctions, malformed plans, summaries/revisions,
GO follow-ups, HIRN errors/modes, resource/filter behavior and lifecycle invariants.

Paid tests cover all 16 authored questions and triage-cited historical cases with
their original wording and necessary predecessors; repeat PLEKHM1 three times.
Do not replay the entire 2,416-occurrence corpus. No reproduced P1 scientific or
privacy defect is acceptable. Separate upstream HIRN limitations from local
regressions and require actual browser acceptance before claiming it.

## Budget and rollout

New combined Claude/OpenAI ceiling: **$20**, independent of the previous closed
campaign and its retained unknown-charge reservations. Allocate $2 baseline,
$10 candidate tests, $4 dev acceptance and $4 contingency. Reserve for every
physical provider request/retry; retain unknown charges and stop admissions when
the next reservation cannot fit. Offline tests incur no paid calls.

After gates pass, open the PR and validate controlled dev using independently
owned service managers. Preserve private `/db` logs/backups and the dashboard
rotation fix. Record rollback code/configuration without restoring old budget
ledgers. Do not bypass the earlier blocked external IGV script exception. Finish
with exact commits, before/after outcomes, spend, remaining owners and rollback
evidence. No document is sent to another team automatically.
