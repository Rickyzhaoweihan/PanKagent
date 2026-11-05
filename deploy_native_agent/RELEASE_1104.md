# Dev release migration to PanKgraph_1104

2026-11-04: user authorized all new dev KG queries to use 1104, retain the
GPT-6.1-sol formatter and historical results, and refresh caches.

The native entrypoint on loopback 18864 opts in when its private
`1104-config.json` exists. The integration reuses the verified 1104 query
core/transport from independent commit 8ccd28cbc52519fbcfa010c48ef96ee676aabe92,
with the existing website Workflow, GPT formatter and presentation modules.
The independent query-dev service on 18865 is retained, not used as an
unformatted replacement for the website workflow.

New state is in `workflow-state-1104`; old `workflow-state` is retained.
The dispatcher reads old saved runs and request IDs from their original store.
Old pending plans cannot confirm or revise against the retired database: they
return 409 asking for a fresh question. New answers retain exact selected-object
SQL evidence, release identity, backend-issued refs and full native history.
The formatter receives original selected evidence, not the query agent's prose.

The results gateway on 8795 uses its release-pinned HTTP query adapter and
viewer bridge at server-local 8826. It does not construct the legacy Neo4j
query driver when enabled. Conventional queries are schema-checked by the
paired reader; obsolete template fields fail explicitly rather than falling
back to an older graph. Health checks report the current native agent and
paired graph identity. Authentication and the existing browser origin policy
remain in place. Neither query nor inspection depends on a Mac tunnel.

Cache renewal uses fresh native state, new `results-1104-release-cache-1`
result identities, fresh process caches, frontend 1104 intent/projection cache
keys and no-store API fetches. Historical stored results and downloads are
retained, not relabeled or deleted as cache. SQL inspection rejects any other
release and cannot infer object ownership from matching IDs.

Private rollback records:
- `/db/pankagent-vnext-private/backups/dev-1104-20261104T235400Z`
- `/db/pankagent-vnext-private/backups/gateway-1104-20261104T235532Z`
- deployment manifests under `operations/lighthouse-direct/state/dev-1104-*.json`.

Before rollback, verify idle queues and current listening PID/UID/cwd. Restore
only recorded changed code/config, restart only native 18864/results 8795, and
retain both state directories. Never restore stale databases to roll back code.
Do not restart 8794, production, model forwarding or the independent 18865.
A stale results PID record was replaced using the verified listening process
and normal process-start metadata; future restarts must recheck ownership.

Validation so far: real 1104 INS prepare/confirm/enrich plus GPT formatter;
ND/T1D/T2D evidence table; post-layout release/ref preservation; live release
and scalar/graph query reads; dispatch regressions; site UI verification is
recorded separately and must not be inferred from a health response.

Live browser acceptance completed on dev.pankgraph.org: run
`simple-qwen-bbc949bd829f4b80b32947625162c677` completed prepare, confirm,
enrichment and GPT-6.1-sol formatting. Expanded Expression evidence summary
loaded General expression (3 rows), RUV (2 rows), and DEG (3 rows).
Historical result GET remained 200; old pending confirmation returned 409.
The existing deployed Basic Auth flags are disabled and were not changed;
wrong-release inspection returned 409 and disallowed browser origin returned 403.
Frontend xuteng/react commit 0d4d32ab10c7f43604a676938a057b3153c6aaa3
was deployed by Amplify job 893 and the Jieliu3 automatic frontend deployer.
