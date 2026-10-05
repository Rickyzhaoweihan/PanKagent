"""Private, bounded source acquisition and per-file bundle preparation."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
import sys
import urllib.error

from .acquire import AcquisitionBlocked, Downloader
from .catalog import (CatalogClient, MODALITIES, PILOT_FILES, accession,
                      refresh_catalog, utc_now, write_json)


def candidates(catalog, scope):
    analyses = {a["accession"]: a for a in catalog["analyses"]}
    files = {f["accession"]: f for f in catalog["files"]}
    selected = [files[a] for a in PILOT_FILES if a in files]
    if scope == "all-principal":
        for key, source in sorted(files.items()):
            parent = analyses.get(accession(source.get("file_set")), {})
            if (key not in PILOT_FILES and parent.get("status") == "released"
                    and parent.get("file_set_type") == "principal analysis"
                    and "TabularFile" in source.get("@type", [])):
                selected.append(source)
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scope", choices=("selected", "all-principal"), default="selected")
    parser.add_argument("--max-file-bytes", type=int, default=32 * 1024**2)
    parser.add_argument("--max-total-bytes", type=int, default=2 * 1024**3)
    parser.add_argument("--catalog-only", action="store_true")
    parser.add_argument("--acquire-only", action="store_true")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--retry-incomplete", action="store_true", help="Reuse saved catalog; preserve and checksum-verify complete files")
    parser.add_argument("--stage-public-matrix", action="store_true", help="Explicit private staging of selected bulk PKBFI7107QVIN only; no controlled files or RDS")
    parser.add_argument("--chunk-rows", type=int, default=5000)
    parser.add_argument("--max-expanded-file-bytes", type=int, default=128 * 1024**2)
    parser.add_argument("--gene-reference", type=Path)
    parser.add_argument("--snapshot-id", default="pankbase-20260919-r1")
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output_dir.chmod(0o700)
    catalog_path = args.output_dir / "catalog.json"
    manifest_path = args.output_dir / "acquisition_manifest.json"
    if args.build_only:
        catalog = json.loads(catalog_path.read_text())
        manifest = json.loads(manifest_path.read_text())
    else:
        if args.retry_incomplete:
            catalog = json.loads(catalog_path.read_text())
            previous = json.loads(manifest_path.read_text())
        else:
            catalog = refresh_catalog(CatalogClient())
            write_json(catalog_path, catalog)
            previous = {"files": {}}
        selected = candidates(catalog, args.scope)
        manifest = {"snapshot_id": args.snapshot_id, "started_utc": utc_now(),
                    "catalog_retrieved_utc": catalog["retrieved_utc"],
                    "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
                    "scope": args.scope, "catalog_released_files": len(catalog["files"]),
                    "selected_files": len(selected), "files": dict(previous["files"]),
                    "budgets": {"max_file_bytes": args.max_file_bytes,
                                "max_total_bytes": args.max_total_bytes}}
        counts = collections.Counter(f["modality"] for f in selected)
        print(json.dumps({"catalog_released_files": len(catalog["files"]),
                          "candidate_files": len(selected), "modalities": dict(counts)}), flush=True)
        write_json(manifest_path, manifest)
        if args.catalog_only:
            return 0
        downloader = Downloader(args.output_dir / "downloads", args.max_file_bytes, args.max_total_bytes,
                                stage_public_matrix=args.stage_public_matrix)
        if args.retry_incomplete:
            for prior in previous["files"].values():
                if prior.get("status") == "downloaded":
                    path = Path(prior["path"])
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != prior["sha256"]:
                        raise ValueError("Completed source changed; use a new output directory")
                    downloader.total_bytes += prior["bytes"]
        for source in selected:
            key = source["accession"]
            if manifest["files"].get(key, {}).get("status") == "downloaded":
                continue
            result = {"modality": source["modality"], "dataset_id": accession(source.get("file_set"))}
            try:
                result.update(downloader.acquire(source))
                result["status"] = "downloaded"
            except AcquisitionBlocked as exc:
                result.update(status="blocked", reason=exc.reason, details=exc.details)
            except urllib.error.HTTPError as exc:
                result.update(status="failed", reason="http_error", http_status=exc.code)
            except Exception as exc:
                result.update(status="failed", reason=type(exc).__name__)
            manifest["files"][key] = result
            manifest["downloaded_bytes"] = downloader.total_bytes
            write_json(manifest_path, manifest)
            print(json.dumps({"file_id": key, "status": result["status"],
                              "bytes": result.get("bytes"), "reason": result.get("reason")}), flush=True)
        manifest["completed_utc"] = utc_now()
        write_json(manifest_path, manifest)
    if not args.acquire_only:
        from .adapters import build_bundles
        result = build_bundles(catalog, manifest, args.output_dir, args.gene_reference,
                               args.chunk_rows, args.max_expanded_file_bytes)
        print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
