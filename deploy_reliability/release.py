"""Build/verify a deterministic, explicitly scoped PanKgraph demo release.

No extraction, dependency installation, inference, or deployment occurs here.
The archive digest must be delivered separately when verifying authenticity.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile

VERSION = 1
MANIFEST = "release-manifest.json"
PROJECT_FILES = frozenset("docs/pankagent-vnext/grounded-planning-2026-09-09/" + name for name in (
    "outbound_privacy_guard.py", "run_fresh_e2e.py", "run_gpu_pool.py",
    "manifest.frozen.json", "manifest.frozen.sha256",
    "manifest.supplement.frozen.json", "manifest.supplement.frozen.sha256",
    "gpu-pool.manifest.frozen.json", "gpu-pool.manifest.frozen.sha256",
))
UX_AUDIT_FILES = frozenset("ux_audit/" + name for name in (
    "run_pairs.py", "freeze_manifest.py", "collect_history.py", "summarize_repairs.py",
    "build_bundle.py", "README.md", "recovery_checks.py", "build_release_inventory.py",
))
BACKEND_DIRS = frozenset({
    "pankagent_vnext", "pankgraph_results", "pankgraph_health",
    "deploy_vnext", "deploy_results", "deploy_health", "deploy_reliability",
    "tests_vnext", "tests_results", "tests_health", "tests_reliability",
})
REQUIREMENTS = frozenset({
    "requirements-vnext.txt", "requirements-vnext.lock",
    "requirements-results.txt", "requirements-results.lock",
    "requirements-health.txt", "requirements-health.lock",
    "requirements-reliability.txt", "requirements-reliability.lock",
})
SOURCE_SUFFIXES = frozenset({".py", ".json", ".md", ".html", ".js", ".css", ".sh", ".service", ".example", ".txt", ".lock"})
FRONTEND_SUFFIXES = frozenset({".html", ".js", ".css", ".json", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map", ".txt", ".webmanifest", ".md", ".pdf"})
FRONTEND_DIRS = frozenset({"src", "public", "scripts"})
FRONTEND_CONFIG = frozenset({"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "craco.config.js", "webpack.hirn.js", "config-overrides.js", "babel.config.js", "postcss.config.js",
    "tailwind.config.js", "tsconfig.json", "jsconfig.json", ".nvmrc", ".browserslistrc"})
FRONTEND_SOURCE_SUFFIXES = FRONTEND_SUFFIXES | SOURCE_SUFFIXES | {".jsx", ".ts", ".tsx", ".scss", ".sass", ".less", ".cjs", ".mjs", ".pdf", ""}
EXCLUDED_PARTS = frozenset({"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv", "var", "state", "logs", "private", "datasets", "data"})
SECRET_NAMES = frozenset({"access.txt", "credentials.json", "credentials.txt", "secrets.json", "secrets.txt", ".ds_store"})
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 50000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def safe_path(name):
    if not isinstance(name, str) or not name or "\\" in name or any(ord(c) < 32 for c in name):
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and str(path) == name and not any(part in {".", ".."} for part in path.parts)


def private_path(name):
    return any(part.casefold() in EXCLUDED_PARTS or part.casefold() in SECRET_NAMES
               or part.startswith("._") or part.casefold().startswith(".env") or part.casefold().endswith(".env")
               for part in PurePosixPath(name).parts)


def allowed_backend(name):
    if not safe_path(name) or private_path(name):
        return False
    parts = PurePosixPath(name).parts
    return name in REQUIREMENTS | UX_AUDIT_FILES or (len(parts) > 1 and parts[0] in BACKEND_DIRS
                                    and PurePosixPath(name).suffix in SOURCE_SUFFIXES)


def allowed_payload(name):
    if not safe_path(name) or private_path(name):
        return False
    if name in PROJECT_FILES:
        return True
    root, _, relative = name.partition("/")
    if root == "backend":
        return allowed_backend(relative)
    if root == "frontend-source":
        parts = PurePosixPath(relative).parts
        return relative in FRONTEND_CONFIG or (len(parts) > 1 and parts[0] in FRONTEND_DIRS
                                               and PurePosixPath(relative).suffix.lower() in FRONTEND_SOURCE_SUFFIXES)
    return root == "frontend" and bool(relative) and PurePosixPath(relative).suffix.lower() in FRONTEND_SUFFIXES


def collect_tree(root, candidates, prefix):
    files = {}
    for candidate in sorted(candidates):
        path = root
        for component in PurePosixPath(candidate).parts:
            path = path / component
            if path.is_symlink():
                raise ValueError("Symlink is not permitted in release inputs: " + prefix + candidate)
        if path.is_symlink():
            raise ValueError("Symlink is not permitted in release inputs: " + prefix + candidate)
        if not path.exists():
            continue
        entries = [path] if path.is_file() else path.rglob("*")
        for entry in sorted(entries):
            relative = entry.relative_to(root).as_posix()
            # Never follow or package a symlink, including a directory symlink.
            if entry.is_symlink():
                raise ValueError("Symlink is not permitted in release inputs: " + prefix + relative)
            if not entry.is_file():
                continue
            name = prefix + relative
            if not allowed_payload(name):
                continue
            if entry.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Release input exceeds per-file byte limit")
            data = entry.read_bytes()
            mode = 0o755 if entry.stat().st_mode & 0o111 else 0o644
            files[name] = (data, mode)
    return files


def git_info(directory, eligible):
    def git(*args):
        return subprocess.run(["git", "-C", str(directory), *args], check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL).stdout
    try:
        root = Path(git("rev-parse", "--show-toplevel").decode().strip())
        commit = git("rev-parse", "HEAD").decode().strip()
        branch = git("rev-parse", "--abbrev-ref", "HEAD").decode().strip()
        records = git("status", "--porcelain=v1", "--untracked-files=all", "-z").decode().split("\0")
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return {"state": "unavailable", "commit": None, "dirty": None, "dirty_files": []}
    changes, outside, i = [], 0, 0
    while i < len(records):
        record = records[i]
        i += 1
        if not record:
            continue
        status, name = record[:2], record[3:]
        original = None
        if "R" in status or "C" in status:
            original = records[i]
            i += 1
        if not safe_path(name) or not eligible(name):
            outside += 1
            continue
        file = root / name
        if file.is_symlink():
            raise ValueError("Dirty source is a symlink: " + name)
        item = {"path": name, "status": status, "sha256": digest(file.read_bytes()) if file.is_file() else None}
        if original and safe_path(original) and eligible(original):
            item["original_path"] = original
        changes.append(item)
    return {"state": "observed", "commit": commit, "branch": branch, "dirty": bool(changes or outside),
            "dirty_files": sorted(changes, key=lambda item: item["path"]), "unpackaged_dirty_entries": outside}


def frontend_source(name):
    return allowed_payload("frontend-source/" + name)


def schema_info(files):
    return {name: {"sha256": digest(data), "packaged": True}
            for name, (data, _) in sorted(files.items())
            if name.endswith(".json") and ("schema" in name or name.endswith("/manifest.json"))}


def runtime_info(path):
    if path is None:
        return {"state": "not_supplied"}
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Invalid runtime inventory input")
    value = json.loads(path.read_text())
    runtime = value.get("runtime", value) if isinstance(value, dict) else None
    if not isinstance(runtime, dict) or not isinstance(runtime.get("python"), str) or not isinstance(runtime.get("packages"), dict):
        raise ValueError("Runtime inventory requires python string and packages mapping")
    packages = runtime["packages"]
    if any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", k) or not isinstance(v, str)
           or len(v) > 256 or any(ord(c) < 32 for c in v) for k, v in packages.items()):
        raise ValueError("Invalid runtime package inventory")
    normalized = {"python": runtime["python"], "packages": packages}
    for key in ("archive_sha256", "base_prefix", "replay_boundary", "platform"):
        if key not in runtime:
            continue
        value = runtime[key]
        if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid runtime replay metadata")
        if key == "archive_sha256" and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Invalid runtime archive digest")
        normalized[key] = value
    return {"state": "supplied_observation", **normalized, "inventory_sha256": digest(canonical(normalized))}


def build(backend_root, frontend_build, output, runtime_inventory=None, frontend_root=None):
    backend_root, frontend_build, output = Path(backend_root), Path(frontend_build), Path(output)
    if backend_root.is_symlink() or frontend_build.is_symlink() or not backend_root.is_dir() or not frontend_build.is_dir():
        raise ValueError("Backend root and explicit frontend build must be real directories")
    frontend_root = Path(frontend_root) if frontend_root is not None else frontend_build.parent
    if frontend_root.is_symlink() or not (frontend_root / "src").is_dir() or not (frontend_root / "package.json").is_file():
        raise ValueError("Frontend source src/ and package.json are required; supply --frontend-source if separate")
    if not any((frontend_root / name).is_file() for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")):
        raise ValueError("Frontend dependency lock is required")
    backend_root = backend_root.resolve()
    project_root = backend_root.parent
    files = collect_tree(backend_root, BACKEND_DIRS | REQUIREMENTS | UX_AUDIT_FILES, "backend/")
    files.update(collect_tree(project_root, PROJECT_FILES, ""))
    files.update(collect_tree(frontend_build, ["."], "frontend/"))
    files.update(collect_tree(frontend_root, FRONTEND_DIRS | FRONTEND_CONFIG, "frontend-source/"))
    required = {"backend/pankagent_vnext/__init__.py", "backend/pankgraph_results/__init__.py",
                "backend/pankgraph_health/__init__.py", "frontend/index.html", "frontend-source/package.json"} | PROJECT_FILES | {"backend/" + name for name in UX_AUDIT_FILES}
    if required - files.keys():
        raise ValueError("Required release inputs missing: " + ", ".join(sorted(required - files.keys())))
    if len(files) > MAX_FILES or sum(len(data) for data, _ in files.values()) > MAX_TOTAL_BYTES:
        raise ValueError("Release exceeds file count or total byte limit")
    source = {"backend": git_info(backend_root, allowed_backend), "frontend": git_info(frontend_root, frontend_source),
              "project_harness": git_info(project_root, lambda name: name in PROJECT_FILES),
              "frontend_build_attestation": "Supplied artifact; source metadata is observed, not proof of build provenance."}
    manifest = {"version": VERSION, "kind": "pankgraph-isolated-release", "source": source,
                "runtime": runtime_info(runtime_inventory), "schema_manifests": schema_info(files),
                "files": {name: {"sha256": digest(data), "size": len(data), "mode": mode} for name, (data, mode) in sorted(files.items())}}
    files[MANIFEST] = (canonical(manifest), 0o644)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ValueError("Release output must not be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".release-", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, (data, mode) in sorted(files.items()):
                    entry = tarfile.TarInfo(name)
                    entry.size, entry.mode, entry.mtime = len(data), mode, 0
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ""
                    archive.addfile(entry, io.BytesIO(data))
        result = verify(temporary)
        os.replace(temporary, output)
        return result
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify(archive_path, expected_sha256=None):
    path = Path(archive_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Release archive must be a regular file")
    with path.open("rb") as source:
        archive_sha = hashlib.file_digest(source, "sha256").hexdigest()
    if expected_sha256 is not None and archive_sha != expected_sha256:
        raise ValueError("Archive SHA-256 mismatch")
    seen, actual, manifest, total = set(), {}, None, 0
    with tarfile.open(path, mode="r:gz") as archive:
        for entry in archive:
            name = entry.name
            if not safe_path(name) or name in seen or entry.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} or (name != MANIFEST and not allowed_payload(name)):
                raise ValueError("Unexpected, duplicate or unsafe archive entry: " + name)
            if entry.uid != 0 or entry.gid != 0 or entry.uname or entry.gname or entry.mtime != 0 or entry.mode not in {0o644, 0o755}:
                raise ValueError("Unexpected archive metadata")
            seen.add(name)
            total += entry.size
            if entry.size < 0 or entry.size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES + MAX_FILE_BYTES or len(seen) > MAX_FILES + 1:
                raise ValueError("Release archive exceeds limits")
            data = archive.extractfile(entry).read(MAX_FILE_BYTES + 1)
            if len(data) != entry.size:
                raise ValueError("Archive entry size mismatch")
            if name == MANIFEST:
                manifest = json.loads(data)
            else:
                actual[name] = {"sha256": digest(data), "size": len(data), "mode": entry.mode}
    if not isinstance(manifest, dict) or manifest.get("version") != VERSION or manifest.get("kind") != "pankgraph-isolated-release":
        raise ValueError("Missing or unsupported release manifest")
    if set(manifest) != {"version", "kind", "source", "runtime", "schema_manifests", "files"}:
        raise ValueError("Unexpected release manifest fields")
    if not isinstance(manifest.get("source"), dict) or not {"backend", "frontend", "frontend_build_attestation"} <= manifest["source"].keys():
        raise ValueError("Release source provenance missing")
    if not isinstance(manifest.get("runtime"), dict) or manifest["runtime"].get("state") not in {"not_supplied", "supplied_observation"}:
        raise ValueError("Release runtime provenance missing")
    if not isinstance(manifest.get("schema_manifests"), dict):
        raise ValueError("Release schema manifest missing")
    for name, info in manifest["schema_manifests"].items():
        if (not safe_path(name) or private_path(name) or not isinstance(info, dict)
                or type(info.get("packaged")) is not bool or not re.fullmatch(r"[0-9a-f]{64}", str(info.get("sha256", "")))):
            raise ValueError("Invalid schema fingerprint")
        if info["packaged"] and (name not in actual or info["sha256"] != actual[name]["sha256"]):
            raise ValueError("Packaged schema digest mismatch")
        if not info["packaged"] and not (name.startswith("frontend-source/src/schema/") and name.endswith(".json")):
            raise ValueError("Unexpected unpackaged schema path")
    if manifest.get("files") != actual:
        raise ValueError("Release manifest file set or digest mismatch")
    required = {"backend/pankagent_vnext/__init__.py", "backend/pankgraph_results/__init__.py",
                "backend/pankgraph_health/__init__.py", "frontend/index.html", "frontend-source/package.json"} | PROJECT_FILES | {"backend/" + name for name in UX_AUDIT_FILES}
    if required - actual.keys():
        raise ValueError("Required release entries missing")
    if not any(name in actual for name in ("frontend-source/package-lock.json", "frontend-source/yarn.lock", "frontend-source/pnpm-lock.yaml")):
        raise ValueError("Frontend dependency lock missing from release")
    if not any(name.startswith("frontend-source/src/") for name in actual):
        raise ValueError("Frontend source missing from release")
    return {"verified": True, "archive_sha256": archive_sha, "manifest_sha256": digest(canonical(manifest)),
            "payload_files": len(actual), "payload_bytes": sum(item["size"] for item in actual.values()), "manifest": manifest}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build")
    create.add_argument("--backend-root", required=True, type=Path)
    create.add_argument("--frontend-build", required=True, type=Path)
    create.add_argument("--frontend-source", type=Path, help="Defaults to the build directory parent")
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--runtime-inventory", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("archive", type=Path)
    check.add_argument("--sha256")
    args = parser.parse_args(argv)
    try:
        result = (build(args.backend_root, args.frontend_build, args.output, args.runtime_inventory, args.frontend_source)
                  if args.command == "build" else verify(args.archive, args.sha256))
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "manifest"}, sort_keys=True))


if __name__ == "__main__":
    main()
