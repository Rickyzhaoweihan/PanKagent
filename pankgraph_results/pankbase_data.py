"""Read-only independent module data service. All reads use an explicit local snapshot."""

import array
import base64
import gzip
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import sys
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

PREFIX = "/api/pankbase-data/v1"


def byte_range(header, size):
    if not header:
        return 0, size - 1, 200
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not match or not any(match.groups()):
        raise HTTPException(
            416,
            "One byte range is required",
            headers={"Content-Range": f"bytes */{size}"},
        )
    first, last = match.groups()
    start = int(first) if first else max(0, size - int(last))
    end = min(size - 1, int(last)) if first and last else size - 1
    if start >= size or end < start or (not first and int(last) == 0):
        raise HTTPException(
            416, "Range outside file", headers={"Content-Range": f"bytes */{size}"}
        )
    return start, end, 206


def create_app(root=None):
    root = Path(root or os.environ["PANKBASE_DATA_ROOT"]).resolve()
    manifest = json.loads((root / "delivery-manifest.json").read_text())
    assets = {item["path"]: item for item in manifest["assets"]}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health/ready")
    def ready():
        if not (root / "expression/index.json").exists():
            raise HTTPException(503, "Expression snapshot is still being prepared")
        expression_index()
        return {
            "status": "ready",
            "snapshot": manifest["snapshot"],
            "assets": len(assets),
            "upstream_runtime_requests": False,
            "expression_ready": (root / "expression/index.json").exists(),
        }

    @app.get(PREFIX + "/manifest")
    def metadata():
        return manifest

    @app.api_route(PREFIX + "/assets/{name:path}", methods=["GET", "HEAD"])
    def asset(name: str, request: Request):
        item = assets.get(name)
        if not item:
            raise HTTPException(404, "Asset not in this source snapshot")
        file = (root / name).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            raise HTTPException(503, "Snapshot file unavailable")
        size = file.stat().st_size
        if size != item["bytes"]:
            raise HTTPException(503, "Snapshot size mismatch")
        compressed = bool(
            item.get("gzip")
            and not request.headers.get("range")
            and any(
                part.strip().split(";")[0] == "gzip"
                and not re.search(r";\s*q=0(?:\.0*)?$", part.strip())
                for part in request.headers.get("accept-encoding", "").split(",")
            )
        )
        representation = item["gzip"] if compressed else item
        if compressed:
            file = (root / representation["path"]).resolve()
            if (
                not file.is_relative_to(root)
                or not file.is_file()
                or file.stat().st_size != representation["bytes"]
            ):
                raise HTTPException(503, "Compressed asset unavailable")
            size = representation["bytes"]
        etag = '"' + representation["sha256"] + '"'
        headers = {
            "ETag": etag,
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
            "X-Source-Snapshot": manifest["snapshot"],
            "X-Content-Type-Options": "nosniff",
        }
        headers["Vary"] = "Accept-Encoding"
        if compressed:
            headers["Content-Encoding"] = "gzip"
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        start, end, status = byte_range(request.headers.get("range"), size)
        length = max(0, end - start + 1)
        headers["Content-Length"] = str(length)
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        media = (
            item.get("media_type")
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        if request.method == "HEAD":
            return Response(status_code=status, headers=headers, media_type=media)

        def chunks():
            with file.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            chunks(), status_code=status, headers=headers, media_type=media
        )

    def database():
        return sqlite3.connect(f'file:{root / "genome.sqlite"}?mode=ro', uri=True)

    @app.get(PREFIX + "/genome/hg38/genes/refGene/queryRegion")
    def region(chr: str, start: int, end: int):
        if (
            not re.fullmatch(r"chr(?:[1-9]|1[0-9]|2[0-2]|X|Y|M)", chr)
            or start < 0
            or end <= start
        ):
            raise HTTPException(400, "Invalid GRCh38 region")
        with database() as db:
            rows = db.execute(
                "SELECT record FROM genes WHERE chrom=? AND tx_start<? AND tx_end>?",
                (chr, end, start),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    @app.get(PREFIX + "/genome/hg38/genes/queryName")
    def names(q: str, getOnlyNames: bool = False, isExact: bool = False):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", q):
            raise HTTPException(400, "Invalid gene symbol")
        with database() as db:
            if getOnlyNames:
                return [
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT name FROM genes WHERE name LIKE ? ORDER BY name LIMIT 50",
                        (q + "%",),
                    )
                ]
            rows = db.execute(
                "SELECT record FROM genes WHERE name=? COLLATE NOCASE", (q,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    @app.get(PREFIX + "/locus")
    def locus(gene: str):
        rows = names(gene)
        chromosomes = {
            r["chrom"]
            for r in rows
            if re.fullmatch(r"chr(?:[1-9]|1[0-9]|2[0-2]|X|Y|M)", r["chrom"])
        }
        if len(chromosomes) != 1:
            raise HTTPException(
                404, "No unique primary-assembly gene locus in this RefGene snapshot"
            )
        chrom = chromosomes.pop()
        rows = [r for r in rows if r["chrom"] == chrom]
        return {
            "data": [
                {
                    "chromosome": chrom[3:],
                    "start": min(r["txStart"] for r in rows),
                    "end": max(r["txEnd"] for r in rows),
                    "genomeBuild": "GRCh38",
                    "source": "UCSC RefGene",
                    "scope": "span of source transcripts on one primary chromosome",
                }
            ]
        }

    @lru_cache(maxsize=1)
    def expression_index():
        index = json.loads((root / "expression/index.json").read_text())
        aliases = {symbol.upper(): symbol for symbol in index["genes"]}
        if len(aliases) != len(index["genes"]):
            raise HTTPException(503, "Ambiguous source gene symbols")
        for name, identity in index.get("files", {}).items():
            path = (root / "expression" / name).resolve()
            if (
                not path.is_relative_to(root / "expression")
                or not path.is_file()
                or path.stat().st_size != identity["bytes"]
            ):
                raise HTTPException(
                    503, "Expression snapshot file unavailable or truncated"
                )
        index["aliases"] = aliases
        return index

    @app.get(PREFIX + "/expression")
    def expression(gene: str, dataset: str, atlas: str):
        index_path = root / "expression/index.json"
        if not index_path.exists():
            raise HTTPException(503, "Expression snapshot is still being prepared")
        index = expression_index()
        if dataset != index["datasetId"] or atlas != index["atlas"]:
            raise HTTPException(409, "Atlas identity mismatch")
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,79}", gene):
            raise HTTPException(400, "Invalid gene symbol")
        source_gene = index["aliases"].get(gene)
        record = index["genes"].get(source_gene)
        if record is None:
            raise HTTPException(404, "Gene not recorded in this source snapshot")
        start, count = record
        with (root / "expression/indices.bin").open("rb") as f:
            f.seek(start * 4)
            indices = f.read(count * 4)
        with (root / "expression/values.bin").open("rb") as f:
            f.seek(start * 8)
            values = f.read(count * 8)
        if len(indices) != count * 4 or len(values) != count * 8:
            raise HTTPException(503, "Incomplete expression snapshot")
        # Match the existing browser representation without rounding the retained source matrix.
        source_ix = array.array("I")
        source_ix.frombytes(indices)
        source_vx = array.array("d")
        source_vx.frombytes(values)
        if sys.byteorder != "little":
            source_ix.byteswap()
            source_vx.byteswap()
        pairs = [
            (i, round(v, 3)) for i, v in zip(source_ix, source_vx) if round(v, 3) > 0
        ]
        out_ix = array.array("I", (i for i, _ in pairs))
        out_vx = array.array("d", (v for _, v in pairs))
        if sys.byteorder != "little":
            out_ix.byteswap()
            out_vx.byteswap()
        indices, values = out_ix.tobytes(), out_vx.tobytes()
        value = {
            "version": 1,
            "datasetId": dataset,
            "atlas": atlas,
            "gene": gene,
            "source_gene": source_gene,
            "count": index["count"],
            "encoding": "sparse-u32-f64-le",
            "indices": base64.b64encode(indices).decode(),
            "values": base64.b64encode(values).decode(),
            "source_sha256": index["source_sha256"],
            "retrieved_at": index["retrieved_at"],
        }
        raw = gzip.compress(
            json.dumps(value, separators=(",", ":")).encode(), compresslevel=6
        )
        return Response(
            raw,
            media_type="application/json",
            headers={
                "Content-Encoding": "gzip",
                "Cache-Control": "public, max-age=3600",
            },
        )

    return app
