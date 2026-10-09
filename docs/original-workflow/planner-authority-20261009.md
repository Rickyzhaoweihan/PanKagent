# Planner-owned scope — 2026-10-09

The live spleen request failed because fixed phrase recognition removed the planner's normalized assay predicate. New server-recorded planner decisions now take a separate preparation path: the planner owns interpretation and predicates; Python resolves identities, verifies selected categorical values against runtime inventory, binds parameters and validates execution. It does not reparse the raw request to infer or reject donor, tissue, source, stage, diagnosis or assay filters. Legacy unselected drafts retain their existing helper behavior. Model ambiguity should be handled by the existing planning/clarification loop.

New field-to-inventory mappings, cohort/assay ownership and generator guidance are in validation.json, within the four-schema pack. The shared implementation contains no PanKgraph owner/field constants. The planner-owned path also avoids metadata word-based sex/gender rejection; actual unsupported field/operation checks remain. Canonical candidate phrase roles no longer require fixed example keywords; explicit planner example roles still compile away their corresponding filters. Broader introduction functionality remains inactive on dev.

Validation:
- 154 targeted tests passed on canonical code; 50 passed on the exact deployed-base implementation. Coverage includes typo/paraphrase authority, source ownership, operators/exclusions, different KG field names, missing recorded values, preserved filters, tampered parameters, disconnected joins, preview, confirmation and formatting.
- Full candidate run: 3,159 passed, 35 failed, 5 skipped, 231 subtests passed. 34 failures match baseline. The additional failure was an order-dependent negative-test string replacement; it was corrected and passes in the 154-test targeted rerun. The baseline archive additionally has four Git-history-dependent test failures because it has no .git directory. Tests that previously required lexical vetoes were updated to the user-requested planner-owned policy. No claim that the historical suite is wholly green.
- Real graph replay with mocked planner-selected canonical bindings: original typo and canonical spleen questions both return the identical 10-sample membership. Expanded PLN request returns 13. All four selected filters remain present. Repeated against the activated release.
- No fresh paid model or browser run; API spending for this change is $0. This verifies the preparation/execution boundary, not model interpretation quality on every future query.

Only the eight-file patch was deployed on the existing backend base. Verified online backup, idle agent queue, owned 8794 restart and readiness check completed. Frontend and results service preserved; model remains claude-sonnet-5-5. Agent PID 4027815. Schema is 2.2.3 on dev and 2.3.3 on the broader inactive canonical candidate.

Implementation commit: `a750b3844480a111b22a5f2889db09057e44ffc6`. Release: `/var/local/serviceuser/projects/pankgraph-demo/releases/20261009-planner-authority-a750b38/backend`.

Application SHA256: `0944b3e105774c656444af735c7edf687d8d048a7ef333f1160bbcff091e8642`. Schema SHA256: `e05ac3195f2654156e2fca277917fd89c974f34a83140d973bf039e1bfbc59f9`.

Private evidence: `/db/pankagent-vnext-private/operations/planner-authority-20261009/` (activation.json, graph-replay.json, deployed-replay.json). Previous release remains available for rollback.
