"""Conservative source-row adapters. Raw records remain private PostgreSQL data.

Graph edges mean result membership only, never significance or causal evidence.
Unknown definitions and identifiers are retained as explicit quarantined records.
"""
from __future__ import annotations

import collections
import csv
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re

from cakg.multimodal import make_context, make_edge, make_node, make_record, validate_bundle

from .catalog import API, MODALITIES, accession, classify_analysis, write_json

ADAPTER_VERSION = "pankbase-source-rows-0.1.0"
ENSEMBL = re.compile(r"^(ENSG[0-9]{11})(?:\.([0-9]+))?$")
CELL_NAMES = ("MUC5B_Ductal", "MUC5B+Ductal", "Gamma_Epsilon", "CyclingAlpha", "Cycling_Alpha", "Active_Stellate",
              "ActiveStellate", "Quiescent_Stellate", "QuiescentStellate",
              "A_Stellate", "Q_Stellate", "Endothelial", "Macrophage", "Gamma",
              "Delta", "Ductal", "Acinar", "Alpha", "Beta", "Immune", "Stellate")
METRIC_COLUMNS = {"p-value": "p_value", "pvalue": "p_value", "padj": "adjusted_p_value",
                  "adjusted_p-value": "adjusted_p_value", "baseMean": "base_mean",
                  "log2FoldChange": "source_log2_fold_change", "lfcSE": "source_lfc_se",
                  "stat": "source_statistic", "change": "source_change",
                  "error": "source_error", "ABC.Score": "abc_score"}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024**2), b""):
            h.update(block)
    return h.hexdigest()


def adapter_code_hashes():
    directory = Path(__file__).parent
    return {name: digest(directory / name) for name in ("__init__.py", "__main__.py", "catalog.py", "acquire.py", "adapters.py")}


class GeneReference:
    """Exact unique current symbols/names only; never synonyms or fuzzy matches."""
    def __init__(self, path=None):
        self.sha256 = digest(path) if path else None
        self.genes, candidates = {}, collections.defaultdict(set)
        if path:
            with Path(path).open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    key = row.get(":ID", "")
                    if not ENSEMBL.fullmatch(key) or "." in key:
                        continue
                    self.genes[key] = {"canonical_id": key, "ensembl_gene_id": key,
                                       "name": row.get("hgnc_symbol:String") or row.get("name:String") or key}
                    for field in ("hgnc_symbol:String", "name:String"):
                        if row.get(field):
                            candidates[row[field]].add(key)
        self.symbols = {key: next(iter(ids)) for key, ids in candidates.items() if len(ids) == 1}
        self.ambiguous = {key for key, ids in candidates.items() if len(ids) > 1}

    def resolve(self, value):
        value = value.strip()
        match = ENSEMBL.fullmatch(value)
        if match:
            key = match.group(1)
            return (key, "reference_ensembl") if key in self.genes else (None, "ensembl_not_in_reference")
        if value in self.ambiguous:
            return None, "ambiguous_current_symbol"
        if value in self.symbols:
            return self.symbols[value], "reference_unique_current_symbol"
        return None, "unresolved_gene_identifier"


def source_entry(source, acquired=None):
    acquired = acquired or {}
    downloaded = acquired.get("status") == "downloaded"
    modality = source.get("modality", "catalog_only")
    content = str(source.get("content_type", "")).lower()
    aggregate = modality in {"03_de_markers", "04_clinical", "05_functional", "06_treatment", "08_abc"}
    if modality == "07_atac" and source.get("file_format_type") == "bed3":
        aggregate = True
    if "meta data" in content or "metadata" in content:
        aggregate = False
    metadata = {"acquisition_status": acquired.get("status", "catalog_only"),
                "rows_unavailable": not downloaded,
                "controlled_access": source.get("controlled_access"),
                "access_metadata_status": ("declared_public" if source.get("controlled_access") is False else
                                           "declared_controlled" if source.get("controlled_access") is True else "unknown"),
                "source_status": source.get("status", "unknown"), "modality": modality,
                "privacy_classification": "public_aggregate" if aggregate else "sensitive",
                "metadata_url": source.get("metadata_url", API + "/"),
                "metadata_sha256": hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
                "audit_categories": source.get("audit_categories", {})}
    for key in ("controlled_access", "file_format", "file_format_type", "content_type", "version",
                "assembly", "md5sum", "derived_from", "analysis_step_version"):
        if key in source:
            metadata[key] = source[key]
    for key in ("bytes", "catalog_md5_verified", "retrieved_utc", "reason", "http_status", "private_staging_only", "acquisition_access_basis"):
        if key in acquired:
            metadata[key] = acquired[key]
    return {"file_id": source["accession"], "dataset_id": accession(source.get("file_set")) or "unassigned:" + source["accession"],
            "url": source.get("file_url") or source.get("metadata_url") or API + "/",
            "sha256": acquired.get("sha256") if downloaded else None,
            "status": "released" if downloaded else "unverified", "metadata": metadata}


