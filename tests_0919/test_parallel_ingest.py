"""Small synthetic fixtures compare parallel preparation with frozen adapters."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from pankgraph0919_ingest.adapters import build_bundles, digest
from pankgraph0919_ingest.parallel_build import build_parallel, combine_worker_index


class ParallelIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.reference = self.root / "genes.csv"
        with self.reference.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([":ID", "name:String", "hgnc_symbol:String"])
            writer.writerow(["ENSG00000000001", "FICTION_GENE", "FICTION_GENE"])
        self.catalog = {"retrieved_utc": "unit-test", "files": [], "analyses": []}
        self.acquired = {"snapshot_id": "unit-test", "files": {}}
        for suffix in ("ONE", "TWO"):
            key, dataset = "PKBFI_" + suffix, "PKBDS_" + suffix
            path = self.root / (key + ".tsv")
            path.write_text("gene\tpvalue\nFICTION_GENE\t0.9\nUNRESOLVED\t0.1\n")
            source = {"accession": key, "file_set": "/analysis-sets/" + dataset + "/",
                      "@type": ["TabularFile"], "status": "released", "controlled_access": False,
                      "file_url": "https://api.data.pankbase.org/" + key}
            self.catalog["files"].append(source)
            self.catalog["analyses"].append({"accession": dataset,
                "aliases": ["example:allFunctionalTraits_FICTION_Beta"], "description": "Fictional unit fixture"})
            self.acquired["files"][key] = {"status": "downloaded", "path": str(path),
                                           "sha256": digest(path), "bytes": path.stat().st_size}
        missing = {**self.catalog["files"][0], "accession": "PKBFI_MISSING"}
        self.catalog["files"].append(missing)
        self.acquired["files"]["PKBFI_MISSING"] = {"status": "blocked", "reason": "unit_test_unavailable"}

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def contents(root, index):
        records, nodes, edges, contexts, sources = {}, {}, {}, {}, {}
        for entry in index["bundles"]:
            path = root / entry["path"]
            if digest(path) != entry["sha256"]:
                raise AssertionError("changed bundle")
            bundle = json.loads(path.read_text())
            for key, target, identity in (("records", records, "id"), ("nodes", nodes, "id"),
                    ("edges", edges, "id"), ("contexts", contexts, "id"), ("sources", sources, "file_id")):
                for row in bundle[key]:
                    target[row[identity]] = row
        return records, nodes, edges, contexts, sources

    def test_parallel_matches_sequential_records_identity_and_accounting(self):
        sequential = self.root / "sequential"
        build_bundles(copy.deepcopy(self.catalog), self.acquired, sequential, self.reference, chunk_rows=1)
        seq_index = json.loads((sequential / "bundle_index.json").read_text())
        parallel = self.root / "parallel"
        par_index = build_parallel(self.catalog, self.acquired, parallel, self.reference,
                                   workers=2, chunk_rows=1)
        self.assertEqual(self.contents(sequential, seq_index), self.contents(parallel, par_index))
        self.assertEqual(par_index["source_row_counts"], {"PKBFI_ONE": 2, "PKBFI_TWO": 2, "PKBFI_MISSING": 0})
        self.assertEqual(par_index["modalities"], seq_index["modalities"])
        self.assertEqual(sum("source_file_id" not in b for b in par_index["bundles"]), 1)
        self.assertEqual(set(par_index["manifest"]["source_row_counts"]), {s["accession"] for s in self.catalog["files"]})
        self.assertIn("parallel_build.py", par_index["manifest"]["adapter_code_sha256"])
        self.assertEqual(par_index["runner"]["workers"], 2)
        for entry in par_index["bundles"][1:]:
            self.assertTrue(entry["path"].startswith("per_file/"))

    def test_limits_and_existing_output_fail_before_publication(self):
        for workers in (0, 5):
            with self.assertRaisesRegex(ValueError, "workers"):
                build_parallel(self.catalog, self.acquired, self.root / "bad", self.reference, workers=workers)
        target = self.root / "existing"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(ValueError, "must be empty"):
            build_parallel(self.catalog, self.acquired, target, self.reference)
        self.assertEqual((target / "keep.txt").read_text(), "keep")
        self.assertFalse((target / "bundle_index.json").exists())

    def test_merge_rejects_changed_chunk_hash(self):
        worker_root = self.root / "global" / "per_file" / "PKBFI_ONE"
        one_catalog = {**self.catalog, "files": [self.catalog["files"][0]]}
        one_manifest = {**self.acquired, "files": {"PKBFI_ONE": self.acquired["files"]["PKBFI_ONE"]}}
        build_bundles(copy.deepcopy(one_catalog), one_manifest, worker_root, self.reference)
        worker = json.loads((worker_root / "bundle_index.json").read_text())
        data_entry = next(e for e in worker["bundles"] if "source_file_id" in e)
        with (worker_root / data_entry["path"]).open("a") as handle:
            handle.write(" ")
        index = {"snapshot_id": "unit-test", "files": {}, "modalities": {}, "source_row_counts": {}, "bundles": []}
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            combine_worker_index(index, worker, worker_root, self.root / "global", "PKBFI_ONE")


if __name__ == "__main__":
    unittest.main()
