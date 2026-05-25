"""Lazy loader and runtime glossary builder for the schema-skill JSON.

The file at ``data/input/schema_skill.json`` carries hand-curated edge / node /
multi-edge-subgraph interpretation guidance. This module reads that file
defensively (the file may carry trailing commas) and exposes
``build_schema_skill_glossary(neo4j_results)`` — given a list of Neo4j result
entries, return a Markdown block containing ONLY the interpretation entries
that match edge/node types actually present in the results.

Mirrors the pattern of ``functional_data_client.build_functional_data_glossary``.
Used by ``rigor_format_response.py`` and ``rigor_reasoning_response.py`` to
inject entity-specific interpretation guidance into the rigor agents' user
prompt at request time.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_SCHEMA_SKILL_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "input" / "schema_skill.json"
)

_cached_skill: dict | None = None


def _load_schema_skill() -> dict:
    """Lazy, cached load of schema_skill.json.

    The file is hand-edited and occasionally contains trailing commas
    (Python-style JSON) — strip them before parsing.
    """
    global _cached_skill
    if _cached_skill is not None:
        return _cached_skill
    try:
        raw = _SCHEMA_SKILL_PATH.read_text()
        # Strip trailing commas before } or ]
        cleaned = re.sub(r",(\s*[}\]])", r"\1", raw)
        _cached_skill = json.loads(cleaned)
    except Exception as exc:
        logger.warning("Could not load schema_skill.json: %s", exc)
        _cached_skill = {}
    return _cached_skill


def _normalise_edge_key(key: str) -> str:
    """Normalise a skill-edge key for matching against live edge types.

    - Strips trailing semicolons (handles `effector_gene_of;` typo).
    - For compound names like `function_annotation;GO`, keeps as-is so we can
      match both the legacy compound and the bare base form.
    """
    return key.strip().rstrip(";")


def _normalise_node_key(key: str) -> str:
    """Normalise a skill-node key — return the LAST `;` token, matching how
    Cypher MATCH patterns typically use a single label."""
    return key.split(";")[-1].strip()


def _collect_edge_types_from_results(neo4j_results: list[dict]) -> set[str]:
    """Walk neo4j_results (the list of {'query', 'result'} entries) and collect
    every relationship type that appears in any edge object.

    Edge objects in PanKgraph results typically expose `type` or `relationship`
    or are wrapped in dicts with `start`, `end`, ... — we try a few shapes.
    """
    types: set[str] = set()
    for entry in neo4j_results or []:
        result = entry.get("result") if isinstance(entry, dict) else None
        if not isinstance(result, dict):
            continue
        # records-style result: {"nodes": [...], "edges": [...]}  OR  {"records": [...]}
        edges = result.get("edges") or []
        for e in edges:
            if isinstance(e, dict):
                t = e.get("type") or e.get("relationship") or e.get("label")
                if t:
                    types.add(str(t))
        # records: each record may have 'edges' key
        for rec in result.get("records", []) or []:
            if not isinstance(rec, dict):
                continue
            for e in rec.get("edges", []) or []:
                if isinstance(e, dict):
                    t = e.get("type") or e.get("relationship") or e.get("label")
                    if t:
                        types.add(str(t))
        # Also scan the executed Cypher text for `[r:TYPE]` patterns —
        # catches cases where the result shape doesn't carry edge types.
        query_str = entry.get("query") if isinstance(entry, dict) else None
        if isinstance(query_str, str):
            for m in re.finditer(r"\[\s*\w*\s*:\s*`?([A-Za-z_][A-Za-z0-9_;]*)`?\s*[\]\{]", query_str):
                types.add(m.group(1))
    return types


def _collect_node_labels_from_results(neo4j_results: list[dict]) -> set[str]:
    """Walk neo4j_results and collect every node label present."""
    labels: set[str] = set()
    for entry in neo4j_results or []:
        result = entry.get("result") if isinstance(entry, dict) else None
        if not isinstance(result, dict):
            continue
        for n in result.get("nodes", []) or []:
            if isinstance(n, dict):
                lbl = n.get("label") or n.get("type") or n.get("labels")
                if isinstance(lbl, str):
                    labels.add(lbl)
                elif isinstance(lbl, list):
                    for x in lbl:
                        if isinstance(x, str):
                            labels.add(x)
        for rec in result.get("records", []) or []:
            if not isinstance(rec, dict):
                continue
            for n in rec.get("nodes", []) or []:
                if isinstance(n, dict):
                    lbl = n.get("label") or n.get("type") or n.get("labels")
                    if isinstance(lbl, str):
                        labels.add(lbl)
                    elif isinstance(lbl, list):
                        for x in lbl:
                            if isinstance(x, str):
                                labels.add(x)
        # Also scan executed Cypher for (x:Label) patterns
        query_str = entry.get("query") if isinstance(entry, dict) else None
        if isinstance(query_str, str):
            for m in re.finditer(r"\(\s*\w*\s*:\s*([A-Za-z_][A-Za-z0-9_; ]*?)\s*(?:\{|\)|$)", query_str):
                labels.add(m.group(1).strip().rstrip(")"))
    return labels


def build_schema_skill_glossary(neo4j_results: list[dict]) -> str:
    """Return a Markdown glossary listing interpretation rules from
    schema_skill.json that apply to entities present in ``neo4j_results``.

    The output is intended to be appended to the rigor agent's user prompt
    (next to the FUNCTIONAL DATA GLOSSARY). Entries whose key doesn't match
    any live edge/node in the results are silently skipped — this is what
    keeps the file's 4 dead entries (DEG_in, OCR_locate_in, effector_gene_of;,
    expression_level_in) from polluting the prompt. Returns empty string
    when no entries match.
    """
    skill = _load_schema_skill()
    edge_skill = skill.get("edge_skill", {}) or {}
    node_skill = skill.get("node_skill", {}) or {}
    complex_skill = skill.get("complex_skill", {}) or {}

    live_edges = _collect_edge_types_from_results(neo4j_results)
    live_node_labels = _collect_node_labels_from_results(neo4j_results)

    # Build normalised match sets
    norm_live_edges = {_normalise_edge_key(e) for e in live_edges}
    norm_live_nodes = set()
    for lab in live_node_labels:
        for tok in lab.split(";"):
            t = tok.strip()
            if t:
                norm_live_nodes.add(t)

    # Match edges
    matched_edges: list[tuple[str, str]] = []
    for key, desc in edge_skill.items():
        nkey = _normalise_edge_key(key)
        if nkey in norm_live_edges:
            matched_edges.append((nkey, desc))

    # Match nodes
    matched_nodes: list[tuple[str, str]] = []
    for key, desc in node_skill.items():
        nkey = _normalise_node_key(key)
        if nkey in norm_live_nodes:
            matched_nodes.append((key, desc))

    # Match complex (subgraph) entries — keys are `#`-joined edge/node tokens.
    # Include a complex entry only if ALL of its tokens are present in the live result.
    matched_complex: list[tuple[str, str]] = []
    for key, desc in complex_skill.items():
        tokens = [t.strip() for t in key.split("#") if t.strip()]
        norm_tokens = [_normalise_edge_key(t) for t in tokens]
        if all((t in norm_live_edges or t in norm_live_nodes) for t in norm_tokens) and norm_tokens:
            matched_complex.append((key, desc))

    if not matched_edges and not matched_nodes and not matched_complex:
        return ""

    lines = ["\n=== SCHEMA SKILL GLOSSARY ==="]
    lines.append(
        "_The following interpretation rules come from `schema_skill.json` and "
        "describe how to interpret the specific edge / node types present in "
        "the retrieved data above._\n"
    )

    if matched_edges:
        lines.append("## Edge interpretation")
        for k, d in sorted(matched_edges):
            lines.append(f"- **`{k}`**: {d}")
        lines.append("")

    if matched_nodes:
        lines.append("## Node interpretation")
        for k, d in sorted(matched_nodes):
            lines.append(f"- **`{k}`**: {d}")
        lines.append("")

    if matched_complex:
        lines.append("## Multi-edge subgraph interpretation")
        for k, d in sorted(matched_complex):
            label = k.replace("#", " + ")
            lines.append(f"- **{label}**: {d}")
        lines.append("")

    return "\n".join(lines)
