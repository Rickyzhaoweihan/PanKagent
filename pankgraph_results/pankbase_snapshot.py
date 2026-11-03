"""Build an explicit, resumable source snapshot; never fetch user-supplied URLs."""

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request


def download(item, root):
    destination = root / item["path"]
    record = destination.with_name(destination.name + ".provenance.json")
    if destination.exists() and record.exists():
        previous = json.loads(record.read_text())
        with destination.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if (
            previous["source_url"] == item["source_url"]
            and previous["bytes"] == destination.stat().st_size
            and previous["sha256"] == digest
        ):
            return previous
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    digest = hashlib.sha256()
    with urllib.request.urlopen(
        item["source_url"], timeout=180
    ) as response, temporary.open("wb") as out:
        if (
            item.get("expected_object_version")
            and response.headers.get("x-amz-version-id")
            != item["expected_object_version"]
        ):
            raise ValueError("Source version changed: " + item["id"])
        expected = response.headers.get("Content-Length")
        count = 0
        while chunk := response.read(4 * 1024 * 1024):
            count += len(chunk)
            if count > item.get("maximum_bytes", 12 * 1024**3):
                raise ValueError("Source exceeded declared maximum: " + item["id"])
            out.write(chunk)
            digest.update(chunk)
        if expected and int(expected) != count:
            raise ValueError("Incomplete source: " + item["id"])
        result = {
            **item,
            "bytes": count,
            "sha256": digest.hexdigest(),
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "etag": response.headers.get("ETag"),
            "source_last_modified": response.headers.get("Last-Modified"),
            "object_version": response.headers.get("x-amz-version-id"),
        }
    temporary.replace(destination)
    record.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"id": item["id"], "bytes": count, "status": "saved"}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    registry = json.loads(args.registry.read_text())
    args.root.mkdir(parents=True, exist_ok=True)
    results, failures = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(download, item, args.root): item for item in registry["assets"]
        }
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                results.append(future.result())
            except Exception as error:
                failures.append({"id": item["id"], "error": str(error)})
            status = {
                **registry,
                "assets": results,
                "failures": failures,
                "expected_assets": len(registry["assets"]),
                "complete": False,
            }
            (args.root / "manifest.progress.json").write_text(
                json.dumps(status, indent=2)
            )
    status["complete"] = not failures
    (args.root / "manifest.json").write_text(json.dumps(status, indent=2) + "\n")
    if failures:
        raise SystemExit("Snapshot incomplete: " + str(len(failures)))


if __name__ == "__main__":
    main()
