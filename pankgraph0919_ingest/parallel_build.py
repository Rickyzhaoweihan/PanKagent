"""Build frozen source adapters in at most four processes, then publish one index.

No network or database access. Each worker calls the existing one-file builder
in an isolated directory. Only the complete combined index is importable.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import contextlib
import copy
import io
import json
from pathlib import Path
import re
import time

from cakg.multimodal import validate_bundle

from .adapters import ADAPTER_VERSION, adapter_code_hashes, build_bundles, digest, source_entry
from .catalog import MODALITIES, accession, classify_analysis, utc_now, write_json


def _summary():
    return {"downloaded_files": 0, "records": 0, "projected_records": 0,
            "quarantined_records": 0, "incomplete_files": []}


def _inside(root, relative):
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError("worker_bundle_path_outside_output")
    return path


def _one_file(job):
    """Top-level function so the process-spawn start method is supported."""
    root = Path(job["output_dir"])
    with contextlib.redirect_stdout(io.StringIO()):
        build_bundles(job["catalog"], job["manifest"], root, job["reference"],
                      job["chunk_rows"], job["max_expanded_file_bytes"])
    index = json.loads((root / "bundle_index.json").read_text())
    if index["snapshot_id"] != job["manifest"]["snapshot_id"]:
        raise ValueError("worker_snapshot_mismatch")
    if index["adapter_code_sha256"] != job["adapter_code_sha256"]:
        raise ValueError("adapter_changed_during_parallel_build")
    if index["gene_reference_sha256"] != job["reference_sha256"]:
        raise ValueError("gene_reference_changed_during_parallel_build")
    # Check actual written sources, not only the index's advertised accession.
    for entry in index["bundles"]:
        path = _inside(root, entry["path"])
        if digest(path) != entry["sha256"]:
            raise ValueError("worker_bundle_hash_mismatch")
        with path.open(encoding="utf-8") as handle:
            bundle = json.load(handle)
        if bundle["sources"] != [job["expected_source"]]:
            raise ValueError("worker_source_metadata_mismatch")
        if bundle["snapshot_id"] != index["snapshot_id"]:
            raise ValueError("worker_bundle_snapshot_mismatch")
    return index


def combine_worker_index(index, worker_index, worker_root, output_root, key):
    """Rebase only complete data chunks; the global catalog is authoritative."""
    if worker_index["snapshot_id"] != index["snapshot_id"]:
        raise ValueError("worker_snapshot_mismatch")
    file_info = worker_index["files"].get(key, {"status": "build_failed", "reason": "missing_worker_file_result"})
    index["files"][key] = file_info
    for modality, summary in worker_index["modalities"].items():
        target = index["modalities"].setdefault(modality, _summary())
        for field in ("downloaded_files", "records", "projected_records", "quarantined_records"):
            target[field] += summary[field]
        target["incomplete_files"].extend(summary["incomplete_files"])
    if file_info["status"] != "built":
        return
    staged = []
    for entry in worker_index["bundles"]:
        if "source_file_id" not in entry:
            continue  # Omit every worker's own 000-catalog.json.
        if entry["source_file_id"] != key:
            raise ValueError("worker_chunk_file_identity_mismatch")
        path = _inside(worker_root, entry["path"])
        if not path.is_relative_to(output_root.resolve()):
            raise ValueError("worker_chunk_outside_global_output")
        if digest(path) != entry["sha256"]:
            raise ValueError("worker_bundle_hash_mismatch_at_merge")
        staged.append({**entry, "path": str(path.relative_to(output_root.resolve()))})
    index["source_row_counts"][key] = worker_index["source_row_counts"][key]
    index["bundles"].extend(staged)


def build_parallel(catalog, acquired, output_dir, gene_reference=None, *, workers=4,
                   chunk_rows=5000, max_expanded_file_bytes=512 * 1024**2,
                   input_hashes=None):
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    if chunk_rows < 1 or max_expanded_file_bytes < 1:
        raise ValueError("chunk_rows and expanded byte budget must be positive")
    output_dir = Path(output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("parallel output directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    catalog = copy.deepcopy(catalog)
    analyses = {a["accession"]: a for a in catalog["analyses"]}
    sources = {s["accession"]: s for s in catalog["files"]}
    if set(acquired["files"]) - set(sources):
        raise ValueError("acquired_file_missing_from_catalog")
    for analysis in analyses.values():
        analysis["modality"] = classify_analysis(analysis)
    for source in sources.values():
        source["modality"] = analyses.get(accession(source.get("file_set")), {}).get("modality", "catalog_only")
    code_hashes = adapter_code_hashes()
    runner_sha = digest(__file__)
    reference = str(Path(gene_reference).resolve()) if gene_reference else None
    reference_sha = digest(reference) if reference else None
    global_sources = {key: source_entry(s, acquired["files"].get(key)) for key, s in sources.items()}
    index = {"snapshot_id": acquired["snapshot_id"], "adapter_version": ADAPTER_VERSION,
             "adapter_code_sha256": {**code_hashes, "parallel_build.py": runner_sha},
             "gene_reference_sha256": reference_sha, "catalog_retrieved_utc": catalog["retrieved_utc"],
             "runner": {"workers": workers, "chunk_rows": chunk_rows,
                        "max_expanded_file_bytes": max_expanded_file_bytes, "started_utc": utc_now()},
             "input_sha256": input_hashes or {}, "source_row_counts": {key: 0 for key in sources},
             "bundles": [], "files": {}, "modalities": {key: _summary() for key in MODALITIES}}
    catalog_bundle = {"snapshot_id": index["snapshot_id"], "manifest": {"adapter_version": ADAPTER_VERSION,
                      "source_row_counts": {key: 0 for key in sources}}, "nodes": [], "edges": [],
                      "contexts": [], "records": [], "sources": list(global_sources.values()), "rejections": []}
    validate_bundle(catalog_bundle)
    catalog_path = output_dir / "bundles" / "000-catalog.json"
    write_json(catalog_path, catalog_bundle)
    index["bundles"].append({"path": str(catalog_path.relative_to(output_dir)), "sha256": digest(catalog_path)})
    jobs = []
    for key, result in acquired["files"].items():
        modality = sources[key]["modality"]
        if result["status"] != "downloaded":
            index["files"][key] = {"status": result["status"], "reason": result.get("reason", result["status"])}
            index["modalities"].setdefault(modality, _summary())["incomplete_files"].append(
                {"file_id": key, "reason": result.get("reason", result["status"])})
            continue
        if not re.fullmatch(r"PKBFI[A-Z0-9_]+", key):
            raise ValueError("invalid_source_accession_for_output_path")
        analysis = analyses.get(accession(sources[key].get("file_set")))
        worker_manifest = {**acquired, "files": {key: result}}
        jobs.append({"key": key, "output_dir": str(output_dir / "per_file" / key),
                     "catalog": {"retrieved_utc": catalog["retrieved_utc"], "files": [sources[key]],
                                 "analyses": [analysis] if analysis else []},
                     "manifest": worker_manifest, "reference": reference, "reference_sha256": reference_sha,
                     "adapter_code_sha256": code_hashes, "expected_source": global_sources[key],
                     "chunk_rows": chunk_rows, "max_expanded_file_bytes": max_expanded_file_bytes})
    jobs.sort(key=lambda j: (-j["manifest"]["files"][j["key"]].get("bytes", 0), j["key"]))
    print(json.dumps({"parallel_build": "started", "workers": workers, "downloaded_files": len(jobs),
                      "catalog_sources": len(sources)}), flush=True)
    started, results = time.monotonic(), {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        future_jobs = {pool.submit(_one_file, job): job for job in jobs}
        for future in as_completed(future_jobs):
            job = future_jobs[future]
            key = job["key"]
            # Integrity failures stop final publication; no partial global index.
            result = future.result()
            results[key] = result
            info = result["files"].get(key, {})
            print(json.dumps({"file_id": key, "build_status": info.get("status", "missing"),
                              "records": info.get("records", 0), "completed_files": len(results),
                              "total_files": len(jobs), "elapsed_seconds": round(time.monotonic() - started, 1)}), flush=True)
    for key in sorted(results):
        combine_worker_index(index, results[key], output_dir / "per_file" / key, output_dir, key)
    if adapter_code_hashes() != code_hashes or digest(__file__) != runner_sha:
        raise ValueError("implementation_changed_before_index_publication")
    if reference and digest(reference) != reference_sha:
        raise ValueError("reference_changed_before_index_publication")
    index["runner"].update(completed_utc=utc_now(), elapsed_seconds=round(time.monotonic() - started, 1))
    index["manifest"] = {k: v for k, v in index.items() if k not in {"snapshot_id", "bundles", "manifest"}}
    write_json(output_dir / "bundle_index.json", index)
    return index


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--gene-reference", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--chunk-rows", type=int, default=5000)
    parser.add_argument("--max-expanded-file-bytes", type=int, default=512 * 1024**2)
    args = parser.parse_args(argv)
    catalog_path = args.source_dir / "catalog.json"
    acquired_path = args.source_dir / "acquisition_manifest.json"
    index = build_parallel(json.loads(catalog_path.read_text()), json.loads(acquired_path.read_text()),
                           args.output_dir, args.gene_reference, workers=args.workers, chunk_rows=args.chunk_rows,
                           max_expanded_file_bytes=args.max_expanded_file_bytes,
                           input_hashes={"catalog": digest(catalog_path), "acquisition_manifest": digest(acquired_path)})
    print(json.dumps({"bundle_index": str(args.output_dir / "bundle_index.json"),
                      "records": sum(index["source_row_counts"].values()), "modalities": index["modalities"]}), flush=True)


if __name__ == "__main__":
    main()
