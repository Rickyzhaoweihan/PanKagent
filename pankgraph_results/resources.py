"""Bounded exact-key supplemental resources, local association index and assets.

``indexed_lookup`` is async and searches only successfully validated cached sets.
Its coverage is deliberately not a claim of exhaustive QTL/GWAS retrieval.
Network requests, parsing, plotting and SQLite work stay off health polling paths.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from urllib.parse import quote, urlparse, unquote

import httpx

from .plots import PLOT_VERSION, render_regional_plot
from .resource_registry import REGISTRY_VERSION, SOURCES, TSV_COLUMNS, registry_snapshot, source_for


class ResourceError(ValueError):
    """Sanitized, stable failure category with no raw upstream response."""


_OBSERVATION_ERRORS = frozenset({
    "access_denied", "not_found", "upstream_http_error", "upstream_timeout", "upstream_unreachable",
    "object_too_large", "invalid_encoding", "schema_mismatch", "row_limit_exceeded",
    "invalid_variant_id", "invalid_statistic", "invalid_allele", "duplicate_variant_in_set",
    "empty_resource", "cache_capacity_exceeded", "coordinate_provider_unavailable",
    "coordinate_lookup_failed", "plot_generation_failed", "plot_dependency_unavailable",
    "verified_coordinates_or_statistics_missing", "multiple_chromosomes_in_regional_set",
})


def _unknown_observation(scope: str) -> dict:
    return {"state": "unknown", "scope": scope, "checked_at": None, "last_success": None,
            "latency_ms": None, "error_category": None, "source_key": None}


def _record_observation(observation: dict, result: dict, started: float):
    """Only called after real work; file-cache hits and health reads do not probe."""
    now = result.get("checked_at") or time.time()
    successful = result.get("status") == "available"
    category = result.get("error_category")
    if successful:
        observation["last_success"] = max(now, observation.get("last_success") or 0)
    if (observation.get("checked_at") or 0) > now:
        return
    observation.update(state="healthy" if successful else "unavailable", checked_at=now,
        latency_ms=(time.monotonic() - started) * 1000, source_key=result.get("source_key"),
        error_category=None if successful else (category if category in _OBSERVATION_ERRORS else "resource_operation_failed"))


def parse_association_tsv(raw: bytes, *, max_rows: int = 50000) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ResourceError("invalid_encoding") from exc
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if tuple(reader.fieldnames or ()) != TSV_COLUMNS:
        raise ResourceError("schema_mismatch")
    rows, seen = [], set()
    for row in reader:
        if len(rows) >= max_rows:
            raise ResourceError("row_limit_exceeded")
        if None in row or any(value is None for value in row.values()):
            raise ResourceError("schema_mismatch")
        if not re.fullmatch(r"[A-Za-z0-9_:.+-]{1,200}", row["snp"]):
            raise ResourceError("invalid_variant_id")
        for field in ("pip", "nominal_p", "slope", "lbf"):
            try:
                number = float(row[field])
            except (ValueError, TypeError) as exc:
                raise ResourceError("invalid_statistic") from exc
            if not math.isfinite(number):
                raise ResourceError("invalid_statistic")
            if field in ("pip", "nominal_p") and not 0 <= number <= 1:
                raise ResourceError("invalid_statistic")
            row[field] = number
        for field in ("effect_allele", "other_allele"):
            if not re.fullmatch(r"[A-Za-z*.-]{1,500}", row[field]):
                raise ResourceError("invalid_allele")
        if row["snp"] in seen:
            raise ResourceError("duplicate_variant_in_set")
        seen.add(row["snp"])
        rows.append(row)
    if not rows:
        raise ResourceError("empty_resource")
    return rows


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_url(value) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() for char in value):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username else None


def _graph_items(evidence: dict) -> tuple[list[dict], list[dict]]:
    source = evidence.get("query_result", evidence)
    if "combined_query_result" in source:
        source = source["combined_query_result"]
    nodes, edges = list(source.get("nodes") or []), list(source.get("edges") or [])
    if not nodes and not edges:
        for step in source.get("steps", []):
            if step.get("status") in {"complete", "partial", "empty"}:
                nodes.extend(step.get("nodes") or [])
                edges.extend(step.get("edges") or [])
    return nodes, edges


def _properties(item: dict) -> dict:
    value = item.get("properties", item.get("~properties", {}))
    return value if isinstance(value, dict) else {}


def _type(item: dict) -> str:
    return str(item.get("type", item.get("~type", ""))).upper()


def _reference_tabs(nodes: list[dict], edges: list[dict]) -> dict:
    tabs = {"references": {}, "pankbase_links": [], "external_links": []}
    nodes_with_ensembl_source = set()
    for item in nodes + edges:
        props = _properties(item)
        for field in ("pmid", "PMID", "pubmed_id", "pmids"):
            for pmid in re.findall(r"\b\d{5,9}\b", str(props.get(field, ""))):
                key = "pmid:" + pmid
                tabs["references"][key] = {"id": key, "title": "PubMed " + pmid,
                    "subtitle": "Reference supplied by graph evidence", "pmid": pmid,
                    "href": "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/"}
        for field in ("doi", "DOI"):
            doi = str(props.get(field, "")).removeprefix("https://doi.org/")
            if re.fullmatch(r"10\.\d{4,9}/\S{1,300}", doi):
                key = "doi:" + doi
                tabs["references"][key] = {"id": key, "title": doi,
                    "subtitle": "Reference supplied by graph evidence", "href": "https://doi.org/" + quote(doi, safe="/()")}
        for field in ("data_source_url", "publication_url", "reference_url", "reference"):
            url = _safe_url(props.get(field))
            if url:
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                pmid = re.fullmatch(r"/(\d{5,9})/?", parsed.path) if host == "pubmed.ncbi.nlm.nih.gov" else None
                if host in {"www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov"}:
                    pmid = re.fullmatch(r"/pubmed/(\d{5,9})/?", parsed.path)
                doi = unquote(parsed.path.lstrip("/")) if host in {"doi.org", "dx.doi.org"} else ""
                if pmid:
                    key = "pmid:" + pmid[1]
                    tabs["references"][key] = {"id": key, "title": "PubMed " + pmid[1], "pmid": pmid[1],
                        "subtitle": "Reference supplied by graph evidence", "href": url}
                    continue
                if re.fullmatch(r"10\.\d{4,9}/\S{1,300}", doi):
                    key = "doi:" + doi
                    tabs["references"][key] = {"id": key, "title": doi,
                        "subtitle": "Reference supplied by graph evidence", "href": url}
                    continue
                if field == "data_source_url":
                    label = str(props.get("data_source") or host or "External database")
                    tabs["external_links"].append([label, "Database source supplied by graph evidence", url])
                    if host == "ensembl.org" or host.endswith(".ensembl.org"):
                        node_id = str(item.get("id", item.get("~id", "")))
                        if node_id:
                            nodes_with_ensembl_source.add(node_id)
                    continue
                key = "url:" + _hash(url.encode())[:16]
                tabs["references"][key] = {"id": key, "title": str(props.get("data_source") or "Graph source"),
                    "subtitle": "Source link supplied by graph evidence", "href": url}
    for node in nodes:
        node_id = str(node.get("id", node.get("~id", "")))
        name = str(_properties(node).get("name") or node_id)
        if re.fullmatch(r"ENSG\d{11}(?:\.\d+)?", node_id) and node_id not in nodes_with_ensembl_source:
            tabs["external_links"].append(["Ensembl", f"View {name} in Ensembl",
                "https://www.ensembl.org/Homo_sapiens/Gene/Summary?g=" + quote(node_id)])
        elif re.fullmatch(r"rs\d+", node_id):
            tabs["external_links"].append(["dbSNP", f"View {node_id} in dbSNP", "https://www.ncbi.nlm.nih.gov/snp/" + node_id])
    edge_types = {_type(edge) for edge in edges}
    if edge_types & {"PART_OF_QTL_SIGNAL", "PART_OF_QTL", "PART_OF_GWAS_SIGNAL", "PART_OF_GWAS", "COLOCALIZATION", "COLOCALIZED_WITH", "SIGNAL_COLOC_WITH"}:
        tabs["pankbase_links"] += [["QTL/GWAS data sources", "https://pankgraph.org/qtldatasource"],
            ["Fine-mapping pipeline", "https://pankgraph.org/pipeline"],
            ["Fine-mapping code", "https://github.com/PanKbase/PanKgraph-finemap-coloc"]]
    # Attach modality-specific resources only when evidence actually has that modality.
    if any("RNA" in edge_type or edge_type in {"EXPRESSED_IN", "EXPRESS_IN", "GENE_EXPRESSION", "GENE_DETECTED_IN", "GENE_ENRICHED_IN", "T1D_DEG_IN"} for edge_type in edge_types):
        tabs["pankbase_links"].append(["PanKbase scRNA-seq code", "https://github.com/PanKbase/PanKbase-scRNA-seq"])
    if any("ATAC" in edge_type or "OCR" in edge_type or edge_type == "GENE_ACTIVITY_SCORE_IN" for edge_type in edge_types):
        tabs["pankbase_links"].append(["PanKbase scATAC-seq code", "https://github.com/PanKbase/HPAP-scATAC-seq"])
    tabs["external_links"] = list({link[2]: link for link in tabs["external_links"]}.values())
    return tabs


class ResourceManager:
    def __init__(self, state_dir: Path, coordinate_lookup=None, public_base="/api/resources", settings=None):
        self.state_dir = Path(state_dir)
        self.asset_dir = self.state_dir / "assets"
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.public_base = public_base.rstrip("/")
        self.coordinate_lookup = coordinate_lookup
        get = lambda name, default: getattr(settings, name, default) if settings else default
        self.max_bytes = int(get("resource_max_bytes", 50 * 1024 * 1024))
        self.cache_max_bytes = int(get("resource_cache_max_bytes", 2 * 1024 ** 3))
        self.max_rows = int(get("resource_max_rows", 50000))
        self.max_objects = int(get("resource_max_objects", 4))
        self.ttl = float(get("resource_ttl_seconds", 86400))
        self.timeout = float(get("resource_timeout", 10))
        self.registry = tuple(get("resource_registry", SOURCES))
        self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False)
        self._semaphore = asyncio.Semaphore(2)
        self._plot_semaphore = asyncio.Semaphore(1)
        self._locks: dict[str, asyncio.Lock] = {}
        self._db_lock = threading.RLock()
        self._db = sqlite3.connect(self.state_dir / "associations.sqlite3", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS objects(object_key TEXT PRIMARY KEY, status TEXT NOT NULL, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS assets(asset_id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS associations(object_key TEXT NOT NULL, ordinal INTEGER NOT NULL,
                snp TEXT NOT NULL, gene TEXT, credible_set TEXT NOT NULL, data_source TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(object_key,ordinal));
            CREATE INDEX IF NOT EXISTS assoc_snp ON associations(snp);
            CREATE INDEX IF NOT EXISTS assoc_gene ON associations(gene);
            CREATE INDEX IF NOT EXISTS assoc_set ON associations(credible_set,data_source);
        """)
        self._snapshot = {"state": "unknown", "check_time": None, "last_success": None,
            "error_category": None, "registry": registry_snapshot(self.registry),
            "coverage": self._coverage(), "active_fetches": 0,
            "source_observations": {source.id: {**_unknown_observation("latest_exact_object_attempt"),
                "prefix": source.prefix, "verification": "verified_prefix" if source.verified else "unverified_prefix"}
                for source in self.registry},
            "plot_generation": _unknown_observation("latest_plot_attempt")}
        self._restore_source_observations()

    def _restore_source_observations(self):
        """Restore at most one observation per registered source, only at startup."""
        for source in self.registry:
            with self._db_lock:
                row = self._db.execute("""
                    WITH source_objects AS (
                        SELECT status, metadata,
                               CAST(json_extract(metadata, '$.checked_at') AS REAL) AS checked_at
                        FROM objects WHERE json_extract(metadata, '$.source_id') = ?
                            AND status IN ('available', 'unavailable')
                    )
                    SELECT status, metadata, checked_at,
                        (SELECT MAX(CASE WHEN status = 'available' THEN checked_at
                            ELSE CAST(json_extract(metadata, '$.last_success') AS REAL) END)
                         FROM source_objects) AS last_success
                    FROM source_objects WHERE checked_at > 0
                    ORDER BY checked_at DESC LIMIT 1
                """, (source.id,)).fetchone()
            if row is None:
                continue
            metadata = json.loads(row["metadata"])
            category = metadata.get("error_category")
            successful = row["status"] == "available"
            self._snapshot["source_observations"][source.id].update(
                state="healthy" if successful else "unavailable", checked_at=row["checked_at"],
                last_success=row["last_success"], source_key=metadata.get("source_key"),
                error_category=None if successful else (category if category in _OBSERVATION_ERRORS else "resource_operation_failed"))

    def snapshot(self) -> dict:
        """Cached observation only: this method performs no network, DB or inference."""
        snapshot = json.loads(json.dumps(self._snapshot))
        now = time.time()
        for observation in [*snapshot["source_observations"].values(), snapshot["plot_generation"]]:
            checked = observation["checked_at"]
            observation["age_seconds"] = max(0, now - checked) if checked is not None else None
            observation["stale"] = checked is not None and observation["age_seconds"] >= self.ttl
            if observation["stale"]:
                observation["last_observed_state"] = observation["state"]
                observation["state"] = "unknown"
        return snapshot

    def _coverage(self) -> dict:
        with self._db_lock:
            rows = self._db.execute("SELECT metadata FROM objects WHERE status='available'").fetchall()
        objects = [json.loads(row[0]) for row in rows]
        return {"scope": "validated_cached_credible_sets_only", "exhaustive": False,
            "indexed_sets": len(objects), "indexed_rows": sum(obj.get("row_count", 0) for obj in objects),
            "sets": [{"data_source": obj["data_source"], "credible_set": obj["credible_set"],
                      "row_count": obj["row_count"], "sha256": obj["sha256"], "checked_at": obj["checked_at"],
                      "stale": time.time() - obj["checked_at"] >= self.ttl}
                     for obj in objects]}

    def _object(self, key: str) -> dict | None:
        with self._db_lock:
            row = self._db.execute("SELECT status,metadata FROM objects WHERE object_key=?", (key,)).fetchone()
        if not row:
            return None
        metadata = dict(json.loads(row["metadata"]), status=row["status"])
        # Seed CLIs and running services can use different public mount points.
        # Only the outgoing URL changes; cached bytes, hashes and IDs are stable.
        if metadata.get("asset_id"):
            metadata["url"] = self.public_base + "/" + metadata["asset_id"]
        return metadata

    def _mark_object(self, key: str, status: str, metadata: dict):
        with self._db_lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?)", (key, status, json.dumps(metadata)))
            if status != "available":
                self._db.execute("DELETE FROM associations WHERE object_key=?", (key,))

    def _read_asset(self, asset_id: str) -> tuple[Path, dict]:
        if not re.fullmatch(r"[a-f0-9]{32}", asset_id):
            raise KeyError(asset_id)
        with self._db_lock:
            row = self._db.execute("SELECT metadata FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if not row:
            raise KeyError(asset_id)
        metadata = json.loads(row[0])
        path = self.asset_dir / metadata["filename"]
        if (not path.is_file() or path.is_symlink() or path.stat().st_size != metadata["size"]
                or _hash(path.read_bytes()) != metadata["sha256"]):
            with self._db_lock, self._db:
                for object_row in self._db.execute("SELECT object_key,metadata FROM objects").fetchall():
                    if json.loads(object_row["metadata"]).get("asset_id") == asset_id:
                        self._db.execute("UPDATE objects SET status='corrupt' WHERE object_key=?", (object_row["object_key"],))
                        self._db.execute("DELETE FROM associations WHERE object_key=?", (object_row["object_key"],))
            raise KeyError(asset_id)
        return path, metadata

    async def asset(self, asset_id: str) -> tuple[Path, str, str]:
        path, meta = await asyncio.to_thread(self._read_asset, asset_id)
        return path, meta["media_type"], meta["download_name"]

    async def download(self, source: str, credible_set: str) -> tuple[Path, str, str]:
        """User-requested exact registry key download; never accepts a URL or SQL."""
        registered = source_for(source, self.registry)
        if registered is None:
            raise KeyError("unmapped_source")
        try:
            registered.object_key(credible_set)
        except ValueError as exc:
            raise KeyError("invalid_credible_set") from exc
        gene = credible_set.split("__")[0] if re.match(r"^ENSG\d{11}__", credible_set) else None
        result = await self._download(registered, credible_set, gene)
        if result["status"] != "available":
            raise ResourceError(result.get("error_category", "resource_unavailable"))
        self._snapshot.update({"state": "healthy", "check_time": time.time(), "last_success": time.time(),
            "error_category": None, "coverage": await asyncio.to_thread(self._coverage)})
        return await self.asset(result["asset_id"])

    def _save_asset(self, raw: bytes, *, kind: str, identity: str, media_type: str, download_name: str, extra=None) -> dict:
        digest = _hash(raw)
        asset_id = _hash((identity + "\0" + digest).encode())[:32]
        suffix = ".png" if media_type == "image/png" else ".tsv"
        metadata = {"asset_id": asset_id, "filename": asset_id + suffix, "kind": kind,
            "sha256": digest, "size": len(raw), "media_type": media_type,
            "download_name": download_name, "url": self.public_base + "/" + asset_id,
            "created_at": time.time(), **(extra or {})}
        if len(raw) > self.cache_max_bytes:
            raise ResourceError("cache_capacity_exceeded")
        with self._db_lock:
            entries = [json.loads(row[0]) for row in self._db.execute("SELECT metadata FROM assets").fetchall()]
            total = sum(entry["size"] for entry in entries if entry["asset_id"] != asset_id)
            protected_asset = (extra or {}).get("source_asset_id")
            evictable = [entry for entry in entries if entry["asset_id"] not in {asset_id, protected_asset}]
            if total + len(raw) - sum(entry["size"] for entry in evictable) > self.cache_max_bytes:
                raise ResourceError("cache_capacity_exceeded")
            for old in sorted(evictable, key=lambda entry: entry["created_at"]):
                if total + len(raw) <= self.cache_max_bytes:
                    break
                if old["asset_id"] == asset_id:
                    continue
                (self.asset_dir / old["filename"]).unlink(missing_ok=True)
                self._db.execute("DELETE FROM assets WHERE asset_id=?", (old["asset_id"],))
                # Evicted data must not keep pretending to be indexed complete evidence.
                for objrow in self._db.execute("SELECT object_key,metadata FROM objects").fetchall():
                    if json.loads(objrow["metadata"]).get("asset_id") == old["asset_id"]:
                        self._db.execute("UPDATE objects SET status='evicted' WHERE object_key=?", (objrow["object_key"],))
                        self._db.execute("DELETE FROM associations WHERE object_key=?", (objrow["object_key"],))
                total -= old["size"]
            fd, temporary = tempfile.mkstemp(dir=self.asset_dir, prefix=".partial-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.asset_dir / metadata["filename"])
                self._db.execute("INSERT OR REPLACE INTO assets VALUES(?,?)", (asset_id, json.dumps(metadata)))
                self._db.commit()
            finally:
                Path(temporary).unlink(missing_ok=True)
        return metadata

    def _store_download(self, key: str, raw: bytes, rows: list[dict], metadata: dict) -> dict:
        asset = self._save_asset(raw, kind="association_tsv", identity=key,
            media_type="text/tab-separated-values", download_name=metadata["credible_set"] + ".txt",
            extra={"source_key": key, "data_source": metadata["data_source"], "credible_set": metadata["credible_set"]})
        metadata = {**metadata, **asset, "row_count": len(rows), "schema": list(TSV_COLUMNS), "schema_version": 1,
                    "object_verification": "validated_tsv"}
        lead = max(rows, key=lambda row: (row["pip"], row["snp"]))
        enriched = [{**row, "gene": metadata.get("gene"), "credible_set": metadata["credible_set"],
            "data_source": metadata["data_source"], "n_snp": len(rows), "lead_snp": lead["snp"],
            "lead_pip": lead["pip"], "lead_definition": "maximum_pip_in_downloaded_set", "resource_sha256": asset["sha256"]}
            for row in rows]
        with self._db_lock, self._db:
            self._db.execute("DELETE FROM associations WHERE object_key=?", (key,))
            self._db.executemany("INSERT INTO associations VALUES(?,?,?,?,?,?,?)", [
                (key, index, row["snp"], row["gene"], row["credible_set"], row["data_source"], json.dumps(row))
                for index, row in enumerate(enriched)])
            self._db.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?)", (key, "available", json.dumps(metadata)))
        return dict(metadata, status="available")

    async def _download(self, source, credible_set: str, gene: str | None) -> dict:
        key = source.object_key(credible_set)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await asyncio.to_thread(self._object, key)
            headers = {}
            if cached and cached["status"] == "available":
                try:
                    await self.asset(cached["asset_id"])
                    if cached.get("gene") and gene and cached["gene"] != gene:
                        return {"status": "unavailable", "source_key": key,
                                "error_category": "gene_context_mismatch"}
                    if time.time() - cached["checked_at"] < self.ttl:
                        return dict(cached, cache="hit")
                    if cached.get("etag"):
                        headers["If-None-Match"] = cached["etag"]
                    elif cached.get("last_modified"):
                        headers["If-Modified-Since"] = cached["last_modified"]
                except KeyError:
                    cached = None
            async with self._semaphore:
                self._snapshot["active_fetches"] += 1
                started = time.monotonic()
                observation = self._snapshot["source_observations"][source.id]
                try:
                    async with self._client.stream("GET", source.url(credible_set), headers=headers) as response:
                        if response.status_code == 304 and cached:
                            cached["checked_at"] = time.time()
                            await asyncio.to_thread(self._mark_object, key, "available", cached)
                            result = dict(cached, cache="revalidated")
                            _record_observation(observation, result, started)
                            return result
                        if response.status_code != 200:
                            category = {403: "access_denied", 404: "not_found"}.get(response.status_code, "upstream_http_error")
                            raise ResourceError(category)
                        try:
                            length = int(response.headers.get("Content-Length", 0))
                        except ValueError:
                            length = 0
                        if length > self.max_bytes:
                            raise ResourceError("object_too_large")
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > self.max_bytes:
                                raise ResourceError("object_too_large")
                        rows = await asyncio.to_thread(parse_association_tsv, bytes(raw), max_rows=self.max_rows)
                        metadata = {"source_id": source.id, "data_source": source.aliases[0], "source_key": key,
                            "source_url": source.url(credible_set), "credible_set": credible_set, "gene": gene,
                            "source_kind": source.kind, "registry_version": REGISTRY_VERSION,
                            "prefix_verification": "verified" if source.verified else "unverified",
                            "etag": response.headers.get("ETag"), "last_modified": response.headers.get("Last-Modified"),
                            "checked_at": time.time()}
                        result = await asyncio.to_thread(self._store_download, key, bytes(raw), rows, metadata)
                        _record_observation(observation, result, started)
                        return result
                except (ResourceError, httpx.HTTPError) as exc:
                    category = str(exc) if isinstance(exc, ResourceError) else (
                        "upstream_timeout" if isinstance(exc, httpx.TimeoutException) else "upstream_unreachable")
                    failure = {"source_id": source.id, "source_key": key, "data_source": source.aliases[0],
                        "credible_set": credible_set, "checked_at": time.time(), "error_category": category,
                        "last_success": observation.get("last_success"),
                        "prefix_verification": "verified" if source.verified else "unverified"}
                    await asyncio.to_thread(self._mark_object, key, "unavailable", failure)
                    result = dict(failure, status="unavailable")
                    _record_observation(observation, result, started)
                    return result
                finally:
                    self._snapshot["active_fetches"] -= 1

    def _lookup(self, filters: dict, limit: int) -> dict:
        terms, params = ["o.status='available'"], []
        for field, value in filters.items():
            if value is not None:
                terms.append(f"a.{field}=?")
                params.append(str(value))
        with self._db_lock:
            rows = self._db.execute("SELECT a.payload FROM associations a JOIN objects o USING(object_key) WHERE "
                + " AND ".join(terms) + " ORDER BY a.data_source,a.credible_set,a.ordinal LIMIT ?", (*params, limit + 1)).fetchall()
        return {"rows": [json.loads(row[0]) for row in rows[:limit]], "truncated": len(rows) > limit,
                "coverage": self._coverage(), "status": "available" if rows else "not_in_local_index"}

    async def indexed_lookup(self, *, snp=None, gene=None, credible_set=None, data_source=None, limit=100) -> dict:
        if not any(value is not None for value in (snp, gene, credible_set, data_source)):
            raise ValueError("At least one indexed identity filter is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.max_rows:
            raise ValueError("invalid_limit")
        source = source_for(data_source, self.registry) if data_source else None
        return await asyncio.to_thread(self._lookup,
            {"snp": snp, "gene": gene, "credible_set": credible_set,
             "data_source": source.aliases[0] if source else data_source}, limit)

    async def _plot(self, metadata: dict) -> dict:
        started = time.monotonic()
        try:
            result = await self._generate_plot(metadata)
        except Exception:
            result = {"status": "unavailable", "error_category": "plot_generation_failed"}
        _record_observation(self._snapshot["plot_generation"],
            {**result, "source_key": metadata["source_key"]}, started)
        return result

    async def _generate_plot(self, metadata: dict) -> dict:
        if self.coordinate_lookup is None:
            return {"status": "unavailable", "error_category": "coordinate_provider_unavailable"}
        async with self._plot_semaphore:
            rows = (await self.indexed_lookup(credible_set=metadata["credible_set"],
                data_source=metadata["data_source"], limit=self.max_rows))["rows"]
            try:
                coordinates = await asyncio.wait_for(self.coordinate_lookup([row["snp"] for row in rows]), timeout=self.timeout)
                if not isinstance(coordinates, dict):
                    raise ValueError("invalid coordinate result")
            except Exception:
                return {"status": "unavailable", "error_category": "coordinate_lookup_failed"}
            identity = PLOT_VERSION + metadata["sha256"] + _hash(json.dumps(coordinates, sort_keys=True).encode())
            fd, temporary = tempfile.mkstemp(dir=self.asset_dir, prefix=".plot-", suffix=".png")
            os.close(fd)
            try:
                result = await asyncio.to_thread(render_regional_plot, rows, coordinates, Path(temporary),
                    title=f"{metadata['data_source']} · {metadata['credible_set']}")
                if result["status"] == "available":
                    raw_plot = await asyncio.to_thread(Path(temporary).read_bytes)
                    result["asset"] = await asyncio.to_thread(self._save_asset, raw_plot,
                        kind="regional_plot", identity=identity, media_type="image/png",
                        download_name=metadata["credible_set"] + ".png", extra={"coverage": result["coverage"],
                            "plot_version": PLOT_VERSION, "source_asset_id": metadata["asset_id"]})
                return result
            except Exception:
                return {"status": "unavailable", "error_category": "plot_generation_failed"}
            finally:
                Path(temporary).unlink(missing_ok=True)

    async def resolve(self, evidence: dict) -> dict:
        started = time.monotonic()
        nodes, edges = _graph_items(evidence)
        tabs = _reference_tabs(nodes, edges)
        by_id = {str(node.get("id", node.get("~id"))): node for node in nodes}
        requests, groups, seen = [], [], set()
        for edge in edges:
            edge_type, props = _type(edge), _properties(edge)
            if edge_type not in {"PART_OF_QTL", "PART_OF_QTL_SIGNAL", "PART_OF_GWAS", "PART_OF_GWAS_SIGNAL"}:
                continue
            source = source_for(props.get("data_source", ""), self.registry)
            credible = props.get("credible_set") or props.get("credibleset") or props.get("credible_set_id")
            if not source or not credible:
                groups.append({"status": "unavailable", "error_category": "source_or_credible_set_unmapped",
                    "data_source": props.get("data_source"), "credible_set": credible})
                continue
            if ("GWAS" in edge_type) != (source.kind == "GWAS"):
                groups.append({"status": "unavailable", "error_category": "source_relation_mismatch"})
                continue
            try:
                key = source.object_key(credible)
            except ValueError:
                groups.append({"status": "unavailable", "error_category": "invalid_credible_set"})
                continue
            if key in seen:
                continue
            seen.add(key)
            endpoints = [str(edge.get("start_id", edge.get("source", edge.get("~start", "")))),
                         str(edge.get("end_id", edge.get("target", edge.get("~end", ""))))]
            genes = [item for item in endpoints if re.fullmatch(r"ENSG\d{11}(?:\.\d+)?", item)]
            gene = genes[0] if genes else (credible.split("__")[0] if re.match(r"^ENSG\d{11}__", credible) else None)
            if credible.startswith("ENSG") and gene and credible.split("__")[0].split(".")[0] != gene.split(".")[0]:
                groups.append({"status": "unavailable", "error_category": "gene_context_mismatch"})
                continue
            context = {field: props[field] for field in ("tissue", "tissue_name", "tissue_id", "cell_type", "condition",
                "disease", "data_source", "data_version", "gene_name", "method", "lead_status") if field in props}
            for endpoint in endpoints:
                node = by_id.get(endpoint, {})
                labels = [str(label).lower() for label in node.get("labels", node.get("~labels", []))]
                if set(labels) & {"cell_type", "disease", "tissue"}:
                    context["endpoint_context"] = {"id": endpoint, "labels": labels, "name": _properties(node).get("name")}
            if len(requests) >= self.max_objects:
                groups.append({"status": "unavailable", "error_category": "resolve_object_limit", "source_key": key})
                continue
            requests.append((source, credible, gene, context))
        results = await asyncio.gather(*(self._download(source, credible, gene) for source, credible, gene, _ in requests))
        assets = []
        for result, request in zip(results, requests):
            result["context"] = request[3]
            groups.append(result)
            if result["status"] != "available":
                continue
            assets.append({key: value for key, value in result.items() if key != "filename"})
            plot = await self._plot(result)
            result["plot"] = plot
            if plot["status"] == "available":
                assets.append(plot["asset"])
            result["resources_tabs"] = {"empirical_evidence": {
                "title": "Regional association and fine-mapping evidence",
                "description": "Observed nominal P values and posterior inclusion probabilities (PIP) for this source and credible set. "
                    "The plot uses verified GRCh38 coordinates; no LD measurements or causal conclusion are inferred.",
                "legend": "View" if plot["status"] == "available" else "Download",
                "status": "available" if plot["status"] == "available" else "download_only",
                "link_text": "Download the source association data ↗", "link": result["url"], "download_url": result["url"],
                "folder": request[0].prefix, "credible_set": result["credible_set"], "data_source": result["data_source"],
                "plot_status": plot["status"], "plot_error_category": plot.get("error_category"),
                "coordinate_coverage": plot.get("coverage"),
                **({"image_url": plot["asset"]["url"]} if plot["status"] == "available" else {})}}
            if "empirical_evidence" not in tabs:
                tabs.update(result["resources_tabs"])
        available = [group for group in groups if group["status"] == "available"]
        status = "not_applicable" if not groups else ("unavailable" if not available else (
            "partial" if len(available) < len(groups) or any(group.get("plot", {}).get("status") != "available" for group in available) else "available"))
        coverage = await asyncio.to_thread(self._coverage)
        now = time.time()
        if groups:
            self._snapshot.update({"state": "healthy" if status == "available" else (
                "degraded" if available else "unavailable"), "check_time": now, "latency_ms": (time.monotonic() - started) * 1000,
                "coverage": coverage, "error_category": next((group.get("error_category") or group.get("plot", {}).get("error_category")
                    for group in groups if group.get("error_category") or group.get("plot", {}).get("error_category")), None)})
        if available:
            self._snapshot["last_success"] = now
        return {"resources_tabs": tabs, "assets": assets, "status": status, "resource_groups": groups,
            "coverage": coverage, "provenance": {"registry_version": REGISTRY_VERSION, "discovery": "exact_key_only",
                "requested_sets": len(requests), "available_sets": len(available), "graph_version": evidence.get("graph_version"),
                "datasets_complete": False}}

    async def close(self):
        await self._client.aclose()
        await asyncio.to_thread(self._db.close)


async def _seed_manifest(state_dir: Path, manifest: Path, maximum: int):
    """Explicit operator seed list only; no bucket listing or prefix crawling."""
    entries = json.loads(manifest.read_text())
    if not isinstance(entries, list) or not 1 <= len(entries) <= maximum <= 256:
        raise ValueError("Seed manifest must list 1..max_objects exact keys (maximum 256)")
    manager = ResourceManager(state_dir)
    try:
        for entry in entries:
            try:
                path, _, _ = await manager.download(entry["source"], entry["credible_set"])
                print(json.dumps({"source": entry["source"], "credible_set": entry["credible_set"],
                    "status": "available", "size": path.stat().st_size}))
            except (KeyError, ResourceError) as exc:
                print(json.dumps({"source": entry.get("source"), "credible_set": entry.get("credible_set"),
                    "status": "unavailable", "error_category": str(exc)}))
    finally:
        await manager.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Seed a bounded list of exact public supplemental resource keys")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--seed-manifest", type=Path, required=True)
    parser.add_argument("--max-objects", type=int, default=16)
    args = parser.parse_args()
    asyncio.run(_seed_manifest(args.state_dir, args.seed_manifest, args.max_objects))
