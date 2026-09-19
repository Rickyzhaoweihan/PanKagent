# PanKbase acquisition and conservative source-row adapters

This package prepares real PanKbase source records for the isolated 0919 release.
It does not connect to a database or deploy an API. The context-aware standard's
`cakg.multimodal` module validates and loads its bundles. Synthetic fixtures exist
only in `tests_0919/test_ingest.py`.

## Run privately

Use a private directory outside Git. The command writes directories with mode
0700 and catalog, source, manifest and bundle files with mode 0600. Never publish
the raw catalog, donor-linked audit messages, source tables, reference export or
bundle files. Source metadata retained in the catalog omits audit detail/path
messages; it is still an internal acquisition artifact.

```bash
python -m pankgraph0919_ingest \
  --output-dir /db/pankgraph0919/sources/pankbase-20260919-r1 \
  --scope all-principal --acquire-only \
  --max-file-bytes 33554432 --max-total-bytes 2147483648
```

The command first refreshes principal/resource AnalysisSets and every released
File, paging with exact total/identity accounting. The September 19, 2026 refresh
returned 11,478 released files. The acquisition candidate count is a file count,
not a count of independent studies. `all-principal` selects all released principal
TabularFiles plus the explicit pilot list; it does not silently include thousands
of intermediate QC files. The 490 initial candidates included 42 clinical files
whose newer aliases use `diffExpr_allTraits_`; these are now classified correctly.

Default acquisition requires `controlled_access=false`, a released TabularFile,
an approved HTTPS direct `file_url`, a complete HTTP 200 response and a full-file
hash. It does not synthesize the broken `@@download` route. It checks catalog MD5
when available and always computes SHA-256. Partial, truncated, oversized or
hash-mismatched files are never promoted to complete inputs. Network failures
and 429/5xx receive bounded retries; 404 does not. HTTP status, byte budgets and
explicit failure reasons remain in the private acquisition manifest.

Three selected matrices can be handled separately after inspection:

```bash
python -m pankgraph0919_ingest \
  --output-dir /db/pankgraph0919/sources/pankbase-20260919-r1 \
  --scope selected --retry-incomplete --acquire-only --stage-public-matrix \
  --max-file-bytes 536870912 --max-total-bytes 2147483648
```

`--retry-incomplete` reuses the timestamped catalog, verifies completed input
hashes, retains those files and counts their bytes against the total budget.
The explicit matrix option admits only PKBFI7107QVIN for private staging from its
public URL. It preserves its missing access flag and declared-format discrepancy;
it never admits an explicitly controlled file or an RDS atlas. ATAC raw/CPM
TabularFiles already declare public access and only require the larger byte
budget. Public reachability is recorded separately from catalog access metadata.

```bash
python -m pankgraph0919_ingest \
  --output-dir /db/pankgraph0919/sources/pankbase-20260919-r1 \
  --build-only --gene-reference /db/pankgraph0919/sources/genes.node.csv \
  --chunk-rows 5000 --max-expanded-file-bytes 536870912
```

Install the matching context-aware standard so `cakg.multimodal` is importable.
`bundle_index.json` supplies `snapshot_id`, a combined `manifest`, and `bundles`
entries containing a relative `path`, SHA-256 and optional `source_file_id`.
The catalog bundle comes first. Subsequent bundles are closed chunks: each has
all referenced sources, contexts, nodes and edges. The loader must verify each
bundle hash, resolve paths relative to the index directory, and call
`load_postgres_files` once against an empty isolated schema. Chunks share
identical canonical objects. This is not an append-to-live workflow.

## Scientific and privacy boundaries

| Modality | Current adapter behavior |
|---|---|
| 01 donor/biosample metadata | Preserve every raw row privately; quarantine; no clinical graph attributes or donor joins. |
| 02 scRNA expression/composition | Preserve accessible sample-table rows privately; quarantine until matrix/sample context is resolved. Two selected Beta count URLs returned 404. |
| 03 DE/markers | Reference-resolved Gene to source Cell_type result membership; preserve statistical source fields and unresolved rows. Sample metadata is private staging. |
| 04 clinical associations | Reference-resolved Gene to source-analysis-qualified Phenotype; no causal interpretation or cross-source trait merge. |
| 05 functional associations | Same membership pattern; analyte/unit/stimulus/window definitions remain source scoped. Exact formula/model/stratum are retained when present. |
| 06 treatment response | Reference-resolved Gene to source-qualified Exposure; source treatment text is searchable without inventing dose, direction or mechanism. |
| 07 ATAC | Declared GRCh38 BED3 intervals must validate as zero-based half-open, canonical chromosome intervals before peak membership edges. Sample matrices remain private, unresolved records. |
| 08 ABC | Preserve all prediction rows with literal ABC score; quarantine coordinates/links until assembly and convention are verified. No enhancer-gene graph claim. |
| 09 perifusion | Preserve raw measurements privately; no clinical graph attributes, donor joins or assumed functional-trait normalization. |
| 10 bulk RNA | Selected public TSV can be explicitly staged privately despite MTX catalog format and missing access flag; no aggregate calculation or inferred sample join. |

