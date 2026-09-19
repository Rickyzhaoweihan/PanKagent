"""Live, non-destructive acceptance of the dedicated 0919 release.

Run as its owning service account with ``python -m deploy_0919.acceptance
--root /db/pankgraph0919``. No source rows or credentials enter the audit output.
Technical acceptance does not establish full scientific integration/readiness.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import subprocess
import time
from urllib.parse import urlsplit

import httpx
from cakg.multimodal import RELATION_PROFILES
from neo4j import GraphDatabase, Query, READ_ACCESS, WRITE_ACCESS
from neo4j.exceptions import Neo4jError
import psycopg
from psycopg.rows import dict_row

from .manage import NAME, PORTS, credentials, owned_root, private_json

MODALITIES = (
    "01_metadata", "02_scrna", "03_de_markers", "04_clinical", "05_functional",
    "06_treatment", "07_atac", "08_abc", "09_perifusion", "10_bulk",
)
PROTECTED_PORTS = (8794, 8795, 8796)
GENE_POINT_QUERY = "MATCH (g:BioEntity:Gene {id:$gene_id}) RETURN g"
GENE_RELATIONSHIP_TYPES = frozenset(name for name, (source, _) in RELATION_PROFILES.items() if source == "Gene")


class GateFailure(Exception):
    """Only constant, non-sensitive reason codes are permitted here."""
    def __init__(self, code: str, details: dict | None = None):
        super().__init__(code)
        self.details = details


def require(condition: bool, code: str) -> None:
    if not condition:
        raise GateFailure(code)


def gene_edge_point_query(relationship_type: str) -> str:
    """Use indexed labels/types; no unchecked source text becomes Cypher syntax."""
    require(isinstance(relationship_type, str) and relationship_type in GENE_RELATIONSHIP_TYPES,
            "unsupported_gene_point_lookup_relationship_type")
    return ("MATCH (g:BioEntity:Gene {id:$gene_id})"
            f"-[r:{relationship_type} {{id:$edge_id}}]->(c:BioEntity) RETURN g,r,c")


def checked_base_url(value: str) -> str:
    parsed = urlsplit(value)
    require(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
            and parsed.port == PORTS["api"] and not parsed.username and not parsed.password
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
            "base_url_must_be_the_owned_loopback_api")
    return value.rstrip("/")


def listener_pids(lines: list[str]) -> dict[str, list[int]]:
    result = {str(port): set() for port in PROTECTED_PORTS}
    for line in lines:
        fields = line.split()
        if len(fields) < 4:
            continue
        port = fields[3].rsplit(":", 1)[-1]
        if port in result:
            result[port].update(int(pid) for pid in re.findall(r"\bpid=(\d+)\b", line))
    return {port: sorted(pids) for port, pids in result.items()}


class Audit:
    def __init__(self):
        self.results: list[dict] = []

    def check(self, name: str, callback) -> None:
        started = time.monotonic()
        try:
            details = callback()
            item = {"check": name, "status": "passed", "details": details or {}}
        except Exception as exc:
            # Never stringify a driver/HTTP exception: it may carry a DSN,
            # request body, full query, protected value, or response content.
            item = {"check": name, "status": "failed", "error_type": type(exc).__name__}
            if isinstance(exc, GateFailure):
                item["reason_code"] = exc.args[0]
                if exc.details is not None:
                    item["details"] = exc.details
            elif isinstance(exc, (psycopg.Error, Neo4jError)):
                code = getattr(exc, "sqlstate", None) or getattr(exc, "code", None)
                if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.]+", code):
                    item["database_error_code"] = code
        item["elapsed_seconds"] = round(time.monotonic() - started, 3)
        self.results.append(item)
        print(json.dumps({"check": name, "status": item["status"]}), flush=True)


class Acceptance:
    def __init__(self, root: Path, base_url: str, pg, graph, api, secret: dict, baseline: Path):
        self.root, self.pg, self.graph, self.api = root, pg, graph, api
        self.base_url, self.secret, self.baseline = base_url, secret, baseline
        self.snapshot = None
        self.manifest: dict = {}
        self.counts: dict = {}
        self.sample: dict = {}
        self.source_rows: list[dict] = []

    def rows(self, sql: str, parameters=()) -> list[dict]:
        return self.pg.execute(sql, parameters).fetchall()

    def one(self, sql: str, parameters=()) -> dict:
        rows = self.rows(sql, parameters)
        require(len(rows) == 1, "expected_one_sql_result")
        return rows[0]

    def request(self, method: str, path: str, *, body=None, params=None, status=200, auth=True) -> dict:
        headers = {"Authorization": "Bearer " + self.secret["api_token"]} if auth else {}
        with self.api.stream(method, self.base_url + path, json=body, params=params, headers=headers) as response:
            require(response.status_code == status, "unexpected_http_status_" + str(response.status_code))
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                require(len(raw) <= 2_000_000, "api_response_exceeded_byte_limit")
        value = json.loads(raw)
        require(isinstance(value, dict), "api_response_is_not_an_object")
        return value

    def snapshot_counts(self) -> dict:
        ident = self.one("SELECT current_database() AS database, current_user AS role, "
                         "current_setting('transaction_read_only') AS read_only")
        require(ident == {"database": NAME, "role": "serviceuser", "read_only": "on"}, "owner_read_connection_identity_mismatch")
        snapshots = self.rows("SELECT snapshot_id,status,manifest FROM cakg_mm.snapshot")
        require(len(snapshots) == 1 and snapshots[0]["status"] == "accepted", "one_accepted_snapshot_required")
        self.snapshot, self.manifest = snapshots[0]["snapshot_id"], snapshots[0]["manifest"]
        actual = self.one("""SELECT
          (SELECT count(*) FROM cakg_mm.graph_object WHERE object_kind='node') AS nodes,
          (SELECT count(*) FROM cakg_mm.graph_object WHERE object_kind='edge') AS edges,
          (SELECT count(*) FROM cakg_mm.context) AS contexts,
          (SELECT count(*) FROM cakg_mm.evidence_record) AS records,
          (SELECT count(*) FROM cakg_mm.source_file) AS sources,
          (SELECT count(*) FROM cakg_mm.object_evidence WHERE snapshot_id=%s) AS evidence_links,
          (SELECT count(*) FROM cakg_mm.evidence_record WHERE assertion_status='quarantined') AS quarantined_records,
          (SELECT COALESCE(sum(count),0) FROM cakg_mm.source_rejection WHERE snapshot_id=%s) AS rejected_rows""",
                          (self.snapshot, self.snapshot))
        self.counts = {key: int(value) for key, value in actual.items()}
        require(self.counts == self.manifest.get("counts"), "manifest_storage_count_mismatch")
        linked = self.one("""SELECT
          (SELECT count(*) FROM cakg_mm.snapshot_object WHERE snapshot_id=%s) AS objects,
          (SELECT count(*) FROM cakg_mm.snapshot_record WHERE snapshot_id=%s) AS records""",
                          (self.snapshot, self.snapshot))
        require(linked == {"objects": self.counts["nodes"] + self.counts["edges"], "records": self.counts["records"]},
                "snapshot_registry_membership_count_mismatch")
        return {"database": NAME, "snapshot_id": self.snapshot, "status": "accepted", "counts": self.counts}

    def graph_counts(self) -> dict:
        require(bool(self.counts), "postgres_counts_not_verified")
        with self.graph.session(database=NAME, default_access_mode=READ_ACCESS) as session:
            result = session.run(Query("MATCH (n) RETURN n.snapshot_id AS snapshot_id, count(n) AS count", timeout=60))
            nodes = list(result)
            require(result.consume().database == NAME, "actual_graph_database_mismatch")
            edges = list(session.run(Query(
                "MATCH ()-[r]->() RETURN r.snapshot_id AS snapshot_id, count(r) AS count", timeout=60)))
            require({r["snapshot_id"] for r in nodes} == {self.snapshot}, "graph_node_snapshot_mismatch")
            require(not edges or {r["snapshot_id"] for r in edges} == {self.snapshot}, "graph_edge_snapshot_mismatch")
            counts = {"nodes": sum(r["count"] for r in nodes), "edges": sum(r["count"] for r in edges)}
            require(counts == {k: self.counts[k] for k in counts}, "graph_registry_count_mismatch")
            malformed = session.run(Query("""MATCH (a)-[r]->(b)
                WHERE r.start_id IS NULL OR r.end_id IS NULL OR a.id IS NULL OR b.id IS NULL
                OR r.start_id<>a.id OR r.end_id<>b.id OR a.snapshot_id<>r.snapshot_id OR b.snapshot_id<>r.snapshot_id
                RETURN count(r) AS count""", timeout=60)).single()["count"]
            require(malformed == 0, "graph_relationship_endpoint_mismatch")
        return {**counts, "snapshot_id": self.snapshot, "invalid_endpoint_mirrors": 0}

    def evidence_eligibility(self) -> dict:
        invalid = self.one("""SELECT count(*) AS count FROM cakg_mm.object_evidence oe
            JOIN cakg_mm.graph_object g USING(object_id)
            JOIN cakg_mm.evidence_record r USING(record_id)
            JOIN cakg_mm.source_file f ON f.file_id=r.source_file_id
            WHERE oe.snapshot_id=%s AND g.object_kind='edge'
              AND (r.assertion_status='quarantined' OR f.status<>'released' OR f.sha256 IS NULL)""", (self.snapshot,))["count"]
        unsupported = self.one("""SELECT count(*) AS count FROM cakg_mm.graph_object g
            JOIN cakg_mm.snapshot_object so USING(object_id)
            WHERE so.snapshot_id=%s AND g.object_kind='edge' AND NOT EXISTS
             (SELECT 1 FROM cakg_mm.object_evidence oe WHERE oe.object_id=g.object_id AND oe.snapshot_id=so.snapshot_id)""",
                               (self.snapshot,))["count"]
        require(invalid == 0 and unsupported == 0, "graph_edge_evidence_is_ineligible_or_missing")
        return {"ineligible_edge_evidence_links": invalid, "unsupported_edges": unsupported,
                "meaning": "source_result_membership_only"}

    def sources_and_modalities(self) -> dict:
        self.source_rows = self.rows("SELECT file_id,sha256,status,metadata FROM cakg_mm.source_file ORDER BY file_id")
        per_source = self.rows("""SELECT f.file_id,
            COALESCE(r.n,0) + COALESCE(x.n,0) AS accounted_rows FROM cakg_mm.source_file f
            LEFT JOIN (SELECT source_file_id,count(*) AS n FROM cakg_mm.evidence_record GROUP BY source_file_id) r
              ON r.source_file_id=f.file_id
            LEFT JOIN (SELECT file_id,sum(count) AS n FROM cakg_mm.source_rejection WHERE snapshot_id=%s GROUP BY file_id) x
              ON x.file_id=f.file_id""", (self.snapshot,))
        declared = self.manifest.get("source_row_counts")
        require(isinstance(declared, dict), "source_row_accounting_manifest_missing")
        require({r["file_id"]: int(r["accounted_rows"]) for r in per_source} == declared, "source_rows_do_not_reconcile")
        source_statuses, groups = {}, {}
        for source in self.source_rows:
            source_statuses[source["status"]] = source_statuses.get(source["status"], 0) + 1
            require(source["sha256"] is not None or (source["status"] == "unverified" and
                    source["metadata"].get("acquisition_status") != "downloaded"), "unverified_source_checksum_misrepresented")
            key = source["metadata"].get("modality", "catalog_only")
            group = groups.setdefault(key, {"source_files": 0, "downloaded_files": 0, "records": 0,
                                           "projected_records": 0, "quarantined_records": 0, "edge_supported_records": 0})
            group["source_files"] += 1
            group["downloaded_files"] += source["metadata"].get("acquisition_status") == "downloaded"
        records = self.rows("""SELECT f.metadata->>'modality' AS modality, r.assertion_status, count(*) AS count
            FROM cakg_mm.evidence_record r JOIN cakg_mm.source_file f ON f.file_id=r.source_file_id
            GROUP BY f.metadata->>'modality', r.assertion_status""")
        for row in records:
            group = groups[row["modality"]]
            group["records"] += row["count"]
            group["quarantined_records" if row["assertion_status"] == "quarantined" else "projected_records"] += row["count"]
        supported = self.rows("""SELECT f.metadata->>'modality' AS modality, count(DISTINCT r.record_id) AS count
            FROM cakg_mm.object_evidence oe JOIN cakg_mm.graph_object g USING(object_id)
            JOIN cakg_mm.evidence_record r USING(record_id) JOIN cakg_mm.source_file f ON f.file_id=r.source_file_id
            WHERE oe.snapshot_id=%s AND g.object_kind='edge' GROUP BY f.metadata->>'modality'""", (self.snapshot,))
        for row in supported:
            groups[row["modality"]]["edge_supported_records"] = row["count"]
        manifest_modalities = self.manifest.get("modalities", {})
        require(all(key in manifest_modalities for key in MODALITIES), "all_ten_modality_statuses_required")
        report = {}
        for key in MODALITIES:
            actual = groups.get(key, {"source_files": 0, "downloaded_files": 0, "records": 0,
                                      "projected_records": 0, "quarantined_records": 0, "edge_supported_records": 0})
            declared_group = manifest_modalities[key]
            require(all(actual[k] == declared_group.get(k, 0) for k in
                        ("downloaded_files", "records", "projected_records", "quarantined_records")),
                    "modality_manifest_count_mismatch")
            incomplete = len(declared_group.get("incomplete_files", []))
            status = ("partially_projected" if actual["edge_supported_records"] and (actual["quarantined_records"] or incomplete)
                      else "source_results_projected" if actual["edge_supported_records"]
                      else "staged_in_postgres" if actual["records"]
                      else "downloaded_without_imported_records" if actual["downloaded_files"]
                      else "catalog_only" if actual["source_files"] else "not_available")
            report[key] = {**actual, "incomplete_files": incomplete, "status": status,
                           "scientific_readiness": "not_established"}
        return {"source_files": len(self.source_rows), "source_status_counts": source_statuses,
                "all_source_rows_reconciled": True, "modalities": report,
                "other_source_modalities": sorted(set(groups) - set(MODALITIES))}

    def graph_read_only(self) -> dict:
        with self.graph.session(database="system", default_access_mode=READ_ACCESS) as session:
            databases = list(session.run(Query("SHOW DATABASES YIELD name, access, currentStatus "
                                               "WHERE name=$name RETURN name,access,currentStatus", timeout=60), name=NAME))
            require(len(databases) == 1 and databases[0]["access"] == "read-only" and
                    databases[0]["currentStatus"] == "online", "live_graph_database_is_not_read_only")
        denied_code = None
        with self.graph.session(database=NAME, default_access_mode=WRITE_ACCESS) as session:
            transaction = session.begin_transaction(timeout=10)
            try:
                transaction.run("CREATE (:PanKgraph0919AcceptanceSentinel {nonce:$nonce}) RETURN 1 AS value",
                                nonce=secrets.token_hex(16)).consume()
            except Neo4jError as exc:
                denied_code = exc.code
            finally:
                # Always roll back, including an unexpectedly accepted CREATE.
                # No sentinel is ever committed by this runner.
                transaction.rollback()
        require(isinstance(denied_code, str) and ("readonly" in denied_code.lower() or
                denied_code == "Neo.ClientError.Security.Forbidden"), "graph_create_was_not_rejected_as_read_only")
        return {"database": NAME, "live_access": "read-only", "create_rejected": True,
                "denial_code": denied_code, "sentinel_committed": False}

    def postgres_reader_denial(self) -> dict:
        with psycopg.connect(host="127.0.0.1", port=PORTS["postgres"], dbname=NAME,
                             user=NAME + "_reader", password=self.secret["reader_password"],
                             connect_timeout=5, autocommit=True, row_factory=dict_row,
                             options="-c statement_timeout=60000") as reader:
            identity = reader.execute("SELECT current_database() AS database,current_user AS role, "
                                      "current_setting('transaction_read_only') AS read_only").fetchone()
            require(identity == {"database": NAME, "role": NAME + "_reader", "read_only": "on"}, "reader_identity_or_default_mismatch")
            permissions = reader.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                                         "FROM pg_roles WHERE rolname=current_user").fetchone()
            require(not any(permissions.values()), "reader_has_privileged_role_attributes")
            denied_code = None
            try:
                # Override only the connection's default transaction mode to
                # verify actual grants. WHERE false can change no source row.
                reader.execute("BEGIN READ WRITE")
                reader.execute("UPDATE cakg_mm.snapshot SET status=status WHERE false")
            except psycopg.Error as exc:
                denied_code = exc.sqlstate
            finally:
                reader.execute("ROLLBACK")
            require(denied_code in {"42501", "25006"}, "reader_dml_not_denied")
        return {"database": NAME, "reader_role": NAME + "_reader", "default_read_only": True,
                "dml_rejected": True, "sqlstate": denied_code, "rows_changed": 0}

    def api_boundary(self) -> dict:
        health = self.request("GET", "/health", auth=False)
        require(health == {"status": "ready"}, "unauthenticated_health_exposes_details_or_is_unready")
        detail = self.request("GET", "/health", params={"detail": "true"})
        require(detail.get("snapshot_id") == self.snapshot and detail.get("status") == "ready", "api_snapshot_health_mismatch")
        self.request("GET", "/health", params={"detail": "true"}, status=401, auth=False)
        self.request("GET", "/sources", status=401, auth=False)
        self.request("POST", "/query", body={"cypher": "CREATE (n) RETURN n"}, status=422)
        self.request("POST", "/query", body={"cypher": "RETURN 1", "snapshot_id": self.snapshot + ":mismatch"}, status=409)
        self.request("POST", "/query", body={"cypher": "RETURN 1", "limit": 201}, status=422)
        self.request("GET", "/contexts", params={"limit": 201}, status=422)
        self.request("POST", "/query", body={"sql": "SELECT 1"}, status=422)
        injection = self.request("POST", "/genes/search", body={"query": "never_match_0919%' OR '1'='1' --", "limit": 2})
        require(injection.get("total") == 0 and injection.get("items") == [], "sql_injection_text_was_not_literal")
        return {"unauthenticated_data_status": 401, "write_cypher_status": 422, "snapshot_mismatch_status": 409,
                "oversized_limit_status": 422, "raw_sql_status": 422, "sql_injection_literal_matches": 0}

    def select_sample(self) -> None:
        sql = """SELECT g.object_id AS gene_id, g.properties->>'name' AS gene_name,
            g.properties->>'canonical_id' AS canonical_id, e.object_id AS edge_id, e.end_id,
            e.relationship_type FROM cakg_mm.graph_object g
            JOIN cakg_mm.graph_object e ON e.start_id=g.object_id AND e.object_kind='edge'
            JOIN cakg_mm.snapshot_object so ON so.object_id=e.object_id
            WHERE g.entity_type='Gene' AND so.snapshot_id=%s AND EXISTS
             (SELECT 1 FROM cakg_mm.object_evidence oe JOIN cakg_mm.evidence_record er USING(record_id)
              JOIN cakg_mm.source_file sf ON sf.file_id=er.source_file_id
              WHERE oe.object_id=e.object_id AND oe.snapshot_id=so.snapshot_id
                AND er.assertion_status<>'quarantined' AND sf.status='released'
                AND sf.metadata->>'privacy_classification'='public_aggregate')"""
        # Try the exact public reference symbol first, without sorting or
        # resolving evidence for every edge in a multimillion-row release.
        samples = self.rows(sql + " AND g.properties->>'name'='INS' LIMIT 1", (self.snapshot,))
        if not samples:
            samples = self.rows(sql + " LIMIT 1", (self.snapshot,))
        require(bool(samples), "no_real_eligible_gene_edge_available")
        self.sample = samples[0]

    def compare_records(self, items: list[dict], *, object_id=None) -> None:
        require(bool(items), "expected_source_evidence_page_is_empty")
        for item in items:
            expected = self.one("""SELECT er.context_id,er.collection_id,er.source_file_id,er.source_record_key,
                er.assertion_status,er.metrics,er.raw_record,sf.url AS source_url,sf.sha256,sf.status AS source_status
                FROM cakg_mm.evidence_record er JOIN cakg_mm.source_file sf ON sf.file_id=er.source_file_id
                JOIN cakg_mm.snapshot_record sr USING(record_id)
                WHERE er.record_id=%s AND sr.snapshot_id=%s""", (item["record_id"], self.snapshot))
            require(all(item.get(key) == value for key, value in expected.items()), "api_record_source_value_or_provenance_mismatch")
            if object_id:
                linked = self.one("SELECT count(*) AS count FROM cakg_mm.object_evidence WHERE "
                                  "snapshot_id=%s AND object_id=%s AND record_id=%s",
                                  (self.snapshot, object_id, item["record_id"]))["count"]
                require(linked == 1, "api_expanded_record_is_not_linked_to_selected_object")

    def api_graph_evidence(self) -> dict:
        self.select_sample()
        cypher = gene_edge_point_query(self.sample["relationship_type"])
        parameters = {"gene_id": self.sample["gene_id"], "edge_id": self.sample["edge_id"]}
        gene = self.request("POST", "/query", body={"cypher": GENE_POINT_QUERY,
                                                    "parameters": {"gene_id": self.sample["gene_id"]}, "limit": 1})
        require(gene.get("object_ids") == [self.sample["gene_id"]] and len(gene.get("graph_rows", [])) == 1,
                "genuine_gene_brief_identity_mismatch")
        gene_detail = self.request("POST", "/query", body={"cypher": GENE_POINT_QUERY,
                                   "parameters": {"gene_id": self.sample["gene_id"]}, "mode": "detail", "limit": 1})
        require(gene_detail.get("expansion_policy") == "returned_nodes" and
                gene_detail["evidence"].get("object_ids") == [self.sample["gene_id"]], "gene_detail_expansion_mismatch")
        self.compare_records(gene_detail["evidence"]["items"], object_id=self.sample["gene_id"])
        brief = self.request("POST", "/query", body={"cypher": cypher, "parameters": parameters, "limit": 2})
        require(set(brief.get("object_ids", [])) == {self.sample["gene_id"], self.sample["edge_id"], self.sample["end_id"]},
                "genuine_graph_object_ids_mismatch")
        require("evidence" not in brief and brief.get("graph_snapshot_ids") == [self.snapshot], "brief_contract_mismatch")
        detail = self.request("POST", "/query", body={"cypher": cypher, "parameters": parameters, "mode": "detail", "limit": 2})
        require(detail.get("expansion_policy") == "returned_edges", "detail_did_not_prefer_edges")
        evidence = detail["evidence"]
        require(evidence.get("object_ids") == [self.sample["edge_id"]], "detail_expanded_unrequested_objects")
        self.compare_records(evidence["items"], object_id=self.sample["edge_id"])
        count = self.one("SELECT count(*) AS count FROM cakg_mm.object_evidence WHERE snapshot_id=%s AND object_id=%s",
                         (self.snapshot, self.sample["edge_id"]))["count"]
        require(evidence["total"] == evidence["unfiltered_total"] == count, "edge_source_record_denominator_mismatch")
        self.sample["record"] = evidence["items"][0]
        spoof = self.request("POST", "/query", body={"cypher": "RETURN $projected AS projected",
                             "parameters": {"projected": {"id": self.sample["edge_id"], "snapshot_id": self.snapshot}}, "mode": "detail"})
        require(spoof.get("object_ids") == [] and spoof["evidence"]["match_status"] == "no_graph_objects",
                "scalar_identity_projection_triggered_expansion")
        return {key: self.sample[key] for key in ("gene_id", "gene_name", "canonical_id", "edge_id", "relationship_type")} | {
            "edge_source_records": count, "source_records_compared": len(evidence["items"]),
            "gene_detail_source_records_compared": len(gene_detail["evidence"]["items"]),
            "source_values_and_provenance_match": True, "scalar_id_spoof_expansion": False}

    def api_context_and_pagination(self) -> dict:
        require("record" in self.sample, "real_graph_sample_not_verified")
        record = self.sample["record"]
        context = self.one("SELECT context_id,collection_id,context_kind,condition,cell_type_id "
                           "FROM cakg_mm.context WHERE context_id=%s", (record["context_id"],))
        params = {k: v for k, v in context.items() if v is not None}
        params.update(record_status=record["assertion_status"], limit=1, offset=0)
        contexts = self.request("GET", "/contexts", params=params)
        require(contexts["total"] == 1 and len(contexts["items"]) == 1 and
                contexts["items"][0]["context_id"] == context["context_id"], "context_filter_result_mismatch")
        empty = self.request("GET", "/contexts", params={**params, "offset": 1})
        require(empty["items"] == [] and empty["total"] == 1 and not empty["has_more"], "context_offset_boundary_mismatch")
        selected = self.request("GET", "/objects/" + self.sample["edge_id"] + "/records", params=params)
        self.compare_records(selected["items"], object_id=self.sample["edge_id"])
        require(all(item["context_id"] == context["context_id"] and item["assertion_status"] == record["assertion_status"]
                    for item in selected["items"]), "object_record_filters_not_applied")
        source = record["source_file_id"]
        expected = self.rows("SELECT record_id FROM cakg_mm.evidence_record WHERE source_file_id=%s ORDER BY record_id LIMIT 2", (source,))
        require(len(expected) == 2, "real_source_lacks_two_records_for_pagination")
        pages = [self.request("POST", "/records/search", body={"source_file_id": source, "limit": 1, "offset": offset})
                 for offset in (0, 1)]
        require([p["items"][0]["record_id"] for p in pages] == [r["record_id"] for r in expected], "stable_record_pagination_mismatch")
        count = self.one("SELECT count(*) AS count FROM cakg_mm.evidence_record WHERE source_file_id=%s", (source,))["count"]
        require(all(p["total"] == p["unfiltered_total"] == count for p in pages), "source_page_denominator_mismatch")
        require(pages[0]["next_offset"] == 1 and pages[0]["has_more"], "source_page_continuation_mismatch")
        return {"exact_context_filter": True, "empty_context_offset_verified": True,
                "stable_two_page_records": True, "source_file_id": source, "full_source_records": count}

    def api_postgres_and_quarantine(self) -> dict:
        require(bool(self.sample), "real_gene_sample_not_verified")
        term = self.sample["canonical_id"] or self.sample["gene_name"]
        searched = self.request("POST", "/genes/search", body={"query": term, "limit": 100})
        require(self.sample["gene_id"] in {r["object_id"] for r in searched["items"]}, "postgres_gene_search_missing_real_gene")
        routed = self.request("POST", "/query", body={"postgres_search": {"query": term, "limit": 100}, "mode": "brief"})
        require(routed.get("query_source") == "postgres_search" and
                self.sample["gene_id"] in {r["object_id"] for r in routed["genes"]["items"]}, "postgres_query_route_mismatch")
        abc = self.rows("""SELECT er.source_file_id FROM cakg_mm.evidence_record er
            JOIN cakg_mm.source_file sf ON sf.file_id=er.source_file_id
            WHERE sf.metadata->>'modality'='08_abc' AND er.assertion_status='quarantined' LIMIT 1""")
        require(bool(abc), "quarantined_abc_source_not_available_for_live_check")
        result = self.request("POST", "/records/search", body={"source_file_id": abc[0]["source_file_id"],
                              "record_status": "quarantined", "limit": 2})
        self.compare_records(result["items"])
        require(all(r["assertion_status"] == "quarantined" for r in result["items"]) and result["object_ids"] == [],
                "quarantined_record_status_or_object_claim_mismatch")
        expected = self.one("SELECT count(*) AS count FROM cakg_mm.evidence_record WHERE source_file_id=%s "
                            "AND assertion_status='quarantined'", (abc[0]["source_file_id"],))["count"]
        require(result["total"] == expected, "quarantined_source_record_count_mismatch")
        return {"postgres_gene_and_structured_query_routes": "passed", "independent_record_search": "passed",
                "abc_source_file_id": abc[0]["source_file_id"], "quarantined_abc_records": expected,
                "graph_outage_fault_injection": "not_performed; independence covered by injected-driver unit tests"}

    def api_sensitive_redaction(self) -> dict:
        sources = self.rows("""SELECT sf.file_id FROM cakg_mm.source_file sf
            WHERE sf.metadata->>'privacy_classification'='sensitive'
            AND EXISTS(SELECT 1 FROM cakg_mm.evidence_record er WHERE er.source_file_id=sf.file_id)
            ORDER BY sf.file_id LIMIT 1""")
        require(bool(sources), "sensitive_source_not_available_for_live_redaction_check")
        result = self.request("POST", "/records/search", body={"source_file_id": sources[0]["file_id"], "limit": 2})
        require(bool(result["items"]), "sensitive_source_page_is_empty")
        forbidden = {"raw_record", "metrics", "metadata", "context_metadata", "source_metadata"}
        require(all(item.get("details_redacted") is True and not (forbidden & set(item)) for item in result["items"]),
                "sensitive_source_details_not_redacted")
        expected = self.one("SELECT count(*) AS count FROM cakg_mm.evidence_record WHERE source_file_id=%s", (sources[0]["file_id"],))["count"]
        require(result["total"] == result["unfiltered_total"] == expected, "redaction_changed_source_denominator")
        return {"sensitive_source_file_id": sources[0]["file_id"], "details_redacted": True,
                "full_source_records": expected, "raw_values_recorded_in_audit": False}

    def api_regulatory_region_overlap(self) -> dict:
        candidates = self.rows("""SELECT go.object_id,go.properties,ei.genome_assembly,ei.chr,ei.start_loc,ei.end_loc
            FROM cakg_mm.entity_interval ei JOIN cakg_mm.graph_object go USING(object_id)
            JOIN cakg_mm.snapshot_object so USING(object_id)
            WHERE so.snapshot_id=%s AND go.entity_type='Regulatory_region'
            ORDER BY go.object_id,ei.genome_assembly,ei.chr,ei.start_loc,ei.end_loc LIMIT 1""", (self.snapshot,))
        require(bool(candidates), "real_regulatory_region_interval_unavailable")
        expected = candidates[0]
        body = {"entity_type": "Regulatory_region", "assembly": expected["genome_assembly"],
                "chromosome": expected["chr"], "start": expected["start_loc"], "end": expected["end_loc"], "limit": 2}
        self.request("POST", "/entities/search", body=body, status=401, auth=False)
        self.request("POST", "/entities/search", body={**body, "entity_type": "Gene' OR true --"}, status=422)
        self.request("POST", "/entities/search", body={**body, "limit": 101}, status=422)
        result = self.request("POST", "/entities/search", body=body)
        require(bool(result["items"]) and result["items"][0]["object_id"] == expected["object_id"],
                "regional_overlap_did_not_return_exact_source_region")
        item = result["items"][0]
        interval = {k: expected[k] for k in ("genome_assembly", "chr", "start_loc", "end_loc")}
        require(interval in item.get("intervals", []) and item.get("properties") == expected["properties"],
                "region_properties_or_interval_values_mismatch")
        require(result["interval_coverage"].get("available") is True and
                result["interval_coverage"].get("coordinate_convention") == "zero_based_half_open", "region_interval_coverage_mismatch")
        evidence = self.request("GET", "/objects/" + expected["object_id"] + "/records", params={"limit": 2})
        self.compare_records(evidence["items"], object_id=expected["object_id"])
        gene_intervals = self.one("""SELECT count(*) AS count FROM cakg_mm.entity_interval ei
            JOIN cakg_mm.graph_object go USING(object_id) JOIN cakg_mm.snapshot_object so USING(object_id)
            WHERE so.snapshot_id=%s AND go.entity_type='Gene'""", (self.snapshot,))["count"]
        require(gene_intervals == 0, "unexpected_gene_intervals_require_coordinate_provenance_review")
        genes = self.request("POST", "/entities/search", body={**body, "entity_type": "Gene"})
        require(genes["items"] == [] and genes["total"] == 0 and genes["interval_coverage"].get("available") is False,
                "absent_gene_interval_coverage_not_explicit")
        return {"regulatory_region_id": expected["object_id"], "assembly": expected["genome_assembly"],
                "coordinate_convention": "zero_based_half_open", "interval_values_match_postgres": True,
                "source_records_compared": len(evidence["items"]), "gene_interval_rows": gene_intervals,
                "gene_interval_coverage": "not_available", "empty_gene_overlap_is_biological_absence": False}

    def api_source_coverage(self) -> dict:
        require(bool(self.source_rows), "source_inventory_not_verified")
        actual, offset = {}, 0
        while True:
            page = self.request("GET", "/sources", params={"limit": 200, "offset": offset})
            require(page["total"] == len(self.source_rows), "api_source_inventory_count_mismatch")
            for item in page["items"]:
                require(item["file_id"] not in actual, "api_source_pagination_duplicates")
                actual[item["file_id"]] = item
            if not page["has_more"]:
                break
            require(page["next_offset"] == offset + len(page["items"]) and len(page["items"]) > 0,
                    "api_source_pagination_does_not_advance")
            offset = page["next_offset"]
            require(offset <= len(self.source_rows), "api_source_pagination_exceeds_inventory")
        require(set(actual) == {row["file_id"] for row in self.source_rows}, "api_source_inventory_ids_mismatch")
        record_counts = self.rows("SELECT source_file_id,assertion_status,count(*) AS count FROM cakg_mm.evidence_record "
                                  "GROUP BY source_file_id,assertion_status")
        rejected = self.rows("SELECT file_id,reason_code,count FROM cakg_mm.source_rejection WHERE snapshot_id=%s", (self.snapshot,))
        for expected in self.source_rows:
            item = actual[expected["file_id"]]
            require(item.get("status") == expected["status"] and item.get("sha256") == expected["sha256"], "api_source_status_or_checksum_mismatch")
            counts = {r["assertion_status"]: r["count"] for r in record_counts if r["source_file_id"] == expected["file_id"]}
            require(item.get("record_counts") == counts, "api_source_record_counts_mismatch")
            wanted = {r["reason_code"]: r["count"] for r in rejected if r["file_id"] == expected["file_id"]}
            require({r["reason_code"]: r["count"] for r in item["rejections"]} == wanted, "api_source_rejection_counts_mismatch")
        return {"all_source_pages_verified": True, "source_files": len(actual),
                "record_status_counts_and_rejections_match_postgres": True}

    def protected_listeners(self) -> dict:
        baseline = json.loads(self.baseline.read_text())
        require(baseline.get("phase") == "before-import", "listener_baseline_is_not_pre_import")
        before = listener_pids(baseline["listeners"])
        require(all(before[str(port)] for port in PROTECTED_PORTS), "protected_listener_baseline_pid_missing")
        result = subprocess.run(["ss", "-ltnp"], check=True, capture_output=True, text=True, timeout=10)
        after = listener_pids(result.stdout.splitlines())
        if after != before:
            # This is a temporal observation, not an attribution of cause.
            raise GateFailure("protected_listener_changed_since_baseline_attribution_not_established",
                              {"before_pids": before, "after_pids": after,
                               "causal_attribution": "not_established"})
        return {"protected_ports": list(PROTECTED_PORTS), "before_pids": before, "after_pids": after,
                "pid_identity_unchanged": True, "causal_attribution": "not_inferred_from_pid_comparison"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18919")
    parser.add_argument("--listener-baseline", type=Path)
    args = parser.parse_args()
    owned_root(args.root)
    base_url = checked_base_url(args.base_url)
    secret = credentials(args.root)
    baseline = args.listener_baseline or args.root / "audit" / "runtime-before-import.json"
    audit = Audit()
    started = datetime.now(timezone.utc).isoformat()
    snapshot = None
    try:
        with ExitStack() as stack:
            pg = stack.enter_context(psycopg.connect(
                host=str(args.root / "socket"), port=PORTS["postgres"], dbname=NAME, user="serviceuser",
                autocommit=True, row_factory=dict_row, connect_timeout=5,
                options="-c default_transaction_read_only=on -c statement_timeout=60000"))
            graph = stack.enter_context(GraphDatabase.driver(
                f"bolt://127.0.0.1:{PORTS['bolt']}", auth=("neo4j", secret["neo4j_password"]),
                connection_timeout=5, connection_acquisition_timeout=10, max_connection_pool_size=2))
            api = stack.enter_context(httpx.Client(timeout=httpx.Timeout(75, connect=5), follow_redirects=False, trust_env=False))
            checks = Acceptance(args.root, base_url, pg, graph, api, secret, baseline)
            for name, callback in (
                    ("postgres_snapshot_manifest_counts", checks.snapshot_counts),
                    ("neo4j_snapshot_counts_and_endpoints", checks.graph_counts),
                    ("edge_evidence_eligibility", checks.evidence_eligibility),
                    ("source_rows_and_all_ten_modalities", checks.sources_and_modalities),
                    ("neo4j_live_read_only_and_create_denial", checks.graph_read_only),
                    ("postgres_reader_dml_denial", checks.postgres_reader_denial),
                    ("api_auth_query_and_input_boundaries", checks.api_boundary),
                    ("api_genuine_graph_and_source_evidence", checks.api_graph_evidence),
                    ("api_context_filters_and_pagination", checks.api_context_and_pagination),
                    ("api_postgres_search_and_quarantined_abc", checks.api_postgres_and_quarantine),
                    ("api_sensitive_details_redacted", checks.api_sensitive_redaction),
                    ("api_regulatory_region_overlap_and_source_evidence", checks.api_regulatory_region_overlap),
                    ("api_complete_source_coverage", checks.api_source_coverage),
                    ("protected_existing_listeners_unchanged", checks.protected_listeners),
                    ("graph_counts_unchanged_after_acceptance", checks.graph_counts)):
                audit.check(name, callback)
            snapshot = checks.snapshot
    except Exception as exc:
        # Setup failures are also sanitized; no connection exception text.
        audit.results.append({"check": "runner_setup_or_teardown", "status": "failed", "error_type": type(exc).__name__})
    passed = bool(audit.results) and all(row["status"] == "passed" for row in audit.results)
    report = {"status": "passed" if passed else "failed", "technical_acceptance_only": True,
              "scientific_readiness": "not_established; inspect modality status and unresolved source semantics",
              "snapshot_id": snapshot, "started_utc": started,
              "completed_utc": datetime.now(timezone.utc).isoformat(), "checks": audit.results,
              "raw_source_values_or_credentials_included": False,
              "existing_service_changes_authorized": False, "sentinel_commits_performed": False}
    private_json(args.root / "audit" / "api-acceptance.json", report)
    print(json.dumps({"status": report["status"], "snapshot_id": snapshot,
                      "passed_checks": sum(row["status"] == "passed" for row in audit.results),
                      "failed_checks": [row["check"] for row in audit.results if row["status"] != "passed"],
                      "technical_acceptance_only": True}), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
