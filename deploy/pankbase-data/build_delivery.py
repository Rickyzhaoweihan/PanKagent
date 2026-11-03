import gzip, hashlib, json, mimetypes, pathlib, sqlite3, sys

root = pathlib.Path(sys.argv[1])
assets = []


def add(path, source, release, mod, extra=None):
    p = root / path
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    assets.append(
        dict(
            path=path,
            bytes=p.stat().st_size,
            sha256=h.hexdigest(),
            source_url=source,
            source_release=release,
            module=mod,
            media_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
            **(extra or {}),
        )
    )


for prefix in ["", "supplement/", "runtime-source/"]:
    m = json.loads((root / prefix / "manifest.json").read_text())
    if not m["complete"]:
        raise ValueError("Incomplete source " + prefix)
    for item in m["assets"]:
        item = dict(item)
        item["path"] = prefix + item["path"]
        assets.append(item)
for folder in ["data", "atlas"]:
    for p in (root / folder).iterdir():
        if p.suffix == ".json":
            d = json.loads(p.read_text())
            source = d.get("source", {})
            extra = {"source_metadata": source}
            add(
                str(p.relative_to(root)),
                "Prepared from source records retained in source_metadata",
                "v3.4" if folder == "atlas" else "v3.3",
                "scbrowser" if folder == "atlas" else "de/pca",
                extra,
            )
for p in (root / "PCA_results").iterdir():
    if p.is_file():
        add(
            str(p.relative_to(root)),
            "https://github.com/PanKbase/PanKbase-RNA-expression-PCA-Explorer/blob/bc187eb072b8719e5dd52f93199757e15db63f06/app/outputs/PCA_results/"
            + p.name,
            "bc187eb072b8719e5dd52f93199757e15db63f06",
            "pca",
        )
# Serve the same pinned runtime, with only reference-data endpoints redirected.
p = root / "runtime-source/runtime/epgg.js"
raw = p.read_text()
patched = raw.replace(
    '"https://lambda.epigenomegateway.org/v2"', 'window.PANKBASE_DATA_BASE+"/genome"'
).replace(
    '"https://vizhub.wustl.edu/public/hg38/hg38.2bit"',
    'window.PANKBASE_DATA_BASE+"/assets/supplement/genome/hg38.2bit"',
)
assert patched != raw
p = root / "runtime-source/runtime/epgg-local.js"
p.write_text(patched)
add(
    str(p.relative_to(root)),
    "https://unpkg.com/epgg@53.6.0/umd/epgg.js",
    "53.6.0",
    "genome",
    {
        "changes": [
            "Redirect genome annotation and hg38 sequence data to local snapshot"
        ],
        "original_sha256": hashlib.sha256(raw.encode()).hexdigest(),
    },
)
# Preserve RefGene transcript structures for genome tracks and explicit locus lookup.
db = sqlite3.connect(root / "genome.sqlite")
db.executescript(
    "DROP TABLE IF EXISTS genes; CREATE TABLE genes(name TEXT COLLATE NOCASE,chrom TEXT,tx_start INTEGER,tx_end INTEGER,record TEXT); CREATE INDEX genes_name ON genes(name); CREATE INDEX genes_region ON genes(chrom,tx_start,tx_end);"
)
with gzip.open(root / "supplement/genome/refGene.txt.gz", "rt") as f:
    for line in f:
        r = line.rstrip("\n").split("\t")
        obj = {
            "id": r[1],
            "chrom": r[2],
            "strand": r[3],
            "txStart": int(r[4]),
            "txEnd": int(r[5]),
            "cdsStart": int(r[6]),
            "cdsEnd": int(r[7]),
            "exonCount": int(r[8]),
            "exonStarts": r[9],
            "exonEnds": r[10],
            "name": r[12],
            "description": "UCSC RefGene hg38 source snapshot",
            "transcriptionClass": "coding" if r[1].startswith("NM_") else "nonCoding",
        }
        db.execute(
            "INSERT INTO genes VALUES (?,?,?,?,?)",
            (obj["name"], obj["chrom"], obj["txStart"], obj["txEnd"], json.dumps(obj)),
        )
db.commit()
print("RefGene transcripts", db.execute("SELECT count(*) FROM genes").fetchone()[0])
db.close()
# Validate heatmap envelopes before activation.
for p in (root / "supplement/heatmaps").glob("*.json"):
    if p.name.endswith(".provenance.json"):
        continue
    d = json.loads(p.read_text())
    assert (
        not d.get("restricted")
        and not d.get("continuation")
        and isinstance(d.get("data"), list)
    )
    assert all(
        r["dataset"] == p.stem and isinstance(r["expression"], list) for r in d["data"]
    )
# Precompress text assets once, keeping byte-range binary responses unchanged.
for item in assets:
    p = root / item["path"]
    if (
        p.suffix in {".json", ".js", ".css", ".txt", ".tsv", ".csv"}
        and p.stat().st_size > 1024
    ):
        relative = "delivery-gzip/" + item["sha256"] + ".gz"
        target = root / relative
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            with p.open("rb") as src, target.open("wb") as dst, gzip.GzipFile(
                fileobj=dst, mode="wb", mtime=0, compresslevel=6
            ) as z:
                while chunk := src.read(1024 * 1024):
                    z.write(chunk)
        with target.open("rb") as f:
            h = hashlib.file_digest(f, "sha256").hexdigest()
        item["gzip"] = {"path": relative, "bytes": target.stat().st_size, "sha256": h}
manifest = {
    "version": 1,
    "snapshot": "pankbase-experience-20261103",
    "scope": ["scbrowser", "genome", "differential_expression", "functional", "pca"],
    "runtime_external_data_requests": False,
    "assets": assets,
    "notes": [
        "Source release identities are distinct; v3.3 PCA/DE and v3.4 atlas are not a paired cohort.",
        "Original software licenses do not grant blanket rights to source datasets.",
        "Expression alignment is validated separately before activation.",
    ],
}
expression = root / "expression/index.json"
if expression.exists():
    index = json.loads(expression.read_text())
    manifest["expression"] = {k: v for k, v in index.items() if k != "genes"}
    manifest["expression"]["gene_count"] = len(index["genes"])
(root / "delivery-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print("Delivery assets", len(assets), "bytes", sum(x["bytes"] for x in assets))
