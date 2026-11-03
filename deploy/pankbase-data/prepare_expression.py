"""Prepare exact atlas cell identifiers and source reference vectors for validation."""

import gzip, json, pathlib, sys, urllib.request

root = pathlib.Path(sys.argv[1])
out = root / "expression"
out.mkdir(exist_ok=True)
p = root / "atlas-source/fields.json.gz"
with gzip.open(p, "rt") as f:
    fields = json.load(f)
(out / "cells.txt").write_text("\n".join(fields["NAME"]) + "\n")
for gene in ["CFTR", "INS", "GCG"]:
    url = (
        "https://bioindex.pankbase.org/api/bio/query/single-cell-lognorm?q=islet_of_Langerhans_scRNA_v3-4,"
        + gene
    )
    with urllib.request.urlopen(url, timeout=180) as response:
        value = json.load(response)
    assert not value.get("restricted") and not value.get("continuation")
    (out / (gene + "-reference.json")).write_text(json.dumps(value))
print("Prepared", len(fields["NAME"]), "cell identities and three reference genes")
