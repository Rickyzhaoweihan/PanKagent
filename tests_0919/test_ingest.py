"""Synthetic unit fixtures only; production ingestion never generates source rows."""
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error

from cakg.multimodal import validate_bundle
from pankgraph0919_ingest.acquire import AcquisitionBlocked, Downloader, public_url
from pankgraph0919_ingest.adapters import (GeneReference, build_bundles, digest,
                                         file_chunks, numeric_metrics, source_entry)
from pankgraph0919_ingest.catalog import CatalogClient, classify_analysis, sanitize


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.reference_path = self.directory / "reference.csv"
        with self.reference_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([":ID", "name:String", "hgnc_symbol:String"])
            writer.writerows([
                ["ENSG00000000001", "FICTION_A", "FICTION_A"],
                ["ENSG00000000002", "FICTION_AMBIGUOUS", "FICTION_B"],
                ["ENSG00000000003", "FICTION_AMBIGUOUS", "FICTION_C"],
            ])
        self.reference = GeneReference(self.reference_path)

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self, text, modality="05_functional", **extra):
        path = self.directory / "source.tsv"
        path.write_text(text)
        source = {"accession": "PKBFI_TEST", "file_set": "/analysis-sets/PKBDS_TEST/",
                  "status": "released", "controlled_access": False, "@type": ["TabularFile"],
                  "file_url": "https://pankbase-data-v1.s3.us-west-2.amazonaws.com/test.tsv",
                  "modality": modality, **extra}
        acquired = {"status": "downloaded", "path": str(path), "sha256": digest(path), "bytes": path.stat().st_size}
        analysis = {"accession": "PKBDS_TEST", "aliases": ["example:allFunctionalTraits_FICTION_Beta"]}
        return source, acquired, analysis

    def test_reference_only_exact_unique_current_names(self):
        self.assertEqual(self.reference.resolve("FICTION_A")[0], "ENSG00000000001")
        self.assertEqual(self.reference.resolve("ENSG00000000001.4")[0], "ENSG00000000001")
        for value in ("fiction_a", "FICTION_AMBIGUOUS", "ENSG99999999999", "missing"):
            self.assertIsNone(self.reference.resolve(value)[0])

    def test_unknown_access_and_unapproved_url_never_download(self):
        source, _, _ = self.fixture("gene\nFICTION_A\n")
        downloader = Downloader(self.directory / "downloads", 100, 100)
        del source["controlled_access"]
        with self.assertRaisesRegex(AcquisitionBlocked, "access_controlled_or_unknown"):
            downloader.acquire(source)
        self.assertFalse(public_url("https://example.com/file"))
        self.assertFalse(public_url("https://user:pass@api.data.pankbase.org/file"))

    def test_stream_limit_removes_partial_and_does_not_import_prefix(self):
        source, _, _ = self.fixture("gene\nFICTION_A\n")
        downloader = Downloader(self.directory / "downloads", 5, 8)
        class Response(io.BytesIO):
            status = 200
            headers = {}
        class Opener:
            def open(self, *args, **kwargs):
                return Response(b"123456789")
        downloader.opener = Opener()
        with self.assertRaisesRegex(AcquisitionBlocked, "stream_size_exceeds_budget"):
            downloader.acquire(source)
        self.assertEqual(list(downloader.directory.iterdir()), [])
        self.assertEqual(downloader.total_bytes, 6)

    def test_md5_mismatch_prevents_source_promotion(self):
        source, _, _ = self.fixture("gene\nFICTION_A\n", md5sum="0" * 32)
        downloader = Downloader(self.directory / "downloads", 100, 100)
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Length": "3"}
        class Opener:
            def open(self, *args, **kwargs):
                return Response(b"abc")
        downloader.opener = Opener()
        with self.assertRaisesRegex(AcquisitionBlocked, "catalog_md5_mismatch"):
            downloader.acquire(source)
        self.assertEqual(list(downloader.directory.iterdir()), [])

    def test_http404_is_not_retried(self):
        source, _, _ = self.fixture("gene\nFICTION_A\n")
        downloader = Downloader(self.directory / "downloads", 100, 100)
        class Opener:
            calls = 0
            def open(self, *args, **kwargs):
                self.calls += 1
                raise urllib.error.HTTPError(source["file_url"], 404, "missing", {}, None)
        downloader.opener = Opener()
        with self.assertRaises(urllib.error.HTTPError):
            downloader.acquire(source)
        self.assertEqual(downloader.opener.calls, 1)

    def test_literal_change_error_and_missing_ensembl_fallback(self):
        source, acquired, analysis = self.fixture(
            "feature\tensembl_ID\tchange\terror\tp-value\nFICTION_A\t.\t-0.5\t0.2\t0.9\n")
        bundle = next(file_chunks(source, acquired, analysis, self.reference, "unit-test"))
        validate_bundle(bundle)
        record = bundle["records"][0]
        self.assertEqual(record["assertion_status"], "source_reported")
        self.assertEqual(record["metrics"]["source_change"], -0.5)
        self.assertNotIn("effect_estimate", record["metrics"])
        self.assertEqual(bundle["edges"][0]["type"], "HAS_ASSOCIATION_RESULT_WITH")
        self.assertEqual(record["raw_record"]["values"], ["FICTION_A", ".", "-0.5", "0.2", "0.9"])

    def test_disagreeing_row_stratum_never_supports_alias_cell(self):
        source, acquired, analysis = self.fixture("feature\tp-value\tstratum\nFICTION_A\t0.1\tAlpha\n")
        bundle = next(file_chunks(source, acquired, analysis, self.reference, "unit-test"))
        validate_bundle(bundle)
        self.assertFalse(bundle["edges"])
        record = bundle["records"][0]
        self.assertEqual(record["assertion_status"], "quarantined")
        self.assertIn("cell_type_stratum_mismatch", record["metadata"]["disposition_reasons"])

    def test_matrix_type_overrides_aggregate_modality_privacy(self):
        source, _, _ = self.fixture("gene\nFICTION_A\n", "04_clinical", **{"@type": ["MatrixFile", "File"], "content_type": "sparse gene count matrix"})
        self.assertEqual(source_entry(source)["metadata"]["privacy_classification"], "sensitive")

    def test_all_rows_counted_and_symbols_not_fabricated(self):
        source, acquired, analysis = self.fixture(
            "gene\tpvalue\nFICTION_A\t0.9\nFICTION_AMBIGUOUS\t0.1\n\nmissing\tNaN\n", "03_de_markers")
        chunks = list(file_chunks(source, acquired, analysis, self.reference, "unit-test", chunk_rows=2))
        self.assertEqual(sum(len(b["records"]) for b in chunks), 4)
        for bundle in chunks:
            validate_bundle(bundle)
        records = [r for b in chunks for r in b["records"]]
        self.assertEqual(sum(r["assertion_status"] == "quarantined" for r in records), 3)
        self.assertEqual(len({r["source_record_key"] for r in records}), 4)

    def test_abc_and_protected_rows_never_have_graph_links(self):
        for modality in ("08_abc", "01_metadata", "09_perifusion"):
            source, acquired, analysis = self.fixture("gene\tpvalue\nFICTION_A\t0.01\n", modality)
            bundle = next(file_chunks(source, acquired, analysis, self.reference, "unit-test"))
            validate_bundle(bundle)
            self.assertFalse(bundle["edges"])
            self.assertFalse(bundle["records"][0]["object_ids"])
            self.assertEqual(bundle["records"][0]["assertion_status"], "quarantined")
            if modality != "08_abc":
                self.assertEqual(bundle["sources"][0]["metadata"]["privacy_classification"], "sensitive")

    def test_bed_requires_declared_build_valid_interval(self):
        source, acquired, analysis = self.fixture("chr1\t0\t10\nchr1\t-1\t10\n", "07_atac",
                                                    file_format_type="bed3", assembly="GRCh38")
        bundle = next(file_chunks(source, acquired, analysis, self.reference, "unit-test"))
        validate_bundle(bundle)
        self.assertEqual(len(bundle["edges"]), 1)
        self.assertEqual(len(bundle["records"]), 2)
        del source["assembly"]
        bundle = next(file_chunks(source, acquired, analysis, self.reference, "unit-test"))
        self.assertFalse(bundle["edges"])

    def test_source_sha_change_rejected(self):
        source, acquired, analysis = self.fixture("gene\tpvalue\nFICTION_A\t0.1\n")
        Path(acquired["path"]).write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "source_sha256_changed"):
            list(file_chunks(source, acquired, analysis, self.reference, "unit-test"))

    def test_source_catalog_privacy_and_new_clinical_alias(self):
        row = {"@id": "/test/", "audit": {"WARNING": [{"category": "missing field", "detail": "PRIVATE", "path": "PRIVATE"}]}}
        self.assertNotIn("PRIVATE", json.dumps(sanitize(row)))
        self.assertEqual(classify_analysis({"aliases": ["example:pankbase2026_diffExpr_allTraits_Age_Beta"]}), "04_clinical")
        source, _, _ = self.fixture("gene\nFICTION_A\n")
        entry = source_entry(source)
        self.assertIsNone(entry["sha256"])
        self.assertTrue(entry["metadata"]["rows_unavailable"])

    def test_index_counts_all_catalog_sources_and_hashes_closed_chunks(self):
        source, acquired, analysis = self.fixture("gene\tpvalue\nFICTION_A\t0.1\n", "05_functional")
        other = {**source, "accession": "PKBFI_UNDOWNLOADED"}
        catalog = {"files": [source, other], "analyses": [analysis], "retrieved_utc": "unit-test"}
        manifest = {"snapshot_id": "unit-test", "files": {source["accession"]: acquired}}
        build_bundles(catalog, manifest, self.directory, self.reference_path, chunk_rows=1)
        index = json.loads((self.directory / "bundle_index.json").read_text())
        self.assertEqual(index["manifest"]["source_row_counts"], {"PKBFI_TEST": 1, "PKBFI_UNDOWNLOADED": 0})
        for entry in index["bundles"]:
            path = self.directory / entry["path"]
            self.assertEqual(digest(path), entry["sha256"])
            validate_bundle(json.loads(path.read_text()))

    def test_catalog_pagination_stability(self):
        client = CatalogClient()
        pages = iter([{"total": 2, "@graph": [{"@id": "/a/"}]}, {"total": 3, "@graph": [{"@id": "/b/"}]}])
        client.get_json = lambda url: next(pages)
        with self.assertRaisesRegex(ValueError, "catalog_changed"):
            client.search(type="File")

    def test_numeric_validation_no_nonfinite_or_invalid_probability(self):
        metrics, invalid = numeric_metrics({"change": "inf", "p-value": "1.01", "error": "-0.5", "baseMean": "-1"})
        self.assertEqual(metrics, {"source_error": -0.5})
        self.assertEqual(set(invalid), {"change", "p-value", "baseMean"})


if __name__ == "__main__":
    unittest.main()
