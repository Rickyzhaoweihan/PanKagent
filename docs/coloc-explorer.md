# Recorded colocalization explorer

The results service exposes recorded T1D colocalization and the corresponding
credible-set members. It does not rerun coloc or fine-mapping, infer effect
direction, manufacture LD values, or modify the graph viewer/agent services.

## HTTP contract

The existing results authentication and public prefix apply to these GETs:

- `/api/coloc/records` returns `version: 1`, `graph_version`, `checked_at`,
  `records`, and `coverage`. Each record has a stable SHA-256 `id`, gene/disease
  IDs/names, QTL/GWAS signal IDs, the exact `gwas_credible_set_id`, `dataset`,
  `tissue`, `qtl_type`, source/version, `nsnp`, `posteriors: {h0,h1,h2,h3,h4}`,
  distinct `gwas_leads`/`qtl_leads`, and `notices`.
- `/api/coloc/records/{id}` returns that `record`, `variants`, `coverage`,
  `sources`, `graph`, `coordinate_build`, `status: ready|partial`, and notices.
  Each variant has `id`, nullable `chromosome`/`position`, `coordinate_source`,
  and independent nullable `gwas`/`qtl` objects with `member`, `pip`,
  `nominal_p`, `effect_allele`, `other_allele`, and `slope`.
- `/api/coloc/records/{id}/download/{gwas|qtl}` downloads an optional validated
  operator-registered extract. Public QTL downloads otherwise reuse the existing
  exact-key `/api/resources/download` endpoint.

All optional numeric fields are finite or null. Missing statistics are not zero.
Recorded posterior vectors are never renormalized; missing/invalid values or a
sum inconsistent with one produce an explicit notice. `nsnp` is the original
coloc analysis denominator, separate from each credible-set/member count.

`coverage` includes `gwas_count`, `qtl_count`, `shared_count`, `variant_count`,
`coordinate_count`, `coordinate_omitted`, `gwas_expected`, `qtl_expected`,
`gwas_omitted`, `qtl_omitted`, `omitted_count`, `gwas_complete`, `qtl_complete`,
`complete`, `scope: credible_set`, and notes. Missing expected counts stay null.
Each source supplies its label, status, original URL when known, SHA-256,
coverage, optional download URL, and original source versions when available.
The source JSON hash identifies retrieved records, not a remote file checksum.

## Retrieval and scientific boundaries

`ColocExplorer(query, resources, coordinates, graph_version, settings)` can be
mounted with `coloc_router(explorer)` in a separately authenticated preview.
The production mount is three additions to the results application; no graph
viewer or agent implementation is changed.

The catalog retains all recorded `Gene → disease` `SIGNAL_COLOC_WITH` rows for
`MONDO_0005147`, regardless of H4. Identity includes graph release, endpoints,
exact signal pair, dataset, source, and source version. Conflicting records with
the same identity are excluded explicitly rather than silently selected.

Every graph read uses `QueryService.execute_query`: configured release check,
read-only validation, EXPLAIN, and materialization limits still apply. Scalar
pages of at most 200 rows avoid applying presentation node/edge limits to
membership. Catalog responses retain at most 1,000 source rows; each membership
branch and the final variant union retain at most 5,000 rows with omitted counts.
A catalog count plus pages are not a transaction-wide snapshot; early/missing
pages produce incomplete coverage. The configured graph release should be
immutable. Catalog cache TTL is 30 seconds; health polling does not call it.
Detail work uses two slots, at most ten admitted requests, a 35-second work
deadline, and a 40-second deadline including waiting for a slot.

GWAS source mapping is explicit for `HIRN_T1D_QTL_GWAS/v1.0` to
`GWAS_finemapping_V1`. Only the verified `__selected` suffix is normalized.
QTL joins use exact gene, credible set, source, and tissue. The two membership
branches are independent: a missing annotation cannot erase primary coloc.
Unregistered source contexts remain inspectable but cannot acquire guessed
signal membership. Returned source versions are exposed; mixed versions mark
coverage ambiguous.

Full QTL members come from the exact registered S3 credible-set object, not the
single QTL lead typically stored in Neo4j. The existing ResourceManager validates
the seven-column TSV schema and caches/downloads it. This adapter checks row
counts against recorded `n_snp`/`credible_set_size`, verifies recorded leads,
and compares mutually available statistics with serialization tolerance. True
contradictions preserve original graph/source evidence but withhold the combined
variant statistics. Missing graph annotations are not contradictions. S3 denial
retains graph evidence and marks incomplete coverage; it is never an empty-set
result. The unverified candidate GWAS S3 prefix is not guessed.

GRCh38 coordinates come from the existing verified CoordinateLookup. A bounded
live comparison found that legacy `PanKgraph_08_04` variant starts equal dbSNP
one-based positions despite the generic graph BED contract. These graph values
therefore **cannot be converted using that generic contract**. The adapter ignores
unverified graph positions and records this limitation. A graph coordinate is
eligible only with explicit `coordinate_system: 0-based-half-open` and
`coordinate_system_verified: true` source metadata plus a matching GRCh38 build;
the currently inspected legacy nodes do not have those markers. No graph data
is changed or retagged by this feature. Non-rs IDs remain in the table with
unplaced coordinates unless a verified mapping is available. Numbers embedded
in an ID are never treated as positions. Conflicts between genuinely verified
same-build coordinates are withheld, and `coordinate_source` names the provider.

The graph is generated by the existing `project_evidence` function and includes
only actual Neo4j records. S3-only members are not converted into invented graph
edges. Frontend display subsets/layout must be labeled independently from full
membership. Catalog/detail GETs do not run layout or inference.

## Optional summary through the shared answer pipeline

