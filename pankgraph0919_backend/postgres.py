"""Parameterized reads over the external cakg multimodal standard only."""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from cakg.multimodal import SCHEMA_NAME
from fastapi import HTTPException

from .models import Filters

if SCHEMA_NAME != "cakg_mm":
    raise RuntimeError("Unsupported context standard schema")


def filter_sql(filters, parameters):
    """Column expressions are constants; every caller value is a bound parameter."""
    clauses = []
    columns = {"source_file_id": "er.source_file_id", "collection_id": "er.collection_id", "context_id": "er.context_id",
               "context_kind": "c.context_kind", "condition": "c.condition",
               "cell_type_id": "c.cell_type_id", "record_status": "er.assertion_status"}
    for key, value in filters.model_dump().items():
        if value is not None:
            clauses.append(columns[key] + " = %s")
            parameters.append(value)
    return " AND " + " AND ".join(clauses) if clauses else ""


def page(items, total, limit, offset):
    return {"items": items, "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(items) < total,
            "next_offset": offset + len(items) if items and offset + len(items) < total else None}


class PostgresStore:
    def __init__(self, settings, connect=None):
        self.settings = settings
        self.connect = connect or psycopg.connect

    @contextmanager
    def transaction(self):
        # Explicit keyword options override options embedded in a DSN. The
        # deployment reader also lacks all DDL/DML grants as a separate guard.
        with self.connect(
                self.settings.postgres_read_dsn, row_factory=dict_row,
                connect_timeout=5, options=("-c default_transaction_read_only=on "
                                           f"-c statement_timeout={self.settings.query_timeout * 1000}")) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database() AS database, current_user AS role, "
                            "current_setting('transaction_read_only') AS read_only")
                identity = cur.fetchone()
                if identity != {"database": "pankgraph0919", "role": "pankgraph0919_reader", "read_only": "on"}:
                    raise HTTPException(503, "Postgres reader identity mismatch")
                yield cur

    def accepted(self, cur):
        cur.execute("SELECT status, (SELECT count(*) FROM cakg_mm.snapshot) AS snapshot_count "
                    "FROM cakg_mm.snapshot WHERE snapshot_id = %s", (self.settings.snapshot_id,))
        row = cur.fetchone()
        if not row or row["status"] != "accepted" or row["snapshot_count"] != 1:
            raise HTTPException(409, "Snapshot has not passed acceptance")

    def probe(self):
        with self.transaction() as cur:
            self.accepted(cur)
            cur.execute("SELECT go.object_kind, count(*) AS count FROM cakg_mm.snapshot_object so "
                        "JOIN cakg_mm.graph_object go USING (object_id) WHERE so.snapshot_id = %s "
                        "GROUP BY go.object_kind", (self.settings.snapshot_id,))
            counts = {"node": 0, "edge": 0}
            for row in cur.fetchall():
                if row["object_kind"] not in counts:
                    raise HTTPException(409, "Unsupported object kind in accepted snapshot")
                counts[row["object_kind"]] = row["count"]
        return {"database": "pankgraph0919", "role": "pankgraph0919_reader", "read_only": True,
                "snapshot_id": self.settings.snapshot_id, "status": "accepted", "object_counts": counts}

    def search_genes(self, search):
        parameters = [self.settings.snapshot_id]
        where = "so.snapshot_id = %s AND go.entity_type = 'Gene' AND go.object_kind = 'node'"
        if search.query and search.query.strip():
            text = search.query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where += (" AND (go.properties->>'canonical_id' ILIKE %s OR go.properties->>'name' ILIKE %s "
                      "OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(CASE "
                      "WHEN jsonb_typeof(go.properties->'aliases') = 'array' THEN go.properties->'aliases' "
                      "ELSE '[]'::jsonb END) alias WHERE alias ILIKE %s))")
            parameters += [f"%{text}%"] * 3
        if search.chromosome is not None:
            where += (" AND EXISTS (SELECT 1 FROM cakg_mm.entity_interval ei WHERE ei.object_id = go.object_id "
                      "AND ei.genome_assembly = %s AND ei.chr = %s AND ei.locus && int8range(%s, %s, '[)'))")
            parameters += [search.assembly, search.chromosome, search.start, search.end]
        common = " FROM cakg_mm.graph_object go JOIN cakg_mm.snapshot_object so USING (object_id) WHERE " + where
        limit = min(search.limit, self.settings.max_rows)
        with self.transaction() as cur:
            self.accepted(cur)
            cur.execute("SELECT count(*) AS count" + common, parameters)
            total = cur.fetchone()["count"]
            cur.execute("SELECT go.object_id, go.entity_type, go.node_labels, go.properties" + common +
                        " ORDER BY go.object_id LIMIT %s OFFSET %s", parameters + [limit, search.offset])
            items = cur.fetchall()
        return page(items, total, limit, search.offset)

    def records(self, object_ids, filters, limit, offset):
        limit = min(limit, self.settings.max_rows)
        parameters = [self.settings.snapshot_id]
        common = (" FROM cakg_mm.evidence_record er JOIN cakg_mm.context c USING (context_id) "
                  "JOIN cakg_mm.source_file sf ON sf.file_id = er.source_file_id "
                  "JOIN cakg_mm.snapshot_record sr USING (record_id) "
                  "WHERE sr.snapshot_id = %s")
        if object_ids is not None:
            common += (" AND EXISTS (SELECT 1 FROM cakg_mm.object_evidence oe WHERE oe.snapshot_id = sr.snapshot_id "
                       "AND oe.record_id = er.record_id AND oe.object_id = ANY(%s))")
            parameters.append(object_ids)
        else:
            scope = {k: getattr(filters, k) for k in ("source_file_id", "collection_id", "context_id")}
            if not any(scope.values()):
                raise HTTPException(422, "Record search requires an explicit source or collection or context")
            common += filter_sql(Filters(**scope), parameters)
            filters = filters.model_copy(update={k: None for k in scope})
        with self.transaction() as cur:
            self.accepted(cur)
            registry = []
            if object_ids is not None:
                cur.execute("SELECT object_id, object_kind FROM cakg_mm.snapshot_object "
                            "JOIN cakg_mm.graph_object USING (object_id) WHERE snapshot_id = %s AND object_id = ANY(%s)",
                            (self.settings.snapshot_id, object_ids))
                registry = cur.fetchall()
                if {r["object_id"] for r in registry} != set(object_ids):
                    raise HTTPException(409, "Object is absent from the accepted snapshot")
            cur.execute("SELECT count(*) AS count" + common, parameters)
            unfiltered_total = cur.fetchone()["count"]
            common += filter_sql(filters, parameters)
            cur.execute("SELECT count(*) AS count" + common, parameters)
            total = cur.fetchone()["count"]
            cur.execute("SELECT er.record_id, er.collection_id, er.context_id, er.source_record_key, "
                        "er.assertion_status, er.metrics, er.raw_record, er.metadata, "
                        "c.context_kind, c.condition, c.cell_type_id, c.metadata AS context_metadata, "
                        "sf.file_id AS source_file_id, sf.dataset_id, sf.url AS source_url, sf.sha256, "
                        "sf.status AS source_status, sf.metadata AS source_metadata" + common +
                        " ORDER BY er.record_id LIMIT %s OFFSET %s", parameters + [limit, offset])
            items = cur.fetchall()
            # An accepted loader disallows this; detect corruption again before
            # exposing any record as relationship evidence.
            if any(r["object_kind"] == "edge" for r in registry) and any(
                    r["assertion_status"] == "quarantined" or r["source_status"] != "released" for r in items):
                raise HTTPException(409, "Blocked record cannot support graph relationship evidence")
            for item in items:
                classification = item["source_metadata"].get("privacy_classification", "unclassified")
                item["privacy_classification"] = classification
                if not self.settings.allow_sensitive_records and classification not in {"public_aggregate", "public_metadata"}:
                    for field in ("raw_record", "metadata", "context_metadata", "source_metadata", "metrics"):
                        item.pop(field, None)
                    item["details_redacted"] = True
                else:
                    item["details_redacted"] = False
        return {**page(items, total, limit, offset), "unfiltered_total": unfiltered_total,
                "object_ids": object_ids or [],
                "match_status": "matched" if total else "no_matching_records" if unfiltered_total else "no_evidence"}

    def contexts(self, filters, limit, offset):
        parameters = [self.settings.snapshot_id]
        common = (" FROM cakg_mm.context c WHERE EXISTS (SELECT 1 FROM cakg_mm.evidence_record er "
                  "JOIN cakg_mm.snapshot_record sr USING (record_id) WHERE er.context_id = c.context_id "
                  "AND sr.snapshot_id = %s" + filter_sql(filters, parameters) + ")")
        limit = min(limit, self.settings.max_rows)
        with self.transaction() as cur:
            self.accepted(cur)
            cur.execute("SELECT count(*) AS count" + common, parameters)
            total = cur.fetchone()["count"]
            cur.execute("SELECT c.context_id, c.collection_id, c.context_kind, c.condition, c.cell_type_id, c.metadata" +
                        common + " ORDER BY c.context_id LIMIT %s OFFSET %s", parameters + [limit, offset])
            items = cur.fetchall()
            if not self.settings.allow_sensitive_records:
                for item in items:
                    if item["metadata"].get("privacy_classification") not in {"public_aggregate", "public_metadata"}:
                        item.pop("metadata", None)
                        item["metadata_redacted"] = True
        return page(items, total, limit, offset)

    def sources(self, limit, offset):
        limit = min(limit, self.settings.max_rows)
        # The standard enforces one immutable snapshot in an empty schema. The
        # acceptance check above verifies this invariant; include catalog-only
        # source rows even when acquisition produced no evidence/rejections.
        common = " FROM cakg_mm.source_file sf"
        parameters = []
        with self.transaction() as cur:
            self.accepted(cur)
            cur.execute("SELECT count(*) AS count" + common, parameters)
            total = cur.fetchone()["count"]
            cur.execute("SELECT sf.file_id, sf.dataset_id, sf.url, sf.sha256, sf.status, sf.metadata" + common +
                        " ORDER BY sf.file_id LIMIT %s OFFSET %s", parameters + [limit, offset])
            items = cur.fetchall()
            ids = [item["file_id"] for item in items]
            cur.execute("SELECT er.source_file_id, er.assertion_status, count(*) AS count "
                        "FROM cakg_mm.evidence_record er JOIN cakg_mm.snapshot_record sr USING (record_id) "
                        "WHERE sr.snapshot_id = %s AND er.source_file_id = ANY(%s) GROUP BY er.source_file_id, er.assertion_status",
                        (self.settings.snapshot_id, ids))
            counts = cur.fetchall()
            cur.execute("SELECT file_id, reason_code, count, details FROM cakg_mm.source_rejection "
                        "WHERE snapshot_id = %s AND file_id = ANY(%s) ORDER BY file_id, reason_code",
                        (self.settings.snapshot_id, ids))
            rejections = cur.fetchall()
            for source in items:
                source["record_counts"] = {r["assertion_status"]: r["count"] for r in counts if r["source_file_id"] == source["file_id"]}
                source["rejections"] = [r.copy() for r in rejections if r["file_id"] == source["file_id"]]
                if not self.settings.allow_sensitive_records and source["metadata"].get("privacy_classification") not in {
                        "public_aggregate", "public_metadata"}:
                    source["metadata"] = {"privacy_classification": source["metadata"].get("privacy_classification", "unclassified")}
                    source["metadata_redacted"] = True
                    for rejection in source["rejections"]:
                        rejection.pop("details", None)
        return {**page(items, total, limit, offset),
                "coverage_scope": "all source files in the single immutable snapshot, including catalog-only sources"}

    def close(self):
        """Connections are scoped to read-only transactions; no pool is retained."""
