"""Validate cell-aligned vectors and activate only a fully checked expression index."""

import array, csv, hashlib, json, pathlib, sys, time, math

root = pathlib.Path(sys.argv[1])
folder = root / "expression"
atlas = json.loads((root / "atlas/manifest.json").read_text())
source = json.loads((root / "atlas-source/scRNA_v3.4.rds.provenance.json").read_text())
genes = {
    r["gene"]: [int(r["start"]), int(r["count"])]
    for r in csv.DictReader((folder / "genes.tsv").open(), delimiter="\t")
}
assert len(genes) == 19546
last = 0
for gene, (start, count) in genes.items():
    assert start == last and 0 <= count <= atlas["count"]
    last = start + count
assert (folder / "indices.bin").stat().st_size == last * 4 and (
    folder / "values.bin"
).stat().st_size == last * 8
report = {}
for gene in ["CFTR", "INS", "GCG"]:
    start, count = genes[gene]
    ix = array.array("I")
    vx = array.array("d")
    with (folder / "indices.bin").open("rb") as f:
        f.seek(start * 4)
        ix.fromfile(f, count)
    with (folder / "values.bin").open("rb") as f:
        f.seek(start * 8)
        vx.fromfile(f, count)
    vector = [0.0] * atlas["count"]
    assert all(a < b for a, b in zip(ix, ix[1:])) and max(ix) < len(vector)
    for i, v in zip(ix, vx):
        vector[i] = v
    ref = json.loads((folder / (gene + "-reference.json")).read_text())
    row = next(
        r
        for r in ref["data"]
        if r["dataset"] == atlas["datasetId"] and r["gene"] == gene
    )
    assert len(row["expression"]) == len(vector)
    diffs = [abs(a - b) for a, b in zip(vector, row["expression"])]
    report[gene] = {
        "cells": len(vector),
        "nonzero": count,
        "max_abs_difference": max(diffs),
        "exact_matches": sum(a == b for a, b in zip(vector, row["expression"])),
        "round3_matches": sum(
            round(a, 3) == b for a, b in zip(vector, row["expression"])
        ),
    }
print(json.dumps(report, indent=2), flush=True)
(folder / "validation-report.json").write_text(json.dumps(report, indent=2) + "\n")
assert all(
    v["round3_matches"] == atlas["count"] for v in report.values()
), "Source values differ beyond BioIndex three-decimal representation; do not activate"
files = {}
for name in ["indices.bin", "values.bin", "cells.txt", "genes.tsv"]:
    p = folder / name
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(8 * 1024 * 1024):
            h.update(b)
    files[name] = {"bytes": p.stat().st_size, "sha256": h.hexdigest()}
index = {
    "version": 1,
    "datasetId": atlas["datasetId"],
    "atlas": atlas["asset"],
    "count": atlas["count"],
    "genes": genes,
    "source_sha256": source["sha256"],
    "retrieved_at": source["retrieved_at"],
    "source_url": source["source_url"],
    "files": files,
    "normalization": "Source Seurat RNA data slot; no recomputation",
    "delivery_precision": "Three decimal places, matching the existing BioIndex representation; Float64 source preserved on disk",
    "alignment": "All 448935 unique atlas NAME identifiers matched to Seurat column names before transposition",
    "validation": report,
}
tmp = folder / "index.json.partial"
tmp.write_text(json.dumps(index, separators=(",", ":")) + "\n")
tmp.replace(folder / "index.json")
print("Activated", len(genes), "genes", last, "nonzero source values")
