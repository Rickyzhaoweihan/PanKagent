"""Read-only exploration of recorded colocalization and its exact credible sets.

These server-owned queries intentionally return scalar maps, then paginate them
through QueryService's release/EXPLAIN/read-only boundary. Graph display limits
must never masquerade as complete signal membership. No statistic is recomputed.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlsplit

from .projection import project_evidence
from .query import COLOC_QTL_CONTEXT, _gwas_credible_set
from .resource_registry import SOURCES, source_for
from .resources import ResourceError, parse_association_tsv

VERSION = 1
T1D = "MONDO_0005147"
MAX_RECORDS = 1000
MAX_MEMBERS = 5000
MAX_EXTRACT_BYTES = 10 * 1024 * 1024
PAGE_SIZE = 200
CATALOG_TTL = 30
CATALOG_MATCH = "MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease {id:$disease_id}) "
CATALOG_QUERY = (CATALOG_MATCH + "RETURN g.id AS gene_id,g.name AS gene_name,"
    "d.id AS disease_id,d.name AS disease_name,properties(r) AS properties "
    "ORDER BY g.id,r.qtl_signal_id,r.gwas_signal_id,r.coloc_dataset,r.data_source,r.data_version "
    "SKIP $offset LIMIT $page_size")
CATALOG_COUNT = CATALOG_MATCH + "RETURN count(r) AS total"
GWAS_MATCH = ("MATCH (v:sequence_variant)-[r:PART_OF_GWAS_SIGNAL]->(d:disease {id:$disease_id}) "
    "WHERE r.credible_set_id=$signal_id AND r.data_source=$data_source ")
QTL_MATCH = ("MATCH (v:sequence_variant)-[r:PART_OF_QTL_SIGNAL]->(g:Gene {id:$gene_id}) "
    "WHERE r.credible_set=$signal_id AND r.data_source=$data_source AND r.tissue_id=$tissue_id ")
MEMBER_RETURN = ("RETURN v.id AS variant_id,v.chr AS chromosome,v.start_loc AS start,"
    "v.end_loc AS end,v.genome_assembly AS assembly,v.ref AS ref,v.alt AS alt,"
    "properties(v)['coordinate_system'] AS coordinate_system,"
    "properties(v)['coordinate_system_verified'] AS coordinate_system_verified,"
    "properties(r) AS properties ORDER BY v.id SKIP $offset LIMIT $page_size")
QTL_LABELS = {
    "t1d_eQTL-inspire_coloc": ("Pancreatic islet", "eQTL"),
    "t1d_exonQTL-inspire_coloc": ("Pancreatic islet", "exonQTL"),
    "t1d_eQTL-gtex_coloc": ("Pancreas", "eQTL"),
    "t1d_sQTL-gtex_coloc": ("Pancreas", "sQTL"),
}
# This is a source-specific mapping in the verified imported release, not an
# inference from gene proximity or from the source's display name.
GWAS_SOURCE = {("HIRN_T1D_QTL_GWAS", "v1.0"): "GWAS_finemapping_V1"}


class ColocUnavailable(RuntimeError):
    pass


def coloc_router(explorer):
    """Mount on the authenticated results app or a separately guarded preview."""
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import JSONResponse, Response
    router = APIRouter()

    @router.get("/api/coloc/records")
    async def catalog():
        try:
            async with asyncio.timeout(25):
                value = await explorer.catalog()
            return JSONResponse(value, headers={"Cache-Control": "no-store"})
        except Exception:
            raise HTTPException(503, "The recorded colocalization catalog is temporarily unavailable.") from None

    @router.get("/api/coloc/records/{record_id}")
    async def detail(record_id: str):
        try:
            value = await explorer.detail(record_id)
            return JSONResponse(value, headers={"Cache-Control": "no-store"})
        except KeyError:
            raise HTTPException(404, "Recorded colocalization not found in the configured graph release.") from None
        except Exception:
            raise HTTPException(503, "This recorded colocalization cannot currently be loaded.") from None

    @router.get("/api/coloc/records/{record_id}/download/{role}")
    async def download(record_id: str, role: str):
        try:
            raw, name = await explorer.extract_download(record_id, role)
            return Response(raw, media_type="text/tab-separated-values",
                headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
        except KeyError:
            raise HTTPException(404, "No exact extract is registered for this record and dataset.") from None
        except Exception:
            raise HTTPException(424, "The registered extract is unavailable or failed validation.") from None

    return router


def _now():
    return datetime.now(timezone.utc).isoformat()


def _number(value, *, probability=False):
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or (probability and not 0 <= value <= 1):
        return None
    return value


def _integer(value):
    number = _number(value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


def _text(value):
    return value if isinstance(value, str) and 0 < len(value) <= 1024 else None


def _leads(value):
    values = value if isinstance(value, list) else re.split(r"[,;\s]+", value) if isinstance(value, str) else []
    return sorted({item for item in values if isinstance(item, str) and re.fullmatch(r"rs\d+", item)})


def record_from_row(row, graph_version):
    props = row.get("properties") or {}
    if not isinstance(props, dict) or row.get("disease_id") != T1D:
        raise ValueError("invalid_coloc_record")
    gene, disease = _text(row.get("gene_id")), _text(row.get("disease_id"))
    if not gene or not disease:
        raise ValueError("missing_coloc_identity")
    identity = [graph_version, gene, disease, props.get("qtl_signal_id"),
        props.get("gwas_signal_id"), props.get("coloc_dataset"),
        props.get("data_source"), props.get("data_version")]
    if any(not isinstance(value, str) or not value or len(value) > 1024 for value in identity):
        raise ValueError("missing_coloc_identity")
    rid = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    posteriors = {"h" + str(i): _number(props.get("pp_h" + str(i) + "_abf"), probability=True) for i in range(5)}
    notices = []
    if any(value is None for value in posteriors.values()):
        notices.append("Some recorded posterior probabilities are missing or invalid; they are not zero.")
    elif abs(sum(posteriors.values()) - 1) > 1e-4:
        notices.append("The recorded posterior probabilities do not sum to one; values have not been renormalized.")
    tissue, qtl_type = QTL_LABELS.get(props["coloc_dataset"], (None, None))
    return {"id": rid, "gene_id": gene, "gene_name": _text(row.get("gene_name")) or gene,
        "disease_id": disease, "disease_name": _text(row.get("disease_name")) or disease,
        "qtl_signal_id": props["qtl_signal_id"], "gwas_signal_id": props["gwas_signal_id"],
        "gwas_credible_set_id": _gwas_credible_set(props["gwas_signal_id"]),
        "dataset": props["coloc_dataset"], "tissue": tissue, "qtl_type": qtl_type,
        "data_source": props["data_source"], "data_version": props["data_version"],
        "nsnp": _integer(props.get("nsnp")), "posteriors": posteriors,
        "gwas_leads": _leads(props.get("gwas_lead_vars")), "qtl_leads": _leads(props.get("qtl_lead_vars")),
        "notices": notices}


def _association(props):
    return {"member": True, "pip": _number(props.get("pip"), probability=True),
        "nominal_p": _number(props.get("nominal_p", props.get("p_value")), probability=True),
        "effect_allele": _text(props.get("effect_allele")),
        "other_allele": _text(props.get("other_allele", props.get("non_effect_allele"))),
        "slope": _number(props.get("slope"))}


def _conflicting_associations(left, right):
    """Missing annotations are not contradictions; keep real source conflicts."""
    for key in ("pip", "nominal_p", "effect_allele", "other_allele", "slope"):
        a, b = left.get(key), right.get(key)
        if a is None or b is None:
            continue
        if key in {"pip", "nominal_p", "slope"}:
            if not math.isclose(a, b, rel_tol=1e-8, abs_tol=0 if key == "nominal_p" else 1e-12):
                return True
        elif a.upper() != b.upper():
            return True
    return False


def _coordinate(row):
    """Require explicit verified source convention, not just the graph contract.

    PanKgraph_08_04 variant rows were independently found to contain one-based
    starts despite the generic BED contract. No release-wide inference is safe.
    Current legacy nodes have no such verified marker and are never a fallback.
    """
    if row.get("coordinate_system_verified") is not True or row.get("coordinate_system") != "0-based-half-open":
        return None
    start, end = _integer(row.get("start")), _integer(row.get("end"))
    chrom = str(row.get("chromosome") or "").removeprefix("chr")
    if row.get("assembly") not in {"GRCh38", "GRCh38.p14", "hg38"}:
        return None
    if start is None or end is None or end <= start or not re.fullmatch(r"(?:[1-9]|1[0-9]|2[0-2]|X|Y|MT)", chrom):
        return None
    return {"chrom": chrom, "pos": start + 1, "assembly": "GRCh38", "verified": True,
        "source": "configured_graph_GRCh38_BED"}


def _valid_coordinate(value):
    return (isinstance(value, dict) and value.get("verified") is True
        and value.get("assembly") in {"GRCh38", "GRCh38.p14"}
        and isinstance(value.get("pos"), int) and not isinstance(value["pos"], bool) and value["pos"] > 0
        and bool(re.fullmatch(r"(?:[1-9]|1[0-9]|2[0-2]|X|Y|MT)", str(value.get("chrom", "")))))


def _manifest_extract(manifest_path, record, role, graph_version):
    """Read only operator-registered small extracts beside a versioned manifest.

    The browser supplies neither paths nor URLs. Extracts use the already
    supported seven-column association format and must be the exact credible
    set; regional observations cannot silently acquire membership semantics.
    """
    path = Path(manifest_path)
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ResourceError("invalid_extract_manifest")
    manifest = json.loads(path.read_text())
    if manifest.get("version") != 1 or manifest.get("graph_version") != graph_version or not isinstance(manifest.get("extracts"), list):
        raise ResourceError("extract_manifest_release_mismatch")
    matches = [item for item in manifest["extracts"] if isinstance(item, dict)
        and item.get("record_id") == record["id"] and item.get("role") == role]
    if not matches:
        return None
    if len(matches) != 1:
        raise ResourceError("ambiguous_extract")
    item = matches[0]
    if (item.get("assembly") != "GRCh38" or item.get("scope") != "credible_set"
            or item.get("signal_id") != record["qtl_signal_id" if role == "qtl" else "gwas_credible_set_id"]
            or not _text(item.get("source_version")) or not _text(item.get("label"))
            or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", "")))):
        raise ResourceError("extract_context_mismatch")
    relative = Path(str(item.get("relative_path", "")))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ResourceError("invalid_extract_path")
    root = path.parent.resolve()
    target = root / relative
    if any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
        raise ResourceError("invalid_extract_path")
    if not target.resolve().is_relative_to(root) or not target.is_file() or target.stat().st_size > MAX_EXTRACT_BYTES:
        raise ResourceError("invalid_extract_size_or_path")
    with target.open("rb") as stream:
        raw = stream.read(MAX_EXTRACT_BYTES + 1)
    if len(raw) > MAX_EXTRACT_BYTES or hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise ResourceError("extract_checksum_mismatch")
    rows = parse_association_tsv(raw, max_rows=MAX_MEMBERS)
    return {"rows": rows, "raw": raw, "metadata": item}


class ColocExplorer:
    def __init__(self, query, resources, coordinates, graph_version, settings):
        self.query, self.resources, self.coordinates = query, resources, coordinates
        self.graph_version, self.settings = graph_version, settings
        self._catalog = None
        self._catalog_rows = {}
        self._catalog_time = 0
        self._catalog_lock = asyncio.Lock()
        self._detail_slots = asyncio.Semaphore(2)
        self._detail_requests = 0

    async def _paged(self, query, count_query, params, kind, maximum):
        count_step = await self.query.execute_query(count_query, params, "coloc_" + kind + "_count")
        count_rows = count_step.get("rows") or []
        total = _integer(count_rows[0].get("total")) if len(count_rows) == 1 else None
        if total is None or count_step.get("truncated") or count_step.get("status") == "failed":
            raise ColocUnavailable("The configured graph did not return a complete record count.")
        rows, notes = [], []
        max_rows = getattr(getattr(self.query, "settings", None), "max_rows", 1000)
        size = max(1, min(PAGE_SIZE, max_rows))
        while len(rows) < min(total, maximum):
            requested = min(size, total - len(rows), maximum - len(rows))
            step = await self.query.execute_query(query, {**params, "offset": len(rows), "page_size": requested},
                "coloc_" + kind + "_members")
            page = step.get("rows") or []
            if not page or len(page) > requested or step.get("status") == "failed":
                notes.append("The configured graph returned an incomplete page; omitted records are not evidence of absence.")
                break
            rows.extend(page)
            if len(page) < requested and not step.get("truncated"):
                notes.append("The graph changed during retrieval or returned an incomplete page.")
                break
        if total > maximum:
            notes.append(f"The bounded {kind} response retains {maximum} of {total} recorded rows.")
        return rows, {"complete": len(rows) == total, "total": total, "returned": len(rows),
            "omitted_count": max(0, total - len(rows)), "notes": notes}

    async def catalog(self):
        async with self._catalog_lock:
            if self._catalog is not None and time.monotonic() - self._catalog_time < CATALOG_TTL:
                return deepcopy(self._catalog)
            rows, coverage = await self._paged(CATALOG_QUERY, CATALOG_COUNT, {"disease_id": T1D}, "catalog", MAX_RECORDS)
            records, originals, conflicts = {}, {}, set()
            invalid = 0
            for row in rows:
                try:
                    record = record_from_row(row, self.graph_version)
                except (ValueError, TypeError):
                    invalid += 1
                    continue
                rid = record["id"]
                if rid in originals and originals[rid] != row:
                    conflicts.add(rid)
                records[rid], originals[rid] = record, row
            for rid in conflicts:
                records.pop(rid, None)
                originals.pop(rid, None)
            if invalid or conflicts:
                coverage["complete"] = False
                coverage["notes"].append(f"Excluded {invalid} malformed records and {len(conflicts)} conflicting record identities.")
            coverage["excluded_identity_count"] = invalid + len(conflicts)
            if len(records) != len(rows):
                coverage["notes"].append("Repeated identical source identities are represented once; retrieval and display counts remain separate.")
            coverage.update(returned=len(records), retrieved_rows=len(rows), scope="recorded_T1D_colocalization")
            self._catalog_rows = originals
            self._catalog = {"version": VERSION, "graph_version": self.graph_version, "checked_at": _now(),
                "records": sorted(records.values(), key=lambda record: (record["gene_name"], record["dataset"], record["id"])),
                "coverage": coverage}
            self._catalog_time = time.monotonic()
            return deepcopy(self._catalog)

    async def _members(self, record, role):
        context = COLOC_QTL_CONTEXT.get(record["dataset"])
        source = GWAS_SOURCE.get((record["data_source"], record["data_version"]))
        if role == "qtl" and context and source:
            match = QTL_MATCH
            params = {"gene_id": record["gene_id"], "signal_id": record["qtl_signal_id"],
                "data_source": context[0], "tissue_id": context[1]}
        elif role == "gwas" and source and record.get("gwas_credible_set_id"):
            match = GWAS_MATCH
            params = {"disease_id": record["disease_id"], "signal_id": record["gwas_credible_set_id"], "data_source": source}
        else:
            return [], {"complete": False, "total": None, "returned": 0, "omitted_count": None,
                "notes": [f"The {role.upper()} source/signal mapping is not registered for this coloc record."]}
        try:
            return await self._paged(match + MEMBER_RETURN, match + "RETURN count(r) AS total", params, role, MAX_MEMBERS)
        except asyncio.CancelledError:
            raise
        except Exception:
            return [], {"complete": False, "total": None, "returned": 0, "omitted_count": None,
                "notes": [f"The independent {role.upper()} membership lookup is unavailable; the recorded coloc result is retained."]}

    async def _extract(self, record, role):
        manifest = getattr(self.settings, "coloc_extract_manifest", "")
        if not manifest:
            return None
        return await asyncio.to_thread(_manifest_extract, manifest, record, role, self.graph_version)

    async def _raw_qtl(self, record):
        context = COLOC_QTL_CONTEXT.get(record["dataset"])
        source = source_for(context[0], getattr(self.resources, "registry", SOURCES)) if context else None
        if (not source or (record["data_source"], record["data_version"]) not in GWAS_SOURCE
                or not record["qtl_signal_id"].startswith(record["gene_id"] + "__")):
            return [], {"label": "QTL credible-set file", "status": "unavailable", "url": None,
                "sha256": None, "download_url": None, "coverage": {"scope": "credible_set", "complete": False, "returned": 0},
                "error_category": "source_or_gene_context_unmapped"}
        try:
            url = source.url(record["qtl_signal_id"])
        except ValueError:
            url = None
        result = {"label": source.aliases[0] + " credible-set file", "status": "unavailable", "url": url,
            "sha256": None, "download_url": None, "coverage": {"scope": "credible_set", "complete": False, "returned": 0}}
        try:
            async with asyncio.timeout(getattr(self.settings, "resource_timeout", 10) + 2):
                path, _, _ = await self.resources.download(source.id, record["qtl_signal_id"])
                def read():
                    if path.stat().st_size > MAX_EXTRACT_BYTES:
                        raise ResourceError("coloc_file_size_limit")
                    with path.open("rb") as stream:
                        raw = stream.read(MAX_EXTRACT_BYTES + 1)
                    if len(raw) > MAX_EXTRACT_BYTES:
                        raise ResourceError("coloc_file_size_limit")
                    return raw, parse_association_tsv(raw, max_rows=MAX_MEMBERS)
                raw, rows = await asyncio.to_thread(read)
            result.update(status="available", sha256=hashlib.sha256(raw).hexdigest(),
                download_url=self.settings.public_path + "/api/resources/download?" + urlencode(
                    {"source": source.id, "credible_set": record["qtl_signal_id"]}),
                coverage={"scope": "credible_set", "complete": True, "returned": len(rows)}, checked_at=_now())
            return rows, result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result["error_category"] = str(exc) if isinstance(exc, ResourceError) else "resource_unavailable"
            return [], result

    async def _file_members(self, record, role):
        notices = []
        try:
            extract = await self._extract(record, role)
        except asyncio.CancelledError:
            raise
        except Exception:
            extract = None
            notices.append(f"The registered {role.upper()} extract failed validation; it was not used.")
        if extract is not None:
            meta = extract["metadata"]
            supplied_url = meta.get("source_url")
            parts = urlsplit(supplied_url) if isinstance(supplied_url, str) else None
            url = supplied_url if parts and parts.scheme == "https" and parts.netloc and not parts.username else None
            return extract["rows"], {"label": meta["label"], "status": "available", "url": url,
                "sha256": meta["sha256"], "source_version": meta["source_version"],
                "download_url": self.settings.public_path + "/api/coloc/records/" + record["id"] + "/download/" + role,
                "coverage": {"scope": "credible_set", "complete": True, "returned": len(extract["rows"])},
                "checked_at": _now()}, notices
        if role == "qtl":
            rows, source = await self._raw_qtl(record)
            return rows, source, notices
        # The candidate GWAS S3 prefix has no verified source mapping for these
        # graph records. A denied/missing candidate is not an empty credible set.
        return [], None, notices

    async def detail(self, record_id):
        if not isinstance(record_id, str) or not re.fullmatch(r"[a-f0-9]{64}", record_id):
            raise KeyError("unknown_coloc_record")
        catalog = await self.catalog()
        record = next((item for item in catalog["records"] if item["id"] == record_id), None)
        if record is None:
            raise KeyError("unknown_coloc_record")
        original = deepcopy(self._catalog_rows[record_id])
        if self._detail_requests >= 10:
            raise ColocUnavailable("The colocalization request queue is full.")
        self._detail_requests += 1
        try:
            async with asyncio.timeout(40):
                async with self._detail_slots:
                    async with asyncio.timeout(35):
                        return await self._detail(record, original)
        finally:
            self._detail_requests -= 1

    async def extract_download(self, record_id, role):
        if role not in {"gwas", "qtl"}:
            raise KeyError("unknown_extract_role")
        catalog = await self.catalog()
        record = next((item for item in catalog["records"] if item["id"] == record_id), None)
        if record is None:
            raise KeyError("unknown_coloc_record")
        extract = await self._extract(record, role)
        if extract is None:
            raise KeyError("no_registered_extract")
        return extract["raw"], role + "-" + record_id + ".tsv"

    async def _detail(self, record, original):
        gwas_result, qtl_result, gwas_file, qtl_file = await asyncio.gather(
            self._members(record, "gwas"), self._members(record, "qtl"),
            self._file_members(record, "gwas"), self._file_members(record, "qtl"))
        notices = list(record["notices"])
        sources, members, graph_nodes, graph_edges, kg_coordinates = [], {}, {}, [], {}
        gene, disease = record["gene_id"], record["disease_id"]
        graph_nodes[gene] = {"id": gene, "labels": ["Gene"], "properties": {"id": gene, "name": record["gene_name"]}}
        graph_nodes[disease] = {"id": disease, "labels": ["disease"], "properties": {"id": disease, "name": record["disease_name"]}}
        graph_edges.append({"start_id": gene, "end_id": disease, "type": "SIGNAL_COLOC_WITH", "properties": original["properties"]})
        branch_coverage = {}
        unverified_graph_coordinates = set()
        for role, (kg_rows, kg_coverage), (raw_rows, raw_source, file_notices) in (
                ("gwas", gwas_result, gwas_file), ("qtl", qtl_result, qtl_file)):
            notices.extend(kg_coverage["notes"] + file_notices)
            source_rows, invalid, conflicts = {}, 0, set()
            for row in kg_rows:
                rid, props = _text(row.get("variant_id")), row.get("properties")
                if not rid or not isinstance(props, dict):
                    invalid += 1
                    continue
                association = _association(props)
                if rid in source_rows and _conflicting_associations(source_rows[rid], association):
                    conflicts.add(rid)
                source_rows[rid] = association
                graph_nodes[rid] = {"id": rid, "labels": ["sequence_variant"],
                    "properties": {"id": rid, "chr": row.get("chromosome"), "start_loc": row.get("start"),
                        "end_loc": row.get("end"), "genome_assembly": row.get("assembly"), "ref": row.get("ref"), "alt": row.get("alt")}}
                graph_edges.append({"start_id": rid, "end_id": gene if role == "qtl" else disease,
                    "type": "PART_OF_QTL_SIGNAL" if role == "qtl" else "PART_OF_GWAS_SIGNAL", "properties": props})
                coordinate = _coordinate(row)
                if coordinate:
                    if rid in kg_coordinates and kg_coordinates[rid] != coordinate:
                        kg_coordinates[rid] = None
                    elif rid not in kg_coordinates:
                        kg_coordinates[rid] = coordinate
                elif row.get("start") is not None:
                    unverified_graph_coordinates.add(rid)
            kg_source = {"label": "Neo4j " + role.upper() + " signal membership", "status": "available" if kg_coverage["complete"] else "partial",
                "url": None, "sha256": hashlib.sha256(json.dumps(kg_rows, sort_keys=True, default=str).encode()).hexdigest(),
                "download_url": None, "coverage": {**kg_coverage, "scope": "recorded_signal_membership"}}
            source_versions = sorted({_text(row.get("properties", {}).get("data_version")) for row in kg_rows}
                - {None})
            kg_source["source_versions"] = source_versions
            if len(source_versions) > 1:
                notices.append(f"The {role.upper()} membership lookup returned multiple source versions; coverage is marked ambiguous.")
                kg_source["status"] = "partial"
            sources.append(kg_source)
            if raw_source is not None:
                sources.append(raw_source)
            expected_sizes = {_integer(row.get("properties", {}).get("n_snp" if role == "qtl" else "credible_set_size")) for row in kg_rows}
            expected_sizes.discard(None)
            declared_size = next(iter(expected_sizes)) if len(expected_sizes) == 1 else None
            size_conflict = len(expected_sizes) > 1 or declared_size == 0
            if size_conflict:
                notices.append(f"The graph has inconsistent or invalid recorded {role.upper()} credible-set sizes.")
            if raw_source and raw_source["status"] == "available":
                # The exact registered file supplies all members. KG properties
                # remain intact in the graph; disagreements are never hidden.
                for raw in raw_rows:
                    rid, association = raw["snp"], _association(raw)
                    if rid in source_rows and _conflicting_associations(source_rows[rid], association):
                        conflicts.add(rid)
                    source_rows[rid] = association
                raw_ids = {row["snp"] for row in raw_rows}
                extras = set(source_rows) - raw_ids
                if extras:
                    notices.append(f"{len(extras)} {role.upper()} graph members are absent from the exact source file; both are retained and coverage is partial.")
                expected = declared_size if declared_size is not None else len(raw_rows)
                complete = not extras and len(raw_rows) == expected and expected > 0
                if len(raw_rows) != expected:
                    notices.append(f"The {role.upper()} file contains {len(raw_rows)} rows but the graph records a credible-set size of {expected}; coverage is partial.")
            else:
                expected = declared_size
                complete = kg_coverage["complete"] and expected is not None and expected > 0 and len(source_rows) == expected
                if role == "qtl":
                    notices.append("The complete QTL source file is unavailable; only independently recorded graph members are shown.")
            missing_leads = set(record[role + "_leads"]) - set(source_rows)
            if missing_leads:
                notices.append(f"{len(missing_leads)} recorded {role.upper()} lead variants are not present in the retrieved members; coverage is partial.")
            for rid in conflicts:
                # A single glyph must not choose between inconsistent evidence.
                source_rows[rid] = {key: True if key == "member" else None for key in _association({})}
            if invalid or conflicts:
                notices.append(f"{role.upper()} membership has {invalid} malformed rows and {len(conflicts)} conflicting variants; conflicting statistics are withheld.")
            complete = (complete and not invalid and not conflicts and not file_notices and not size_conflict
                and not missing_leads and len(source_versions) <= 1)
            branch_coverage[role] = {"complete": complete, "expected": expected,
                "returned": len(source_rows), "omitted": max(0, expected - len(source_rows)) if expected is not None else None}
            members[role] = source_rows

        variant_ids = sorted(set(members["gwas"]) | set(members["qtl"]))
        omitted = max(0, len(variant_ids) - MAX_MEMBERS)
        variant_ids = variant_ids[:MAX_MEMBERS]
        if omitted:
            notices.append(f"The response limit omits {omitted} variants; full source files remain available separately.")
        coordinates = {rid: value for rid, value in kg_coordinates.items() if value and rid in variant_ids}
        if unverified_graph_coordinates:
            notices.append("Legacy graph variant positions lack a verified coordinate convention and are not used for locus placement; independent dbSNP coordinates are used when available.")
        try:
            looked_up = await asyncio.wait_for(self.coordinates(variant_ids), timeout=12)
            if not isinstance(looked_up, dict):
                raise ValueError("invalid_coordinate_response")
            coordinate_conflicts = []
            for rid, value in looked_up.items():
                if rid not in variant_ids or not _valid_coordinate(value):
                    continue
                previous = coordinates.get(rid)
                if previous and (previous["chrom"], previous["pos"]) != (value["chrom"], value["pos"]):
                    coordinate_conflicts.append(rid)
                    coordinates.pop(rid, None)
                elif rid in kg_coordinates and kg_coordinates[rid] is None:
                    coordinate_conflicts.append(rid)
                else:
                    coordinates[rid] = value
            if coordinate_conflicts:
                notices.append(f"{len(coordinate_conflicts)} variants have conflicting GRCh38 coordinates across sources; their positions are withheld.")
        except asyncio.CancelledError:
            raise
        except Exception:
            notices.append("The shared coordinate lookup is unavailable; only positions with an independently verified assembly and coordinate convention can be retained.")
        variants = [{"id": rid, "chromosome": coordinates.get(rid, {}).get("chrom"),
            "position": coordinates.get(rid, {}).get("pos"), "coordinate_source": coordinates.get(rid, {}).get("source"),
            "gwas": members["gwas"].get(rid), "qtl": members["qtl"].get(rid)} for rid in variant_ids]
        missing_coordinates = len(variant_ids) - len(coordinates)
        if missing_coordinates:
            notices.append(f"{missing_coordinates} variants lack verified GRCh38 coordinates; they remain in the membership table.")
        complete = all(value["complete"] for value in branch_coverage.values()) and not omitted
        coverage = {"gwas_count": sum(row["gwas"] is not None for row in variants),
            "qtl_count": sum(row["qtl"] is not None for row in variants),
            "shared_count": sum(row["gwas"] is not None and row["qtl"] is not None for row in variants),
            "coordinate_count": len(coordinates), "variant_count": len(variants), "scope": "credible_set",
            "complete": complete, "gwas_complete": branch_coverage["gwas"]["complete"] and not omitted,
            "qtl_complete": branch_coverage["qtl"]["complete"] and not omitted,
            "gwas_expected": branch_coverage["gwas"]["expected"], "qtl_expected": branch_coverage["qtl"]["expected"],
            "gwas_omitted": branch_coverage["gwas"]["omitted"], "qtl_omitted": branch_coverage["qtl"]["omitted"],
            "omitted_count": omitted, "coordinate_omitted": missing_coordinates,
            "notes": ["Credible-set subsets, not all variants tested for colocalization. Shared membership is not a new colocalization result.",
                "A missing dataset side means not present in the returned members; consult completeness before interpreting absence.",
                "No LD estimates, new coloc probabilities or effect-direction harmonization are computed."]}
        optional_sources_complete = all(source["status"] == "available" for source in sources)
        # Keep only real KG records in the graph. S3-only members remain in the
        # table/tracks, rather than becoming invented database relationships.
        included = {gene, disease, *variant_ids}
        graph_evidence = {"graph_version": self.graph_version,
            "nodes": [node for rid, node in graph_nodes.items() if rid in included],
            "edges": [edge for edge in graph_edges if edge["start_id"] in included],
            "completeness": "complete" if complete else "partial"}
        try:
            graph = project_evidence(graph_evidence, [gene, disease])["combined_query_result"]
        except Exception:
            graph = project_evidence({"graph_version": self.graph_version,
                "nodes": [graph_nodes[gene], graph_nodes[disease]], "edges": graph_edges[:1]}, [gene, disease])["combined_query_result"]
            notices.append("Graph display is limited to the recorded coloc edge; full available membership remains in the table.")
        return {"version": VERSION, "record": record, "graph_version": self.graph_version, "checked_at": _now(),
            "variants": variants, "coverage": coverage, "sources": sources, "graph": graph,
            "coordinate_build": "GRCh38" if coordinates else None,
            "status": "ready" if complete and optional_sources_complete and not missing_coordinates and not record["notices"] else "partial",
            "notices": list(dict.fromkeys(notices))}
