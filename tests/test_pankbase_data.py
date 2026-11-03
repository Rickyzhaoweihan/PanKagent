import array
import base64
import gzip
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from pankgraph_results.pankbase_data import create_app, byte_range, PREFIX


@pytest.fixture
def snapshot(tmp_path):
    content = b"0123456789"
    (tmp_path / "track.bw").write_bytes(content)
    (tmp_path / "delivery-manifest.json").write_text(
        json.dumps(
            {
                "snapshot": "test",
                "assets": [
                    {
                        "path": "track.bw",
                        "bytes": 10,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            }
        )
    )
    expression = tmp_path / "expression"
    expression.mkdir()
    (expression / "indices.bin").write_bytes(array.array("I", [0, 2]).tobytes())
    (expression / "values.bin").write_bytes(
        array.array("d", [0.0004, 1.23456]).tobytes()
    )
    (expression / "index.json").write_text(
        json.dumps(
            {
                "datasetId": "dataset",
                "atlas": "map",
                "count": 3,
                "genes": {"INS": [0, 2]},
                "source_sha256": "source",
                "retrieved_at": "2026-11-03",
            }
        )
    )
    db = sqlite3.connect(tmp_path / "genome.sqlite")
    db.execute(
        "CREATE TABLE genes(name TEXT COLLATE NOCASE,chrom TEXT,tx_start INTEGER,tx_end INTEGER,record TEXT)"
    )
    for start, end in [(100, 200), (150, 250)]:
        db.execute(
            "INSERT INTO genes VALUES (?,?,?,?,?)",
            (
                "CFTR",
                "chr7",
                start,
                end,
                json.dumps(
                    {"name": "CFTR", "chrom": "chr7", "txStart": start, "txEnd": end}
                ),
            ),
        )
    db.commit()
    db.close()
    return tmp_path


def test_range_and_head_read_only(snapshot):
    client = TestClient(create_app(snapshot))
    r = client.get(PREFIX + "/assets/track.bw", headers={"Range": "bytes=3-5"})
    assert (
        r.status_code == 206
        and r.content == b"345"
        and r.headers["content-range"] == "bytes 3-5/10"
    )
    assert (
        client.get(PREFIX + "/assets/track.bw", headers={"Range": "bytes=-2"}).content
        == b"89"
    )
    assert client.head(PREFIX + "/assets/track.bw").content == b""
    for header in ["bytes=10-", "bytes=9-3", "bytes=-0", "bytes=0-1,3-4", "invalid"]:
        assert (
            client.get(
                PREFIX + "/assets/track.bw", headers={"Range": header}
            ).status_code
            == 416
        )
    assert client.post(PREFIX + "/assets/track.bw").status_code == 405
    assert client.get(PREFIX + "/assets/not-registered").status_code == 404
    assert client.get(PREFIX + "/assets/%2e%2e/private").status_code == 404
    etag = client.get(PREFIX + "/assets/track.bw").headers["etag"]
    assert (
        client.get(
            PREFIX + "/assets/track.bw", headers={"If-None-Match": etag}
        ).status_code
        == 304
    )
    (snapshot / "track.bw").write_bytes(b"changed")
    assert client.get(PREFIX + "/assets/track.bw").status_code == 503


def test_expression_precision_identity_and_missing_data(snapshot):
    client = TestClient(create_app(snapshot))
    params = {"gene": "INS", "dataset": "dataset", "atlas": "map"}
    response = client.get(PREFIX + "/expression", params=params)
    assert response.status_code == 200
    value = response.json()
    ix = array.array("I")
    ix.frombytes(base64.b64decode(value["indices"]))
    vx = array.array("d")
    vx.frombytes(base64.b64decode(value["values"]))
    assert list(ix) == [2] and list(vx) == [1.235]
    assert (
        client.get(
            PREFIX + "/expression", params={**params, "atlas": "other"}
        ).status_code
        == 409
    )
    assert (
        client.get(
            PREFIX + "/expression", params={**params, "gene": "MISSING"}
        ).status_code
        == 404
    )
    assert (
        client.get(
            PREFIX + "/expression", params={**params, "gene": "../INS"}
        ).status_code
        == 400
    )
    (snapshot / "expression/index.json").unlink()
    assert client.get("/health/ready").status_code == 503
    assert client.get(PREFIX + "/expression", params=params).status_code == 503


def test_gene_lookup_preserves_transcripts_and_primary_locus(snapshot):
    client = TestClient(create_app(snapshot))
    assert (
        client.get(PREFIX + "/locus", params={"gene": "CFTR"}).json()["data"][0]["end"]
        == 250
    )
    records = client.get(
        PREFIX + "/genome/hg38/genes/refGene/queryRegion",
        params={"chr": "chr7", "start": 190, "end": 220},
    ).json()
    assert len(records) == 2
    assert client.get(PREFIX + "/locus", params={"gene": "UNKNOWN"}).status_code == 404
    assert (
        client.get(
            PREFIX + "/genome/hg38/genes/refGene/queryRegion",
            params={"chr": "chr7", "start": 220, "end": 190},
        ).status_code
        == 400
    )
    assert client.get("/health/ready").json()["upstream_runtime_requests"] is False


def test_compressed_text_keeps_identity_ranges(snapshot):
    path = snapshot / "track.bw"
    packed = gzip.compress(path.read_bytes(), mtime=0)
    (snapshot / "track.gz").write_bytes(packed)
    manifest = json.loads((snapshot / "delivery-manifest.json").read_text())
    manifest["assets"][0]["gzip"] = {
        "path": "track.gz",
        "bytes": len(packed),
        "sha256": hashlib.sha256(packed).hexdigest(),
    }
    (snapshot / "delivery-manifest.json").write_text(json.dumps(manifest))
    client = TestClient(create_app(snapshot))
    r = client.get(PREFIX + "/assets/track.bw", headers={"Accept-Encoding": "gzip"})
    assert r.content == b"0123456789" and r.headers["content-encoding"] == "gzip"
    r = client.get(
        PREFIX + "/assets/track.bw",
        headers={"Accept-Encoding": "gzip", "Range": "bytes=0-1"},
    )
    assert r.content == b"01" and "content-encoding" not in r.headers
    r = client.get(PREFIX + "/assets/track.bw", headers={"Accept-Encoding": "gzip;q=0"})
    assert r.content == b"0123456789" and "content-encoding" not in r.headers