Graph predicates are `HAS_EXPRESSION_RESULT_IN`,
`HAS_ASSOCIATION_RESULT_WITH`, `HAS_TREATMENT_RESULT_WITH` and
`HAS_ACCESSIBILITY_RESULT_IN`. They express membership in a source result, not
significance, detection, direction, independence or causality. Conditions,
source versions, context and measurements remain in PostgreSQL evidence records.
Graph identifiers follow the standard's canonical SHA-256 identity contract.

Gene resolution uses a SHA-pinned `genes.node.csv` reference. Source Ensembl IDs
must exist in that reference; recognized version suffixes are retained in raw
data while the reference gene ID is canonical. Exact current `hgnc_symbol` and
`name` values are eligible only when uniquely associated with one gene. No fuzzy
matching, synonym expansion or invented HGNC identifiers is performed. Missing
`ensembl_ID='.'` permits use of the actual `feature` column; an explicit unknown
Ensembl identifier remains unresolved. Source cell-type spellings remain source
qualified; ontology mappings are not invented.

Numeric fields have intentionally literal names. `change/error` become
`source_change/source_error`; `log2FoldChange/lfcSE/stat` become
`source_log2_fold_change/source_lfc_se/source_statistic`. They are not silently
renamed to a verified effect estimate or standard error. Exact p-value headers
map to `p_value/adjusted_p_value`, `baseMean` to `base_mean`, and `ABC.Score` to
`abc_score`. Invalid/nonfinite metrics remain in the raw row and quarantine the
record. No missing value becomes zero. Unrecognized columns remain raw data.

Raw records preserve the exact header/cell strings, physical line range and
full-file SHA. Duplicate headers, row-width errors and blank data rows receive
explicit dispositions; no silent row cap or deduplication is used. Every parsed
row has one record. Quarantined records are counted once and have no graph
object links. File build errors exclude all its partial chunks from the index.
Combined source-row accounting includes zero for catalog-only files, which are
marked `rows_unavailable`; it never treats a file-level skip as one source row.

Source privacy classification is `sensitive` for metadata, sample matrices,
composition and measured perifusion. Aggregate statistical result tables and
BED3 peak tables are `public_aggregate`. Classification controls raw-record API
visibility; acquisition and staging alone do not authorize public release.

## Verified source checkpoints

These are September 19 full-file or bounded-preview observations, not study
sample sizes. The private machine manifests are authoritative for each build.

| Source | Observation |
|---|---|
| [PKBFI5887OGWB](https://data.pankbase.org/tabular-files/PKBFI5887OGWB/) | Full DE table, 13,191 rows, header `gene,baseMean,log2FoldChange,lfcSE,stat,pvalue,padj`. |
| [PKBFI1363TEHQ](https://data.pankbase.org/tabular-files/PKBFI1363TEHQ/) | Full marker table, 14,549 rows, same source header. |
| [PKBFI0491GBGO](https://data.pankbase.org/tabular-files/PKBFI0491GBGO/) | Full HbA1c result table, 13,334 rows, same source header, now directly verified. |
| [PKBFI1772ZSFL](https://data.pankbase.org/tabular-files/PKBFI1772ZSFL/) | Full functional table, 13,291 rows; `feature,ensembl_ID,change,error,p-value,adjusted_p-value,baseMean,stratum,formula,model`. |
| [PKBFI4372KNNK](https://data.pankbase.org/tabular-files/PKBFI4372KNNK/) | Full treatment table, 12,891 rows, same literal coefficient header. |
| [PKBFI7874FAIJ](https://data.pankbase.org/tabular-files/PKBFI7874FAIJ/) | Full GRCh38 BED3, 132,138 headerless interval rows. |
| [PKBFI8305YDIV](https://data.pankbase.org/tabular-files/PKBFI8305YDIV/) | Full ABC table, 85,425 prediction rows, assembly unresolved. |
| [PKBFI8117XDTM](https://data.pankbase.org/tabular-files/PKBFI8117XDTM/) / [PKBFI3163ZUPS](https://data.pankbase.org/tabular-files/PKBFI3163ZUPS/) | Range previews: 45,223,742 / 152,056,974 bytes, interval index plus 41 sample columns. Coordinate convention is not inherited from the separate BED file. |
| [PKBFI7107QVIN](https://data.pankbase.org/matrix-files/PKBFI7107QVIN/) | Public Range 206, 39,583,764 bytes; actual TSV begins with sample columns and metadata rows. Declared MTX/access semantics unresolved. |

Outstanding curation is tracked in [PanKgraph issue 1](https://github.com/RingoMao/PanKgraph_codex/issues/1),
[access/format issue 2](https://github.com/RingoMao/PanKgraph_codex/issues/2), and the
[standard extension issue](https://github.com/RingoMao/context-aware-kg-standard/issues/1).
The September 18 integration plans remain proposals; they do not override the
implemented context-aware standard or turn its synthetic prototypes into data.