def table_rows(path, source, max_expanded_bytes=128 * 1024**2):
    """Yield header, exact raw cells and physical line locators, including blanks.

    BED3 has no source header. No synthetic data rows or implied numeric values
    are introduced. Original bytes remain SHA-pinned in private source storage.
    """
    with Path(path).open("rb") as handle:
        magic = handle.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rb") as binary:
        raw = binary.read(max_expanded_bytes + 1)
    if len(raw) > max_expanded_bytes:
        raise ValueError("expanded_file_byte_limit")
    text = raw.decode("utf-8-sig")
    delimiter = "\t" if "\t" in text.splitlines()[0] else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    bed = str(source.get("file_format_type", "")).lower() == "bed3"
    header = ["chrom", "chromStart", "chromEnd"] if bed else next(reader)
    previous = 0 if bed else reader.line_num
    for number, cells in enumerate(reader, 1):
        start = previous + 1
        previous = reader.line_num
        yield header, cells, number, start, previous


def cell_name(analysis):
    text = " ".join(analysis.get("aliases", []))
    for name in CELL_NAMES:
        if re.search(r"(?:^|[_: ])" + re.escape(name) + r"(?:$|[_: ])", text, re.IGNORECASE):
            return name
    return None


def numeric_metrics(row):
    result, problems = {}, []
    for field, name in METRIC_COLUMNS.items():
        value = row.get(field)
        if value is None or value.strip().lower() in {"", "na", "nan", "null", "none"}:
            continue
        try:
            number = float(value)
        except ValueError:
            problems.append(field)
            continue
        if not math.isfinite(number):
            problems.append(field)
        elif name in {"p_value", "adjusted_p_value", "abc_score"} and not 0 <= number <= 1:
            problems.append(field)
        elif name in {"base_mean", "source_lfc_se"} and number < 0:
            problems.append(field)
        else:
            result[name] = number
    return result, problems


def base_bundle(snapshot_id, source):
    return {"snapshot_id": snapshot_id, "manifest": {"adapter_version": ADAPTER_VERSION},
            "nodes": [], "edges": [], "contexts": [], "records": [], "sources": [source], "rejections": []}


