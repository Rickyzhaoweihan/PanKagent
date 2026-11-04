# Schema-bound Qwen query dev

An independent, authenticated loopback service for a frozen graph release and
its explicitly owned PostgreSQL evidence. It preserves the saved simple agent's
prepare → confirm → enrichment protocol. It does not change the existing GPT
formatter, resource pipeline, website routing, or production deployment.

## Data and architecture boundary

| Layer | Responsibility when changing a dataset |
|---|---|
| Schema pack | Actual stored labels, typed properties, directed relations, source definitions, versions, declared categories and SQL table contracts |
| Runtime configuration | Pinned release/schema identity, graph-query URL, model URL/name, credential-file paths and operational limits |
| Query backend | Read-only execution, stable typed object refs, registered object/snapshot/table/record ownership, pagination and authorization |
| Agent core | Generic pattern compilation, tool feedback, model decisions, selection and two-phase workflow; no gene/disease/modality routing branches |

Changing schema alone cannot change a connection or authorize SQL joins. A new
release on the same interfaces needs a rebuilt pack and an explicit runtime
binding. New generic interface capabilities can require shared code changes:
this release adds typed array membership (`has`) and exact-object SQL inspection.
Scientific rules and dataset facts belong in the pack, not a prompt branch.

The pack is compiled from frozen `inputs.json`, `data_schema.json`, and the
matching published reader schema. Required labels are labels on the same node,
not an inheritance hierarchy. Shared-label property types may be a union;
`node_variants` preserves precise types. Schema/table definitions do not grant
record access. Source versions are not replaced by the graph release name.

## Tool contract

Seven tools: `schema`, `resolve_entity`, `property_values`, `query`,
`inspect_result`, `clarify`, `finish`.

- `query(detail=none|summary|records)` uses `/api/kg/v1/cypher` and retains
  only selected delivered graph objects and their owned attachments.
- `inspect_result(kind=evidence, query_id, ref, table_id, fields, filters,
  cursor)` requires a server-issued typed ref in that query's graph. The backend
  independently checks the table's exact owner and snapshot.
- SQL pages are bounded, default 20 and maximum 100. A cap is disclosed as
  partial. NULL, false, zero, unknown license and service failure stay distinct.
- No endpoint or same-ID fallback grants evidence ownership. A variant cannot
  read an association's credible set; a Gene/Cell cannot borrow expression from
  neighboring relationships.
- Read table definitions with `schema(tables=[...])`. Discover exact accessible
  tables by inspecting a retained object without `table_id`.

## Runtime

Python 3.11+ and the local dependencies in `requirements.txt`. The service uses
an existing self-hosted OpenAI-compatible vLLM endpoint. Its API token is a
service access token; there is no commercial model fallback.

Private JSON configuration must contain these fields (secret values are never
configuration literals):

```json
{
  "release_id": "PINNED_RELEASE",
  "schema_path": "/private/schema_packs/SHA256.json",
  "state_root": "/private/state",
  "api_token_file": "/private/api-token",
  "query_url": "http://127.0.0.1:QUERY_PORT",
  "query_token_file": "/private/query-token",
  "model_url": "http://127.0.0.1:MODEL_PORT/v1",
  "model": "SERVED_MODEL_NAME",
  "model_token_file": "/private/model-token",
  "query_limits": {"nodes": 100, "edges": 400},
  "port": 18865
}
```

Use a private runtime directory owned by the service user, directories 0700,
files 0600, `umask 077`. Keep all state and model traces outside Git. The
supervisor expects its importable code directory at `<config-parent>/code`:

```bash
python -m pankagent_query.runtime --config /private/config.json start
python -m pankagent_query.runtime --config /private/config.json status
python -m pankagent_query.runtime --config /private/config.json stop
```

Only matching UID/cwd/module PIDs are controlled. `/health/ready` validates the
paired release digest and authenticated model registry without inference.
All non-health routes require the private API bearer token. The schema route
is `/agent/v2/schema`; retained selected evidence is available through
`/agent/v2/runs/{run_id}/evidence`. Client SQL/Cypher is not accepted by this
agent; patterns compile to parameterized read-only requests.

## Acceptance: 2026-11-04

The isolated jieliu3 service uses release `PanKgraph_1104`, port 18865,
query API 8826, and the existing H100-served `pank-candidate` model. The original
18864 service and its GPT formatter remain unchanged. No UI or production
route was switched.

- Unit checks cover arrays, typed schema, exact ownership, selection,
  NULL/zero/source-version retention, and unauthorized refs before network I/O.
- Real Qwen INS detection read the three owned ND/T1D/T2D absolute summaries.
- Real Qwen RUV read condition-level summaries, preserved `2025-05-22` and
  `RUVSeq_native_export`, and reported absent T2D RUV without treating it as zero.
- Real Qwen HS3ST1 read association-owned credible sets with paginated members.
  It also retrieved one exonQTL alongside the requested eQTL and explicitly
  distinguished it. This is a query-scope limitation, not strict biological
  filtering acceptance. No dataset-specific rule was added to hide it.
- Eight real API ownership checks: SNP→credible and Gene/Cell→expression
  rejected; QTL→credible, CCC→contexts, donor→metadata and sample→two metadata
  tables succeeded. These checks used zero model calls.

These are bounded integration examples, not an accuracy benchmark or a new
scientific reanalysis. Full old formatter/literature/download features are not
claimed for the isolated query-dev service. Saved baseline provenance is in
`SOURCE.json`; its old schema identity matches the observed 18864 service,
but that does not assert byte identity with inaccessible private runtime code.