`POST /api/coloc/records/{id}/summary` accepts no request body and returns HTTP
202 with the existing durable result envelope. The initial result has
`status: ready`, `source: {kind: coloc, record_id, snapshot_sha256,
summary_version}`, `answer: ""`, and component states `graph: available`,
`layout: not_requested`, `resources: not_requested`, `answer: pending`.
Read the answer using the existing `GET /api/results/{result_id}` route. Inspect
`component_status.answer` for completion; overall `ready` does not mean that
the answer has finished. The result's source identity is present from the
initial atomic SQLite insert, including if the process stops before scheduling.

Only an explicit POST schedules synthesis. Catalog reads, detail reads, result
polling and health checks never call a model. The frontend may submit this POST
once after opening a detail. The backend retains at most eight detail snapshots
for 30 seconds so that the immediate request reuses the server-observed record
without another graph query, file read or coordinate lookup. A cache miss reads
that exact record through the existing adapter; it never runs a broader gene
template or recomputes coloc. Layout and resource resolution are not repeated.

The scientific adapter supplies the actual graph records, original H0–H4 and
`nsnp`, complete available membership counts, coverage/omissions, source hashes,
and up to five recorded candidates per study ranked by recorded PIP (ties use
nominal P then ID). The rows disclose how many members are omitted from these
top-row excerpts. QTL file members remain tabular evidence and never become
invented Neo4j edges. Missing PIP/P and incomplete membership remain explicit.
The original analysis denominator, credible-set sizes, and shared-member count
are separate observations; no new colocalization probability or causal claim
is calculated by this adapter.

`ResultsRuntime.answer` calls the same `ClaudeGateway.prepare_answer` and
`synthesize` used by PanKagent, with its verified `AnswerSkillRouter` bundle,
existing system instructions, evidence compactor, citation filter and shared
SQLite model budget. The Coloc-specific text is only the user question asking
for a concise interpretation of this exact comparison. No independent model
client, system prompt, paid planner, or literature request is added. The shared
result semaphore, queue admission, provider audit, cancellation and restart
interruption behavior apply. The actual answer profile and configuration are
retained with the result; reference-ID checks are not claim-level verification.

Durable reuse is keyed by the scientific snapshot and the shared model,
bundle hash/version, router version/limit, style version and adapter version.
Observation timestamps and download routing URLs are excluded. A repeated POST
returns the same result even at queue capacity or after a failed, cancelled or
interrupted answer. It never silently spends another inference call. A changed
scientific snapshot or shared answer configuration creates a new identity.
Provider failure or budget exhaustion leaves the scientific detail intact and
marks the answer unavailable or partial. This initial contract has no automatic
retry or regeneration endpoint. Authentication and cross-site mutation policy
are the existing results-service rules; no account or access configuration is
changed.

## Optional Turbo/local extracts

`PANK_RESULTS_COLOC_EXTRACT_MANIFEST` is unset by default. Operators may point it
to a versioned JSON manifest **outside Git** beside small, pre-indexed exact
credible-set extracts. This is not a directory scan, raw-file query engine, or
authorization to mix other studies/builds into existing results.

```json
{
  "version": 1,
  "graph_version": "configured-release",
  "extracts": [{
    "record_id": "64-character-catalog-record-id",
    "role": "qtl",
    "signal_id": "exact-qtl-signal-id",
    "relative_path": "prepared/exact-set.tsv",
    "sha256": "64-character-content-sha256",
    "assembly": "GRCh38",
    "scope": "credible_set",
    "source_version": "verified-source-version",
    "label": "Source and exact signal",
    "source_url": "https://source.example/record"
  }]
}
```

The manifest is capped at 1 MiB; each file at 10 MiB/5,000 rows. Absolute paths,
traversal, symlink components, wrong release/build/signal/scope, duplicate
identities, invalid statistics and checksum mismatches are rejected. Extract
columns are exactly `snp,pip,nominal_p,effect_allele,other_allele,slope,lbf`
(tab-separated). The Gwas role matches `gwas_credible_set_id` instead.
Files with `scope: region` are deliberately rejected: regional observations
cannot be silently marked as credible-set members. The full 15.8 GB islet file
must not be configured as a request-time extract. T2D/hg19 data must remain a
separate future collection. No manifest or raw source is enabled by this change.

## Validation

Run `python -m pytest tests_results/test_coloc.py tests_results/test_coloc_summary.py -q`, followed by
`python -m pytest tests_results -q`. Tests cover pagination beyond graph display
limits, low-H4 retention, exact signal/source joins, partial/denied data, source
and coordinate conflicts, finite posteriors, non-rs variants, extract boundaries,
and authentication/no-inference integration. Live source checks and rendered
browser acceptance must be recorded separately from these offline tests.

### Read-only live source acceptance, 2026-09-21

The isolated adapter read the identity-verified `PanKgraph_08_04` release: 23
recorded T1D comparisons across 17 genes. Exact S3 QTL files and graph GWAS
memberships produced these counts, independently of graph display limits:

| Record | GWAS | QTL | Shared | Union | Verified coordinates |
| --- | ---: | ---: | ---: | ---: | ---: |
| ADCY3 eQTL | 102 | 28 | 5 | 125 | 122 |
| ADCY3 exonQTL | 102 | 6 | 5 | 103 | 100 |
| GSDMB eQTL | 79 | 100 | 67 | 112 | 109 |
| GSDMB exonQTL | 79 | 43 | 24 | 98 | 95 |

Membership is complete for these source-defined sets. Three non-rs variants per
comparison remain unplaced, explicitly reported as partial coordinate coverage.
Original coloc `nsnp` remains 1273 for ADCY3 and 1073 for GSDMB.