def file_chunks(source, acquired, analysis, reference, snapshot_id, chunk_rows=5000,
                max_expanded_file_bytes=128 * 1024**2):
    if chunk_rows < 1:
        raise ValueError("chunk_rows must be positive")
    if digest(acquired["path"]) != acquired["sha256"]:
        raise ValueError("source_sha256_changed_after_acquisition")
    src = source_entry(source, acquired)
    modality = source.get("modality", "unclassified")
    collection = "pankbase:" + modality
    cell = cell_name(analysis)
    cell_node = make_node("Cell_type", "PanKbase.cell_type", cell, {"name": cell}) if cell else None
    context_metadata = {"modality": modality, "source_analysis_id": src["dataset_id"],
                        "source_analysis_description": analysis.get("description", ""),
                        "source_analysis_aliases": analysis.get("aliases", []),
                        "source_file_version": source.get("version"),
                        "source_file_id": src["file_id"], "source_sha256": src["sha256"],
                        "adapter_version": ADAPTER_VERSION, "gene_reference_sha256": reference.sha256,
                        "adapter_source_sha256": digest(__file__),
                        "interpretation": "source_result_membership_only",
                        "effect_scale_status": "unverified", "sample_denominator_status": "unverified"}
    if cell:
        context_metadata["source_cell_type"] = cell
    context = make_context(collection, "source_table", src["dataset_id"], context_metadata,
                           cell_node["id"] if cell_node else None)
    target = None
    if modality in {"04_clinical", "05_functional", "06_treatment"}:
        label = "Exposure" if modality == "06_treatment" else "Phenotype"
        source_name = analysis.get("aliases", [src["dataset_id"]])[0].split(":", 1)[-1]
        target = make_node(label, "PanKbase.analysis_condition", src["dataset_id"],
                           {"name": source_name, "source_description": analysis.get("description", ""),
                            "definition_status": "source_scoped_unresolved"})
    reason_counts = collections.Counter()
    bundle, nodes, edges = base_bundle(snapshot_id, src), {}, {}
    def seed():
        bundle["contexts"] = [context]
        if cell_node:
            nodes[cell_node["id"]] = cell_node
    seed()
    for header, cells, number, start, end in table_rows(acquired["path"], source, max_expanded_file_bytes):
        row = dict(zip(header, cells))
        reasons = []
        objects = []
        if len(cells) != len(header) or len(set(header)) != len(header):
            reasons.append("invalid_or_duplicate_header_width")
        metrics, invalid = numeric_metrics(row)
        row_context = context
        if modality in {"03_de_markers", "04_clinical", "05_functional", "06_treatment"}:
            terms = {"source_" + k: row[k] for k in ("stratum", "formula", "model") if row.get(k)}
            if terms:
                row_context = make_context(collection, "source_table", src["dataset_id"],
                                           {**context_metadata, **terms}, cell_node["id"] if cell_node else None)
                if not any(c["id"] == row_context["id"] for c in bundle["contexts"]):
                    bundle["contexts"].append(row_context)
        if invalid:
            reasons.append("invalid_numeric_metric")
        is_metadata = "meta" in str(source.get("content_type", "")).lower()
        if modality in {"01_metadata", "09_perifusion"} or is_metadata:
            reasons.append("protected_source_staging_only")
        elif modality == "08_abc":
            reasons.append("abc_assembly_and_coordinate_convention_unverified")
        elif modality == "07_atac":
            if str(source.get("file_format_type", "")).lower() != "bed3":
                reasons.append("sample_matrix_context_unverified")
            elif source.get("assembly") != "GRCh38":
                reasons.append("bed_assembly_unverified")
            elif not cell_node:
                reasons.append("cell_type_unresolved")
            elif not reasons:
                try:
                    chrom = row["chrom"].removeprefix("chr")
                    chrom = "MT" if chrom == "M" else chrom
                    left, right = int(row["chromStart"]), int(row["chromEnd"])
                    if chrom not in {str(x) for x in range(1, 23)} | {"X", "Y", "MT"} or left < 0 or right <= left:
                        raise ValueError("invalid interval")
                    region = make_node("Regulatory_region", "GRCh38.bed0", f"{chrom}:{left}-{right}",
                                       {"name": f"GRCh38:{chrom}:{left}-{right}", "chr": chrom,
                                        "start_loc": left, "end_loc": right, "genome_assembly": "GRCh38"})
                    edge = make_edge(region["id"], "HAS_ACCESSIBILITY_RESULT_IN", cell_node["id"],
                                     {"interpretation": "source_peak_membership"})
                    nodes[region["id"]], edges[edge["id"]] = region, edge
                    objects = [region["id"], cell_node["id"], edge["id"]]
                except (ValueError, KeyError):
                    reasons.append("invalid_bed_interval")
        elif modality in {"03_de_markers", "04_clinical", "05_functional", "06_treatment"}:
            gene_value = next((row[k] for k in ("ensembl_ID", "ensembl_id", "gene", "feature", "")
                               if row.get(k) and row[k].strip().lower() not in {".", "na", "nan", "none"}), "")
            gene_key, mapping = reference.resolve(gene_value)
            if not gene_key:
                reasons.append(mapping)
            if not cell_node:
                reasons.append("cell_type_unresolved")
            if not metrics:
                reasons.append("result_metric_columns_unrecognized")
            if not reasons:
                gene = make_node("Gene", "Ensembl", gene_key, reference.genes[gene_key])
                endpoint = target or cell_node
                predicate = ("HAS_TREATMENT_RESULT_WITH" if modality == "06_treatment" else
                             "HAS_ASSOCIATION_RESULT_WITH" if target else "HAS_EXPRESSION_RESULT_IN")
                edge = make_edge(gene["id"], predicate, endpoint["id"], {"interpretation": "source_result_membership"})
                nodes[gene["id"]], nodes[endpoint["id"]], edges[edge["id"]] = gene, endpoint, edge
                objects = [gene["id"], endpoint["id"], edge["id"]]
        else:
            reasons.append("sample_matrix_or_modality_context_unverified")
        reason_counts.update(reasons)
        metadata = {"physical_line_start": start, "physical_line_end": end,
                    "source_row_number": number, "disposition_reasons": reasons}
        record = make_record(collection, row_context["id"], src["file_id"],
                             f"sha256:{src['sha256']}:line:{start}-{end}", metrics,
                             {"columns": header, "values": cells}, metadata, objects,
                             "quarantined" if reasons else "source_reported")
        bundle["records"].append(record)
        if len(bundle["records"]) >= chunk_rows:
            bundle["nodes"], bundle["edges"] = list(nodes.values()), list(edges.values())
            bundle["manifest"]["source_row_counts"] = {src["file_id"]: len(bundle["records"])}
            yield bundle
            bundle, nodes, edges = base_bundle(snapshot_id, src), {}, {}
            seed()
    if bundle["records"]:
        bundle["nodes"], bundle["edges"] = list(nodes.values()), list(edges.values())
        bundle["manifest"]["source_row_counts"] = {src["file_id"]: len(bundle["records"])}
        yield bundle


