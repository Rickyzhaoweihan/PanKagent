"""Tests for the lazy entity-aware schema slice accessors."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from src.schema_loader import (
    extract_entities_from_cypher,
    get_detailed_schema_for_cypher,
    get_property_enum_map,
)


def test_enum_map_scope_excludes_unrelated_entities():
    """The slice for `donor` + `T1D_DEG_in` must NOT include OCR_peak / gene_ontology."""
    emap = get_property_enum_map(["donor"], ["T1D_DEG_in"])
    assert "node_properties" in emap and "edge_properties" in emap
    assert "donor" in emap["node_properties"]
    assert "T1D_DEG_in" in emap["edge_properties"]
    # Unrelated entities must not leak in
    assert "OCR_peak" not in emap["node_properties"]
    assert "gene_ontology" not in emap["node_properties"]
    assert "OCR_peak_in" not in emap["edge_properties"]


def test_enum_map_carries_actual_enum_values():
    """T1D_DEG_in.UpOrDownRegulation should be the canonical 2-value enum."""
    emap = get_property_enum_map([], ["T1D_DEG_in"])
    up_down = emap["edge_properties"].get("T1D_DEG_in", {}).get("UpOrDownRegulation")
    assert up_down is not None
    assert "Upregulated in T1D" in up_down
    assert "Downregulated in T1D" in up_down


def test_enum_map_skips_high_cardinality_properties():
    """gene.name has 78K distinct values — should NOT be in the enum map."""
    emap = get_property_enum_map(["gene"], [])
    gene_props = emap["node_properties"].get("gene", {})
    # gene.name uses `examples` (not `values`) after resync, so excluded
    assert "name" not in gene_props


def test_extract_entities_from_cypher_round_trips():
    """The label/rel-type extractor must surface the entities the LLM picked."""
    cypher = (
        "MATCH (g:gene)-[r:T1D_DEG_in]->(c:anatomical_structure) "
        "WHERE c.name = 'beta cell' RETURN g, r, c"
    )
    ent = extract_entities_from_cypher(cypher)
    assert "gene" in ent["node_labels"]
    assert "anatomical_structure" in ent["node_labels"]
    assert "T1D_DEG_in" in ent["relationship_types"]


def test_get_detailed_schema_for_cypher_returns_paired_views():
    """get_detailed_schema_for_cypher returns (text, enum_map) — same entity set."""
    draft = (
        "MATCH (g:gene)-[r:T1D_DEG_in]->(c:anatomical_structure) "
        "WHERE c.name = 'beta cell' RETURN g, r, c"
    )
    text, enum_map = get_detailed_schema_for_cypher(draft)

    assert isinstance(text, str) and len(text) > 100
    assert "T1D_DEG_in" in text
    assert "anatomical_structure" in text

    assert "T1D_DEG_in" in enum_map["edge_properties"]
    assert "anatomical_structure" in enum_map["node_properties"]


def test_slice_for_unknown_entity_is_empty():
    """Asking for an entity that doesn't exist in the schema must not crash."""
    emap = get_property_enum_map(["nonexistent_entity"], ["fake_edge"])
    assert emap == {"node_properties": {}, "edge_properties": {}}
