"""Public catalog discovery; never retain donor-linked audit details in manifests."""
from __future__ import annotations

import collections
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.data.pankbase.org"
USER_AGENT = "PanKgraph-0919-source-audit/0.1"
MODALITIES = {
    "01_metadata": "Donor/biosample metadata",
    "02_scrna": "scRNA expression/composition",
    "03_de_markers": "Differential expression/markers",
    "04_clinical": "Clinical expression associations",
    "05_functional": "Functional expression associations",
    "06_treatment": "Treatment response",
    "07_atac": "snATAC accessibility",
    "08_abc": "ABC predictions",
    "09_perifusion": "Perifusion measurements",
    "10_bulk": "Bulk RNA expression",
}
PILOT_FILES = (
    "PKBFI1271LIAH", "PKBFI7352MILU", "PKBFI9252NXOY",
    "PKBFI4780OSVU", "PKBFI9865TVEO", "PKBFI9683CCZE",
    "PKBFI5887OGWB", "PKBFI1363TEHQ", "PKBFI6865YLFO",
    "PKBFI0491GBGO", "PKBFI1772ZSFL", "PKBFI4372KNNK",
    "PKBFI7874FAIJ", "PKBFI8117XDTM", "PKBFI3163ZUPS",
    "PKBFI8305YDIV", "PKBFI6927FNYO", "PKBFI7107QVIN",
)
RESOURCE_MODALITIES = {
    "PKBDS5236MJJT": "01_metadata", "PKBDS1057RJYW": "01_metadata",
    "PKBDS2932PQVM": "01_metadata", "PKBDS9874MXRB": "09_perifusion",
    "PKBDS1349YHGQ": "02_scrna", "PKBDS8427QUCQ": "02_scrna",
    "PKBDS7658QIGY": "02_scrna", "PKBDS0470WCHR": "07_atac",
}


def utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def accession(value) -> str:
    if isinstance(value, dict):
        value = value.get("accession") or value.get("@id", "")
    return str(value or "").strip("/").split("/")[-1]


def audit_categories(item: dict) -> dict:
    return {level: dict(collections.Counter(a.get("category", "unknown") for a in rows))
            for level, rows in item.get("audit", {}).items() if isinstance(rows, list)}


def sanitize(item: dict, *, file: bool = False) -> dict:
    fields = ("@id", "@type", "accession", "uuid", "aliases", "status", "description",
              "file_set_type", "files", "file_set", "file_url", "file_format",
              "file_format_type", "content_type", "controlled_access", "file_size",
              "md5sum", "version", "schema_version", "assembly", "release_timestamp",
              "transcriptome_annotation", "derived_from", "analysis_step_version")
    result = {key: item[key] for key in fields if key in item}
    result["audit_categories"] = audit_categories(item)
    result["metadata_url"] = API + item.get("@id", "/") + "?format=json&frame=object"
    return result


def classify_analysis(item: dict) -> str:
    acc = item.get("accession", "")
    if acc in RESOURCE_MODALITIES:
        return RESOURCE_MODALITIES[acc]
    text = " ".join([item.get("description", ""), *item.get("aliases", [])]).lower()
    tests = (
        ("08_abc", ("from_abc", "predicted by abc", "cre-target gene")),
        ("05_functional", ("allfunctionaltraits",)),
        ("06_treatment", ("alltreatments",)),
        ("04_clinical", ("pa_de_phenotype", "allclinicaltraits", "diffexpr_alltraits_", "testing association with measured")),
        ("03_de_markers", ("pa_de_celltype", "ruvnormalizedpseudobulkcounts_de", "pseudobulk comparison between", "all other cells")),
        ("07_atac", ("peakcounts_snatacseq", "signaltrack_snatacseq", "peak calls and read counts", "atac")),
        ("02_scrna", ("geneexpcounts_scrnaseq", "gene expression counts for pancreatic")),
        ("10_bulk", ("bulkrna-seq_tpmmatrix", "bulk rna-seq tpm matrix")),
    )
    for modality, fragments in tests:
        if any(fragment in text for fragment in fragments):
            return modality
    return "unclassified"


class CatalogClient:
    def __init__(self, timeout: int = 90, max_response_bytes: int = 64 * 1024**2):
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def get_json(self, url: str) -> dict:
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise ValueError("catalog_response_byte_limit")
                return json.loads(raw)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
            except (TimeoutError, urllib.error.URLError):
                if attempt == 2:
                    raise
            time.sleep(1 + attempt)
        raise RuntimeError("unreachable")

    def search(self, **params) -> tuple[list[dict], dict]:
        params = {**params, "frame": "object", "format": "json", "limit": 300}
        rows, seen = [], set()
        expected = None
        offset = 0
        queries = []
        while expected is None or offset < expected:
            url = API + "/search/?" + urllib.parse.urlencode({**params, "from": offset})
            data = self.get_json(url)
            total = int(data["total"])
            if expected is not None and total != expected:
                raise ValueError("catalog_changed_during_pagination")
            expected = total
            page = data.get("@graph", [])
            queries.append(url)
            if not page and offset < expected:
                raise ValueError("catalog_incomplete_page")
            for row in page:
                key = row.get("@id") or row.get("accession")
                if key in seen:
                    raise ValueError("catalog_duplicate_pagination_identity")
                seen.add(key)
                rows.append(row)
            offset += len(page)
        if len(rows) != expected:
            raise ValueError("catalog_count_mismatch")
        return rows, {"total": expected, "queries": queries}


def refresh_catalog(client: CatalogClient) -> dict:
    analyses = []
    query_accounting = {}
    for kind in ("principal analysis", "resource analysis"):
        rows, info = client.search(type="AnalysisSet", file_set_type=kind)
        analyses.extend(sanitize(row) for row in rows)
        query_accounting[kind] = info
    files, query_accounting["released_files"] = client.search(type="File", status="released")
    clean_files = [sanitize(row, file=True) for row in files]
    analysis_by_id = {a["accession"]: a for a in analyses}
    for analysis in analyses:
        analysis["modality"] = classify_analysis(analysis)
    for source in clean_files:
        analysis = analysis_by_id.get(accession(source.get("file_set")), {})
        source["modality"] = analysis.get("modality", "catalog_only")
        source["analysis_status"] = analysis.get("status", "unknown")
    return {"retrieved_utc": utc_now(), "source": API, "complete": True,
            "analyses": analyses, "files": clean_files, "query_accounting": query_accounting}


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        handle.write("\n")
    temporary.chmod(0o600)
    temporary.replace(path)
