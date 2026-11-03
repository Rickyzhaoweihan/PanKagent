# Independent PanKgraph module data

Five modules use one read-only, same-origin API: scBrowser, Genome Browser, Differential Expression, Functional Browser, and PCA Explorer. No request handler contacts PanKbase, GitHub, S3, or a CDN. Refreshing a source is an explicit offline operation; failure does not trigger an upstream fallback.

## Source snapshot

The three JSON registries pin the original URLs and releases. `pankgraph_results/pankbase_snapshot.py` records retrieval UTC, bytes, SHA-256, HTTP ETag, Last-Modified and S3 version where supplied. It streams downloads and verifies both size and hash when resuming. Snapshot manifests must be complete before delivery construction.

The 20261103 snapshot contains 134 registered source/prepared/runtime assets (15,358,611,152 bytes), plus a derived expression store. It retains:

- scRNA v3.4 Seurat RNA `data` and `counts` in the original RDS; all 448,935 cells and 19,546 genes. Matrix export stores 1,646,749,582 nonzero normalized values as Float64 and cell indices as Uint32. The original RDS is retained unchanged.
- Complete functional v1 metadata, trait and time-course inputs, normalization variants, association and skipped-analysis files.
- Twelve ATAC bigWig tracks, twelve peak files and their twelve tabix indexes.
- Six DE comparisons and heatmaps, twelve source PCA result groups, prepared browser payloads, hg38 sequence and UCSC RefGene transcripts.
- Pinned genome-browser runtime dependencies. The original A-Frame URL returned 403, so the same 0.8.0 published package distribution is retained unminified. The registry records the alternate URL. WashU's annotation and hg38 sequence URLs are redirected to this snapshot; other code is retained.

These resources are distinct releases, not a matched cohort. Software licenses do not establish data licenses. Original URLs, source terms and provenance remain attached; no blanket data-license claim is made. The service is loopback-only and does not create public access or change an existing authentication policy.

## Reproduce and validate

Use a Python 3.13 environment with `requirements.txt`, and R 4.4.3 with Matrix 1.7.6 for the one-time source export. Supply absolute paths through environment variables; datasets and donor records never belong in Git.

1. Run `python -m pankgraph_results.pankbase_snapshot --registry <registry.json> --root <snapshot>` for the primary registry; use `<snapshot>/supplement` and `<snapshot>/runtime-source` for the other two registries.
2. Copy the content-addressed `data/` and `atlas/` prepared assets and source `PCA_results/` into the snapshot. Their refresh/validation scripts and pinned source commits are maintained in the frontend repository. Normal frontend builds no longer download data.
3. Run `prepare_expression.py <snapshot>`; it writes cell IDs and three source reference vectors locally. Run `Rscript export_expression.R <snapshot>` to match every unique atlas NAME to Seurat column names before transposing. No positional assumption or sampling is used.
4. Run `validate_expression.py <snapshot>`. It validates every reference cell for CFTR, INS and GCG, all gene spans, matrix file sizes and SHA-256 checksums before atomically activating `expression/index.json`. The three full vectors match the existing BioIndex representation exactly after its three-decimal rounding. The service performs that rounding for delivery; full-precision values stay on disk.
5. Run `build_delivery.py <snapshot>` to create `delivery-manifest.json`, the RefGene lookup, the endpoint-adapted genome runtime and precompressed text assets. Activation requires the complete source manifests. Metadata includes raw and derived hashes, transformation notes, alignment and validation results. `GET /api/pankbase-data/v1/manifest` exposes this metadata.
6. Run `python -m pytest -q tests/test_pankbase_data.py`. Tests cover byte ranges, HEAD/ETag, missing/truncated files, unsupported mutations, path allowlisting, expression precision, identity rejection and transcript/locus lookup.

Full expression export requires substantial RAM and temporary disk; the inspected source has 1.65 billion nonzero values. The steady-state service reads only one gene slice, not the full matrix.

## Running and recovery

Use `start.sh` with `PANKBASE_DATA_ROOT`, `PANKBASE_PYTHON`, `PANKBASE_SERVICE_DIR`, and `PANKBASE_LOG_DIR`. `PANKBASE_PORT` defaults to 8818. It refuses to replace an existing listener, uses loopback only and writes a private PID/log. This deployment uses a service-owned background process; it is not configured to restart automatically after a host reboot. Root service-manager permissions were unavailable, so no systemd unit was installed.

Stop only the PID in this service's private `service.pid`, after verifying its command contains `uvicorn pankbase_data:create_app` and `--port 8818`. To roll back, stop that owned process, select the previous immutable snapshot/code pair and run the same launcher. Never restart ports 8794, 8795, 8796 or shared databases.

`/health/ready` returns 503 until the validated expression index exists. Asset requests use an explicit registry, immutable ETags, size checks, single byte ranges and a gzip representation for text when accepted. Binary range requests retain identity encoding. There are no write or arbitrary-URL endpoints.

For the local demo, forward `127.0.0.1:18818` to jieliu3 `127.0.0.1:8818` via SSH, then run the frontend preview with `PANKBASE_DATA_PORT=18818`. An authenticated public reverse-proxy route is a separate operator action; none was activated by this work.
