"""Pure, cached cell membership from a complete release-specific ontology snapshot.

No query templates, graph calls, record measurements or question rewriting live
here. Callers retain the requested organ constraint and use the returned typed
membership/provenance when preparing GPU-generated checks.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

REGISTRY_PATH = Path(__file__).with_name("anatomy_membership.json")
_BYTES = REGISTRY_PATH.read_bytes()
REGISTRY = json.loads(_BYTES)
VERSION = REGISTRY["version"]
DIGEST = hashlib.sha256(_BYTES).hexdigest()
CLOSURE_RELATIONS = frozenset({"HAS_CELL_TYPE", "PART_OF", "SUBCLASS_OF"})
ENDPOINT_CATEGORIES = {
    "HAS_CELL_TYPE": ({"cell_type"}, {"tissue"}),
    "PART_OF": ({"region", "tissue"}, {"tissue"}),
    "SUBCLASS_OF": ({"cell_type", "tissue"}, {"cell_type"}),
    "HAS_STATE": ({"cell_type"}, {"cell_type"}),
}


def _validate_snapshot(snapshot: dict) -> None:
    if snapshot.get("complete_inventory") is not True:
        raise ValueError("anatomical_membership_inventory_incomplete")
    if not snapshot.get("source_sha256") or not snapshot.get("category_source_sha256"):
        raise ValueError("anatomical_membership_source_identity_missing")
    categories = snapshot["categories"]
    seen = set()
    for edge in snapshot["edges"]:
        identity = (edge["source"], edge["relation"], edge["target"])
        if identity in seen:
            raise ValueError("duplicate_anatomical_membership_edge")
        seen.add(identity)
        allowed = ENDPOINT_CATEGORIES.get(edge["relation"])
        if allowed is None or categories.get(edge["source"]) not in allowed[0] or categories.get(edge["target"]) not in allowed[1]:
            raise ValueError("invalid_anatomical_membership_endpoint_role")


def membership_from_snapshot(root_id: str, graph_release: str, snapshot: dict, registry_digest: str) -> dict:
    """Resolve every recorded base-cell path to a tissue/region; states stay separate.

    A result is ontology membership, not a measured cell census or a marker
    ranking. Unknown roots/releases and incomplete/cyclic inventories fail
    explicitly. The function does not mutate the snapshot or caller's plan.
    """
    _validate_snapshot(snapshot)
    if graph_release != snapshot["graph_release"]:
        raise ValueError("anatomical_membership_graph_release_mismatch")
    category = snapshot["categories"].get(root_id)
    if category is None:
        raise ValueError("unregistered_anatomical_membership_root")
    if category not in {"tissue", "region"}:
        raise ValueError("anatomical_membership_root_requires_tissue_or_region")
    incoming = defaultdict(list)
    for edge in snapshot["edges"]:
        if edge["relation"] in CLOSURE_RELATIONS:
            incoming[edge["target"]].append(edge)
    paths = defaultdict(list)

    def visit(target: str, suffix: list[dict], visited: frozenset[str]) -> None:
        for edge in incoming[target]:
            source = edge["source"]
            if source in visited:
                raise ValueError("cyclic_anatomical_membership_inventory")
            path = [dict(edge), *suffix]
            if snapshot["categories"][source] == "cell_type":
                paths[source].append(path)
            visit(source, path, visited | {source})

    visit(root_id, [], frozenset({root_id}))
    state_parents = defaultdict(set)
    for edge in snapshot["edges"]:
        if edge["relation"] == "HAS_STATE" and edge["source"] in paths:
            state_parents[edge["target"]].add(edge["source"])
    # State identity is explicit in the source graph, never inferred from '_'.
    for state_id in state_parents:
        if state_id in paths:
            raise ValueError("ambiguous_base_cell_and_state_membership")
    cells = sorted(paths)
    return {
        "state": "resolved" if cells else "no_registered_cell_membership",
        "graph_release": graph_release,
        "registry_version": snapshot["version"],
        "registry_digest": registry_digest,
        "source_sha256": snapshot["source_sha256"],
        "category_source_sha256": snapshot["category_source_sha256"],
        "requested_root": {"entity_type": "anatomical_structure", "id": root_id, "category": category},
        "match_kind": "verified_anatomical_hierarchy_membership",
        "cell_ids": cells,
        "cell_count": len(cells),
        "cell_paths": {cell: paths[cell] for cell in cells},
        "maximum_path_length": max((len(p) for alternatives in paths.values() for p in alternatives), default=0),
        "states": [{"id": state, "base_cell_ids": sorted(state_parents[state]), "relation": "HAS_STATE"}
                   for state in sorted(state_parents)],
        "coverage": "Complete recorded hierarchy snapshot; cell membership does not require a marker annotation.",
    }


@lru_cache(maxsize=128)
def _cached_membership(root_id: str, graph_release: str, registry_digest: str) -> dict:
    if registry_digest != DIGEST:
        raise ValueError("anatomical_membership_registry_digest_mismatch")
    return membership_from_snapshot(root_id, graph_release, REGISTRY, registry_digest)


def resolve_cell_membership(root_id: str, graph_release: str) -> dict:
    """Return an independent copy of release-and-digest-cached ontology membership."""
    return copy.deepcopy(_cached_membership(root_id, graph_release, DIGEST))
