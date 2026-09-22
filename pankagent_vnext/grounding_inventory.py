"""Read-only, release-scoped metadata catalog for pre-planning grounding.

Inventory building is an explicit warm-up/offline operation. It stores public
entity records plus aggregate sample/donor terminology (recorded stage, source,
and assay vocabularies), never donor rows, individual sample records, or donor
identifiers. The cache is protected mode 0600. Clinical donor-category values
used for execution stay in the live local semantic resolver and are not copied
into model grounding. Schema observations are diagnostic until reviewed; they
never silently replace the release registry used by validation.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
import re
import stat
from pathlib import Path
import tempfile
import inspect

from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST
from .semantic_registry import DIGEST as SEMANTIC_DIGEST, CAPABILITIES, SOURCE

VERSION = "grounding-inventory-6-live-refresh"
ENVELOPE_DIGEST_VERSION = "grounding-inventory-envelope-v1"
DEFAULT_CACHE_TTL_SECONDS = 300.0
PUBLIC_CATALOG_LABELS = (
    "Gene", "anatomical_structure", "disease", "GO_term", "kegg", "reactome", "data_modality",
)
ANNOTATION_SOURCE_QUERY = (
    "MATCH ()-[r:`FUNCTION_ANNOTATION`]->() RETURN collect(DISTINCT r.data_source) AS values, "
    "count(r) AS record_count, count(r.data_source) AS valued_records")


def stable_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def inventory_envelope_digest(inventory):
    """Detect accidental partial edits across cache metadata and content.

    This is an unkeyed consistency digest, not an authenticity mechanism. File
    permissions and rebuilding from the selected graph are the trust boundary.
    """
    return stable_digest({
        "version": ENVELOPE_DIGEST_VERSION,
        "content_digest": inventory.get("content_digest"),
        "built_at": inventory.get("built_at"),
        "identity": inventory.get("identity"),
    })


def inventory_age_seconds(inventory, *, now=None):
    """Return the age of a timestamped inventory or reject an invalid clock value."""
    raw = inventory.get("built_at") if isinstance(inventory, dict) else None
    if not isinstance(raw, str) or not raw:
        raise ValueError("invalid_grounding_inventory_built_at")
    try:
        built_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_grounding_inventory_built_at") from exc
    if built_at.tzinfo is None:
        raise ValueError("invalid_grounding_inventory_built_at")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("invalid_grounding_inventory_clock")
    age = (current.astimezone(timezone.utc) - built_at.astimezone(timezone.utc)).total_seconds()
    # A future timestamp must not extend cache lifetime indefinitely when a
    # clock or persisted file is wrong. Normal builds always timestamp before
    # they are published.
    if age < 0:
        raise ValueError("invalid_grounding_inventory_built_at")
    return age


def validated_cache_ttl(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or value < 0):
        raise ValueError("invalid_grounding_inventory_ttl")
    return float(value)


def inventory_identity(graph):
    """The public identity carries hashes, never addresses or credentials."""
    details = graph.preview_identity()
    return {
        "version": VERSION, "graph_release": graph.settings.graph_version,
        "schema_digest": SCHEMA_DIGEST, "semantic_digest": SEMANTIC_DIGEST,
        "source_identity_digest": stable_digest({key: details.get(key) for key in
            ("graph_version", "identity_manifest_sha256", "neo4j_uri", "neo4j_database")}),
    }


def catalog_query(label):
    if label not in PUBLIC_CATALOG_LABELS or label not in REGISTRY["nodes"]:
        raise ValueError("unsupported_grounding_catalog")
    fields = REGISTRY["nodes"][label]
    columns = [f"n.{field} AS {field}" if field in fields else f"null AS {field}"
               for field in ("id", "name", "synonyms", "hgnc_symbol", "hgnc_id")]
    if label == "GO_term":
        columns.append("n.go_domain AS go_domain")
    return f"MATCH (n:`{label}`) RETURN " + ", ".join(columns) + ", labels(n) AS labels"


def _text(value):
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= 512 else None


def _synonyms(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = value.split("|") if "|" in value else value.split(";") if ";" in value else [value]
        value = parsed if isinstance(parsed, list) else [value]
    return sorted({v for item in value or [] if (v := _text(item))}) if isinstance(value, list) else []


def public_record(label, row):
    identifier = _text(row.get("id"))
    labels = row.get("labels")
    if not identifier or not isinstance(labels, list) or label not in labels:
        raise ValueError("invalid_grounding_catalog_record")
    allowed_labels = sorted(set(labels) & REGISTRY["nodes"].keys())
    name = _text(row.get("name")) or _text(row.get("hgnc_symbol")) or identifier
    aliases = _synonyms(row.get("synonyms"))
    aliases.extend(v for field in ("hgnc_symbol", "hgnc_id") if (v := _text(row.get(field))))
    record = {"id": identifier, "name": name, "entity_type": label,
              "labels": allowed_labels, "aliases": sorted(set(aliases) - {identifier, name})}
    # Keep the primary symbol's field provenance. A historic synonym or the
    # display name alone cannot authorize an HGNC-symbol predicate rewrite.
    if label == "Gene" and (symbol := _text(row.get("hgnc_symbol"))):
        record["hgnc_symbol"] = symbol
    return record


async def build_inventory(graph, *, include_schema_observations=False):
    """Scan the entire selected public catalog, with at most two metadata reads.

    Optional full schema scans can be expensive and belong to explicit operator
    builds, not individual questions. They read every record rather than samples.
    """
    await graph._ensure_identity()
    if graph.settings.graph_version != REGISTRY["release"]:
        raise ValueError("grounding_registry_release_mismatch")
    identity = inventory_identity(graph)
    semaphore = asyncio.Semaphore(2)
    public_categories = {}
    category_metadata = {}

    async def one(label):
        async with semaphore:
            rows = await graph._small_query(catalog_query(label))
        if label == "GO_term":
            values = sorted({value for row in rows if (value := _text(row.get("go_domain")))})
            public_categories["GO_term.go_domain"] = values
            category_metadata["GO_term.go_domain"] = {
                "state": "checked" if values or not rows else "metadata_unavailable",
                "complete_scan": True, "value_count": len(values),
                "missing_records": sum(_text(row.get("go_domain")) is None for row in rows),
                "source": "distinct values from full public GO_term catalog scan",
            }
        return label, [public_record(label, row) for row in rows]

    async def annotation_sources():
        key = "FUNCTION_ANNOTATION.data_source"
        try:
            async with semaphore:
                rows = await graph._small_query(ANNOTATION_SOURCE_QUERY)
            row = rows[0] if isinstance(rows, list) and len(rows) == 1 else {}
            raw = row.get("values")
            total, valued = row.get("record_count"), row.get("valued_records")
            valid = (isinstance(raw, list) and all(_text(value) == value for value in raw)
                     and type(total) is int and type(valued) is int and 0 <= valued <= total
                     and (bool(raw) == bool(valued)))
            if not valid:
                raise ValueError("invalid_annotation_source_inventory")
            values = sorted(set(raw))
            public_categories[key] = values
            category_metadata[key] = {"state": "checked", "complete_scan": True,
                "value_count": len(values), "record_count": total, "missing_records": total - valued,
                "source": "distinct values from full FUNCTION_ANNOTATION relationship scan"}
        except Exception:
            # Optional public metadata failure cannot supply invented categories.
            public_categories[key] = []
            category_metadata[key] = {"state": "metadata_unavailable", "complete_scan": False,
                                      "value_count": 0}

    loaded = await asyncio.gather(*(one(label) for label in PUBLIC_CATALOG_LABELS), annotation_sources())
    loaded = loaded[:-1]
    records = [record for _, items in loaded for record in items]
    # A duplicated ID across collections remains separate for ambiguity review.
    records.sort(key=lambda record: (record["entity_type"], record["id"], record["name"]))
    semantic = graph.semantic_vocabulary
    parameters = inspect.signature(semantic).parameters
    vocabulary = await semantic(force=True) if 'force' in parameters else await semantic()
    terminology = {key: vocabulary.get(key) for key in
                   ("stages", "sources", "donor_sources", "sample_sources", "modalities",
                    "assay_donor_sources", "inventory_complete")}
    recorded_stages = vocabulary.get("stages")
    stage_values = recorded_stages if isinstance(recorded_stages, list) else []
    valid_stages = [value for value in stage_values if isinstance(value, str)
                    and re.fullmatch(r"Stage [123]:\s*\S.*", value)]
    invalid_stages = [value for value in stage_values if value not in valid_stages]
    # Preserve unexpected raw values in protected diagnostics, but never offer
    # a source/product name as a clinical stage to the planner or the user.
    terminology["stages"] = valid_stages
    terminology["stage_metadata_quality"] = {
        "valid_value_count": len(valid_stages), "excluded_value_count": len(invalid_stages),
        "state": "unrecognized_values_excluded" if invalid_stages else "checked" if isinstance(recorded_stages, list) else "inventory_unavailable",
        "rule": "Only the listed recorded clinical-stage labels are selectable; excluded metadata values are not clinical stages.",
    }
    if not isinstance(recorded_stages, list):
        terminology["inventory_complete"] = False
    terminology["assay_capabilities"] = {
        assay: {"components": components, "scope": "documented HPAP protocol", "source": SOURCE}
        for assay, components in CAPABILITIES.items()
        if assay in (vocabulary.get("modalities") or [])
    }
    terminology["capability_scope_rule"] = (
        "A capability is not a synonym for an exact standalone assay. Apply documented capability "
        "expansion only to HPAP records or the verified HPAP-only assay sources shown here; "
        "CITE-seq Protein alone does not establish RNA data. Indexed assays do not verify downloadable files.")
    value = {"identity": identity, "built_at": datetime.now(timezone.utc).isoformat(),
             "catalog_complete": True, "catalog_labels": list(PUBLIC_CATALOG_LABELS),
             "counts": {label: len(items) for label, items in loaded}, "records": records,
             "sample_terminology": terminology,
             "public_categories": public_categories, "category_metadata": category_metadata,
             "metadata_quality_diagnostics": {"unrecognized_donor_stage_values": invalid_stages},
             "schema_source": "reviewed_full_release_registry"}
    if include_schema_observations:
        value["schema_observations"] = await observe_schema(graph)
    value["content_digest"] = stable_digest({key: item for key, item in value.items() if key != "built_at"})
    value["envelope_digest"] = inventory_envelope_digest(value)
    return value


async def observe_schema(graph):
    """Full read-only directed/property inventory; never updates Neo4j/registry."""
    node_rows = await graph._small_query(
        "MATCH (n) UNWIND labels(n) AS label UNWIND CASE WHEN size(keys(n))=0 THEN [null] ELSE keys(n) END AS property "
        "RETURN label, collect(DISTINCT property) AS properties")
    edge_rows = await graph._small_query(
        "MATCH (a)-[r]->(b) WITH labels(a) AS source, type(r) AS relation, "
        "labels(b) AS target, keys(r) AS properties "
        "UNWIND CASE WHEN size(properties)=0 THEN [null] ELSE properties END AS property "
        "RETURN source, relation, target, collect(DISTINCT property) AS properties")
    return {"complete_scan": True, "nodes": node_rows, "directed_relationships": edge_rows,
            "admission": "diagnostic_only_requires_registry_review"}


def write_inventory(path, inventory):
    """Atomic private catalog, separate from Git and operational logs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".grounding-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(inventory, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_inventory(path, identity, *, max_age_seconds=DEFAULT_CACHE_TTL_SECONDS):
    path = Path(path)
    fd = None
    try:
        # Validate the same inode that is read.  lstat rejects a cache-path
        # symlink, O_NOFOLLOW closes the replacement race where supported, and
        # the inode comparison is the portable fallback for that race.
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise ValueError("stale_or_invalid_grounding_inventory")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_mode & 0o077):
            raise ValueError("stale_or_invalid_grounding_inventory")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = None
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("stale_or_invalid_grounding_inventory") from exc
    finally:
        if fd is not None:
            os.close(fd)
    expected = value.get("content_digest")
    expected_envelope = value.get("envelope_digest")
    body = {key: val for key, val in value.items()
            if key not in {"content_digest", "built_at", "envelope_digest"}}
    if (value.get("identity") != identity or not value.get("catalog_complete")
            or expected != stable_digest(body)
            or not isinstance(expected_envelope, str)
            or not hmac.compare_digest(expected_envelope, inventory_envelope_digest(value))
            or value.get("catalog_labels") != list(PUBLIC_CATALOG_LABELS)
            or not isinstance(value.get("sample_terminology"), dict)
            or not isinstance(value.get("public_categories"), dict)):
        raise ValueError("stale_or_invalid_grounding_inventory")
    ttl_seconds = validated_cache_ttl(max_age_seconds)
    try:
        age_seconds = inventory_age_seconds(value)
    except ValueError as exc:
        raise ValueError("stale_or_invalid_grounding_inventory") from exc
    if age_seconds > ttl_seconds:
        raise ValueError("stale_or_invalid_grounding_inventory")
    return value


async def _main_async(args):
    from .config import Settings
    from .graph import GraphAdapter
    # Settings reads the existing protected deployment environment.
    graph = GraphAdapter(Settings())
    try:
        inventory = await build_inventory(graph, include_schema_observations=args.observe_schema)
        write_inventory(args.output, inventory)
        print(json.dumps({"path": str(args.output), "identity": inventory["identity"],
                          "counts": inventory["counts"], "content_digest": inventory["content_digest"]}))
    finally:
        await graph.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--observe-schema", action="store_true",
                        help="Include a full read-only property and directed-endpoint scan (offline only)")
    asyncio.run(_main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
