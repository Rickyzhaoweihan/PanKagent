"""Exercise the generic SQL loader with synthetic rows in a rollback transaction."""
import argparse
import json
from pathlib import Path
import tempfile

import psycopg
from cakg.multimodal import make_node, make_edge, make_context, make_record, load_postgres_files
from .manage import NAME, PORTS, owned_root, private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    owned_root(args.root)
    gene = make_node("Gene", "synthetic", "fixture-gene", {"name": "Synthetic fixture gene"})
    cell = make_node("Cell_type", "synthetic", "fixture-cell", {"name": "Synthetic fixture cell"})
    edge = make_edge(gene["id"], "HAS_EXPRESSION_RESULT_IN", cell["id"], {"assertion_status": "source_reported"})
    with tempfile.TemporaryDirectory(prefix="synthetic-fixture-", dir=args.root / "audit") as work:
        paths = []
        for number in range(2):
            file_id = "synthetic-file-" + str(number)
            source = {"file_id": file_id, "dataset_id": "synthetic-dataset", "url": "https://example.org/synthetic",
                      "sha256": str(number) * 64, "status": "released", "metadata": {"is_synthetic": True}}
            context = make_context("synthetic_expression", "source_expression_result", "synthetic-" + str(number),
                                   {"is_synthetic": True, "source_file_id": file_id}, cell["id"])
            record = make_record("synthetic_expression", context["id"], file_id, "row:1", {},
                                 {"value": "001.0"}, {"is_synthetic": True}, [gene["id"], cell["id"], edge["id"]])
            bundle = {"snapshot_id": "synthetic-rollback-only", "manifest": {"source_row_counts": {file_id: 1}},
                      "nodes": [gene, cell], "edges": [edge], "contexts": [context], "records": [record],
                      "sources": [source], "rejections": []}
            path = Path(work) / (str(number) + ".json")
            path.write_text(json.dumps(bundle))
            paths.append(path)
        with psycopg.connect(host=str(args.root / "socket"), port=PORTS["postgres"], dbname=NAME, user="serviceuser") as conn:
            conn.execute("SELECT 1")  # Outer transaction; loader uses a savepoint.
            result = load_postgres_files(paths, conn, "synthetic-rollback-only", {})
            assert result["counts"]["nodes"] == 2 and result["counts"]["edges"] == 1
            assert result["counts"]["records"] == 2 and result["counts"]["evidence_links"] == 6
            assert conn.execute("SELECT raw_record->>'value' FROM cakg_mm.evidence_record LIMIT 1").fetchone()[0] == "001.0"
            conn.rollback()
            assert conn.execute("SELECT to_regnamespace('cakg_mm')").fetchone()[0] is None
    result = {"status": "passed", "synthetic_rows": 2, "all_fixture_state_rolled_back": True,
              "checked": ["SQL migration", "multi-file deduplication", "canonical identities", "evidence foreign keys", "raw string preservation", "transaction rollback"]}
    private_json(args.root / "audit" / "synthetic-fixture.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
