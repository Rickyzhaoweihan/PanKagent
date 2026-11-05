# Release-scoped information-panel gateway

Status: activated and browser-verified on dev on 2026-11-04; see RELEASE_1104.md. The captured source manifest
remains the historical 2026-11-04 baseline, not a claim that these additions are live.

The frontend follows `wangyiqunumich/pank_frontend` `xuteng/react`. The results
service's existing authentication configuration and origin policy apply to three routes:

- `GET /api/viewer/v1/schema?release_id=...`
- `POST /api/viewer/v1/inspect` with `release_id` and exact `ref`
- `POST /api/viewer/v1/table` with the same owner and registered table selectors

Configuration defaults to disabled:

| Setting | Meaning |
| --- | --- |
| `PANK_RESULTS_VIEWER_URL` | Server-local loopback origin of the paired graph/evidence API |
| `PANK_RESULTS_VIEWER_RELEASE` | Exact graph release accepted by this bridge |
| `PANK_RESULTS_VIEWER_TOKEN_FILE` | Private server-side token file; never send its contents to a browser |

For the independent 1104 runtime, the intended upstream is jieliu3 loopback
8826 and the release is `PanKgraph_1104`. This does not authorize a database
switch. The website's historical 08_04 result cannot borrow 1104 SQL evidence.
The existing formatter/history/authentication chain must survive any separately
approved query-engine migration.

The frontend requires the result's `release_id` (or `kg_release`) plus the
backend-issued `ref: {kind,type_id,id}` on each graph node/edge. Display aliases
are never lookup keys. Preserve these refs through projection and persistence;
do not reconstruct them from node labels, an SNP ID, or neighboring endpoints.
Old saved graphs without refs keep their recorded information panels.

The bridge permits only inspection and registered-table reads. It rejects raw
queries, arbitrary upstreams, mismatched versions and mismatched inspection
objects. Browser cookies and authorization are not propagated to the reader.
Limits are 64 KiB request and 16 MiB response; no response caching. A failed read
preserves recorded graph evidence, without declaring absent evidence.

## Activation and rollback checklist

1. Verify the actual owned results service on 8795 and its source root. Do not
   restart agent8794, model forwarding, or production as part of this change.
2. Back up only changed code/config under the private `/db` backup tree with
   owner-only permissions. Preserve all runtime state and historical manifests.
3. Verify the selected reader's release and schema using its existing server
   token; keep secrets out of logs. Check idle state before service restart.
4. Set the three variables in the existing service configuration. Deploy the
   matched frontend commit from xuteng/react. Never route through a Mac tunnel.
5. Verify site authentication, exact release/owner rejection, real inspect/table,
   saved-result compatibility, tabs, pagination/download, and full graph controls.
6. If verification fails, restore the prior code/config and restart only the
   owned results service. If rolling back a frontend selector, coordinate its
   existing autodeploy timer so it does not overwrite the rollback.

Validation: `tests_results/test_native_viewer_proxy.py` exercises the actual
captured gateway module and authentication middleware. Local real-reader checks
covered 12 inspections and 16 table first pages; these are not deployment
acceptance or scientific accuracy claims.