def build_bundles(catalog, manifest, output_dir, gene_reference=None, chunk_rows=5000,
                  max_expanded_file_bytes=128 * 1024**2):
    """Create closed, bounded chunks and an explicit all-ten-modality index."""
    output_dir = Path(output_dir)
    reference = GeneReference(gene_reference)
    sources = {s["accession"]: s for s in catalog["files"]}
    analyses = {a["accession"]: a for a in catalog["analyses"]}
    for analysis in analyses.values():
        analysis["modality"] = classify_analysis(analysis)
    for source in sources.values():
        source["modality"] = analyses.get(accession(source.get("file_set")), {}).get("modality", "catalog_only")
    bundle_dir = output_dir / "bundles"
    index = {"snapshot_id": manifest["snapshot_id"], "adapter_version": ADAPTER_VERSION,
             "adapter_code_sha256": adapter_code_hashes(),
             "gene_reference_sha256": reference.sha256, "catalog_retrieved_utc": catalog["retrieved_utc"],
             "bundles": [], "files": {}, "source_row_counts": {key: 0 for key in sources},
             "modalities": {m: {"downloaded_files": 0, "records": 0, "projected_records": 0,
                                "quarantined_records": 0, "incomplete_files": []} for m in MODALITIES}}
    all_sources = [source_entry(s, manifest["files"].get(s["accession"])) for s in catalog["files"]]
    catalog_bundle = {"snapshot_id": manifest["snapshot_id"], "manifest": {"adapter_version": ADAPTER_VERSION,
                      "source_row_counts": {s["file_id"]: 0 for s in all_sources}},
                      "nodes": [], "edges": [], "contexts": [], "records": [], "sources": all_sources, "rejections": []}
    validate_bundle(catalog_bundle)
    catalog_path = bundle_dir / "000-catalog.json"
    write_json(catalog_path, catalog_bundle)
    index["bundles"].append({"path": str(catalog_path.relative_to(output_dir)), "sha256": digest(catalog_path)})
    for key, acquired in manifest["files"].items():
        source = sources[key]
        modality = source.get("modality", "unclassified")
        summary = index["modalities"].setdefault(modality, {"downloaded_files": 0, "records": 0,
                     "projected_records": 0, "quarantined_records": 0, "incomplete_files": []})
        if acquired["status"] != "downloaded":
            summary["incomplete_files"].append({"file_id": key, "reason": acquired.get("reason", acquired["status"])})
            continue
        summary["downloaded_files"] += 1
        counts = collections.Counter()
        paths = []
        reasons = collections.Counter()
        try:
            for chunk, bundle in enumerate(file_chunks(source, acquired,
                    analyses.get(accession(source.get("file_set")), {}), reference, manifest["snapshot_id"],
                    chunk_rows, max_expanded_file_bytes)):
                validate_bundle(bundle)
                path = bundle_dir / f"{key}-{chunk:05d}.json"
                write_json(path, bundle)
                paths.append({"path": str(path.relative_to(output_dir)), "sha256": digest(path), "source_file_id": key})
                for record in bundle["records"]:
                    counts["records"] += 1
                    counts["quarantined_records" if record["assertion_status"] == "quarantined" else "projected_records"] += 1
                    reasons.update(record["metadata"]["disposition_reasons"])
        except Exception as exc:
            # A failed file contributes no partial table to the import index.
            summary["incomplete_files"].append({"file_id": key, "reason": str(exc)})
            index["files"][key] = {"status": "build_failed", "reason": str(exc), "unindexed_partial_chunks": len(paths)}
            continue
        index["bundles"].extend(paths)
        index["files"][key] = {"status": "built", **counts, "disposition_counts": dict(reasons)}
        index["source_row_counts"][key] = counts["records"]
        for name in ("records", "projected_records", "quarantined_records"):
            summary[name] += counts[name]
        index["manifest"] = {k: v for k, v in index.items() if k not in {"snapshot_id", "bundles", "manifest"}}
        write_json(output_dir / "bundle_index.json", index)
        print(json.dumps({"file_id": key, "build_status": "built", **counts}), flush=True)
    index["manifest"] = {k: v for k, v in index.items() if k not in {"snapshot_id", "bundles", "manifest"}}
    write_json(output_dir / "bundle_index.json", index)
    return {"bundle_index": str(output_dir / "bundle_index.json"), "modalities": index["modalities"]}
