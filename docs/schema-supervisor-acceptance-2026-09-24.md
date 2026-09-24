# PanKgraph schema supervisor — acceptance, 2026-09-24

Implementation: `6d7340d5840c97fee805d5e82a6b5fe9a89e6676`, following extraction commit `eec97eb` on `codex/composable-planner`. Baseline: `e062fff`. The release is PanKgraph-first; this is not a separate GKB/GLKB deployment.

## Frozen inputs and scope

- General `KG_agent/kg-standard` v0.3.0: `414c2732b5c8a50ba3a4d18561cfe12785a8b535`. No shared-standard changes.
- Frontend `xuteng/react` rechecked at `38c8f454eaecf79cacd7ef42d6b109595798791a`.
- [Landing examples](https://github.com/wangyiqunumich/pank_frontend/blob/38c8f454eaecf79cacd7ef42d6b109595798791a/src/schema/landing_sample_questions.json): SHA256 `66aacb28b6915fa11d92c3b097c290a30468681b1e09b298e61a58d56a0c99de`; 16 displayed entries, expanded to 14 distinct concrete questions. Guided forms use configured choices, never literal placeholders.
- Pack `pankgraph` version `1.0.0`, release `PanKgraph_08_04`, digest `b1edebc5541fc9e8a390961e58101179e6a4615f6bcfa3d21817206117c13632`.
- Six JSON modules and contracts live under `pankagent_vnext/agent_schemas/`. No frontend schema implementation changes. PostgreSQL OCR mapping remains explicitly disabled/unverified.

## What changed

Python grounding and Claude resolver calls share signed, release/pack-bound identity evidence. Case variants, recorded synonyms, repeated aliases for the same verified ID, and cell-name inflections reuse the evidence. D01 is advisory. Preparation is a supervisor tool with task-specific diagnostics; executable schema, scope and binding checks still run before retrieval.

The supervisor has two lookup batches, three proposals, two execution repairs, and seven planning/execution Claude calls maximum. Repair claims persist in the run audit. Independent usable tasks survive failures; failed descendants cannot become unrestricted queries. Backend typed IDs and paths drive chains, joins and counts before any formatter compaction.

A schema-declared session population reference binds “these donors” to complete backend records from the same session, release and schema snapshot. The server verifies the saved result fingerprint and remaps an explicitly named prior task. Formatter samples never supply these IDs.

Clinical fixes distinguish ND from not-T1D, stage from diagnosis, exact assay labels from explicitly requested RNA-capable multiome inclusion, source ownership on donors, and unspecified tissue from a required filter. Comparison grammar no longer interprets “and” as a tissue.

Run materialization is bounded at 12,000 nodes, 20,000 edges and 12 MB. Independent tasks receive reservations; completed parents transfer only measured unused reservations, divided across children. This prevents small cohort-parent queries from withholding capacity needed by their sample children. Exhaustiveness still requires cursor completion.

Large-population answer streaming now avoids repeatedly decoding the saved graph for status checks and delta persistence. Event redaction uses a cached identity-only context invalidated when evidence/plan changes. Aggregate projection removes redundant copying and quadratic sample-edge scans. Output generation, citation filtering, rendering and answer-writing methods remain unchanged; input/public projection and persistence were optimized.

## Live acceptance

All 14 concrete landing cases executed real grounding, planning, Neo4j retrieval and answer synthesis. Answers were inspected, not accepted merely by their length. They cover ADCY3/PLEKHM1/CFTR colocalization, effector/marker status, expression/enrichment, pathway/interaction evidence, comparative interpretation and guided QTL questions. Verified empty marker evidence is reported as a release-scoped absence. Broad pathway-overview cases disclose bounded annotation coverage. “Lead QTL” answers distinguish recorded membership from missing explicit lead designation.

Independent Neo4j references compared full distinct ID membership, not just equal totals:

| Request | Verified count | ID membership |
|---|---:|---|
| HPAP ND donors | 96 | Exact |
| HPAP ND samples | 5,480 | Exact |
| HPAP ND exact scRNA-seq samples | 47 | Exact |
| HPAP T1D donors | 44 | Exact |
| HPAP T1D samples | 3,064 | Exact |
| HPAP T1D exact scRNA-seq samples | 12 | Exact |
| HPAP stage 1 donors | 3 | Exact |
| HPAP stage 2 donors | 1 | Exact |
| HPAP stage 3 donors | 44 | Exact |
| HPAP ND spleen RNA-capable samples, explicit multiome inclusion | 10 | Exact |

Additional actual API checks:

- Stage-1 donor answer → “these donors” → exact scRNA-seq samples: the same 3 parent donors and 1 sample match independent references.
- ND/T1D comparison: both donor sets and both sample sets match separately. ND and T1D are not treated as complementary.
- Revision API: all HPAP donors 192 → narrow to ND 96 → widen to all 192 → replace with T1D 44. Every membership matched; widening retrieved 96 IDs absent from the narrowed set. Questions remained standalone.
- Four runtime cases reconstructed saved answers exactly from streamed deltas; refreshed answers were identical. Runtime harness literature was deliberately unavailable, so their terminal status was `partial` despite complete graph checks and no graph diagnostics.

Private artifacts, full populations, model traces, reference queries and the enforced ledger remain under service-owned protected storage. Maintained replay entry points: `scripts/acceptance/`; frozen source manifests: `tests_vnext/fixtures/acceptance/`.

## Offline acceptance and known baseline failures

Final full run: **2,748 passed, 19 failed, 5 skipped; 231 subtests passed**. All 19 failures also occur on baseline `e062fff`; no new failure remains. Baseline had 20 failures and 2,722 passes. The test suite is therefore not globally green, and these failures are not hidden or relabeled as passes.

Known failures: audit repair (1), unsupported gene exclusion (3), cold-grounding fake adapter (1), results lease fixture (1), historical whole-plan preview rejection expectations (7), and historical whole-plan readiness rejection expectations (6). The latter expectations predate the existing independent partial-answer policy. The old formatter-source comparison baseline was corrected to the actual task baseline, while output methods remain AST-checked unchanged.

New tests cover signed identity reuse/tampering, duplicate aliases, clinical/assay semantics, typed session references and ownership, dependency closure, persistent repair limits, per-query context coexistence, full-ID operations, streaming persistence/redaction, and schema validation. Existing chain/mixed/revision tests remain exercised. One small renamed-schema/type/property fixture checks the generic path/lookup interfaces; no additional Neo4j deployment was built.

## Diagram and deployment

Tab 01 of `pankagent-current-io-payload.drawio` shows the schema pack, advisory D01, Claude supervisor, preparation tools, task-local repair and the parallel/chain/mixed paths. Standalone M05 is removed. Yellow Schema 1–6 notes and red error notes are inline; tooltips name the files. Retained component geometry is identical; only the necessary repair connector was rerouted. Tabs 02–04 remain byte-for-byte identical to the manual-edit snapshot, and tab 01 was rendered and inspected.

Dev release target: `20260924-schema-supervisor-6d7340d/backend` on owned port 8794. Immutable rollback: `20260923-error-diagnostics-e062fff/backend`. Deployment uses `deploy_reliability.manage`; results 8795 and production are not restarted. Session and budget databases receive consistent private backups before switching. See the private `deployment.json` for the verified ownership transition.

Paid validation ledger: **$5.4021105 settled + $1.2368275 conservatively reserved = $6.638938 accounted**, below the separate $10 ceiling. Six interrupted/timeout reservations remain reserved because their final provider charge is uncertain; they were not reset or assumed free. The ledger includes the earlier planning validation balance. Deployment itself makes no paid model calls.

## Remaining limits

Some complex clinical parsing and signal matching remain tested Python operators during incremental extraction; the JSON pack is not a claim of arbitrary-KG compatibility. PostgreSQL release/coordinate mapping is a separate optional acceptance task. Broad annotation overview answers remain bounded rather than exhaustive. Existing baseline test debt is recorded above. No formatter output behavior was changed to conceal retrieval or semantic limitations.
