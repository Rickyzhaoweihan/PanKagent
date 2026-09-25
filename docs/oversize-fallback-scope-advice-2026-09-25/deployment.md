# Dev deployment — 2026-09-25

Implementation commit: `947777ccc9269589a1ec13d283857ebd74d2907d` on existing `Ringo`.
Public entry: https://dev.pankgraph.org/.

The user explicitly accepted related GWAS exploration and authorized deployment.
The planner keeps direct answer evidence primary, permits useful optional context,
and discourages a large donor-only investigation for a simple disease definition.
This is guidance to Claude, not another Python rejection gate.

## Deployment

- New immutable release: `/var/local/serviceuser/projects/pankgraph-demo/releases/20260925-exploration-947777c/backend`.
- Replaced the single owned dev agent on loopback 8794 through `deploy_reliability.manage`.
- Exactly one active `pankagent_vnext.app:create_app` process verified, PID 404204.
  Previous PID 3353932 exited. No second dev agent was started on another port.
- The release-local virtual environment uses the existing fixed base runtime plus
  the schema-validation packages already used in candidate validation. No shared
  runtime was changed. Schema imports and pack validation passed before cutover.
- New application SHA-256: `5ab0bc7a8599c37c6ebe76bce4d7b244788bcef33f17712a7c8edc417869db85`.
- Schema pack SHA-256: `262e3c8f4c301b467aaf266e9ddedeec89f6be3f49396a5799aecabeca6cdfd4`.
- Agent and results readiness passed. Results PID 29663 and health PID 4188181
  stayed unchanged. No production service was restarted.
- Existing session/event/audit and budget table hashes stayed identical across
  cutover, excluding the expected ownership-lease transition. No budget reset.
- Log remains in `/db/pankagent-vnext-private/logs/pankagent-vnext/release-agent.log`,
  mode 0600. Existing state directories and compatibility links were preserved.

## Public frontend verification

Submitted “What is T1D?” in the real public frontend, confirmed its displayed plan,
and waited for the final answer and literature. Run:
`960e9a78-7bf9-4248-aced-a3b26994620d`.

- Status completed; graph and literature both completed.
- One disease node, zero relationships, zero donor nodes.
- Definition, identifier, synonym and provenance were rendered.
- Graph answer: 1,107 characters; durable SSE deltas reconstruct it exactly.
- The run's application fingerprint matches the deployed release.
- Model charges observed since submission totaled $0.0803929; the live dev ledger
  retained its earlier spend and reservations. This is additional to the isolated
  validation ledger's $7.105156 settled and $0.213457 reserved, still within the
  authorized remaining validation amount. No ledger allowance was reset.

Before release: 26 focused tests passed for the guidance change and existing
scope/overflow behavior. Earlier input-policy validation had 89 focused passes;
the full offline suite retained 19 known baseline failures. This deployment smoke
check does not claim a fresh complete correctness/performance matrix.

## Rollback and single-instance policy

Only one dev agent is active. Previous releases are inactive rollback artifacts,
not concurrently serving versions. Do not delete historical backups or unrelated
services as part of this cutover.

A verified five-database online snapshot is at
`/db/pankagent-vnext-private/backups/pankgraph-demo/pre-exploration-947777c`.
Rollback code release:
`/var/local/serviceuser/projects/pankgraph-demo/releases/20260924-schema-supervisor-6d7340d/backend`.
Use each release's ownership-aware manager: stop only the new `agent`, then start
only the previous `agent`; preserve current sessions and budget, and do not restore
old database snapshots merely to roll back code.

Detailed continuity and browser-verification records are under
`/db/pankagent-vnext-private/operations/deployment-exploration-947777c/`.
