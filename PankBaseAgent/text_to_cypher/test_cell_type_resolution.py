"""Tests for token-set cell-type name resolution in the Cypher validator.

The model often emits an abbreviated/reordered cell-type label (e.g. "ductal" or
"MUC5B+Ductal") that does not match the stored anatomical_structure name verbatim,
so the query returns 0 rows. Token-set resolution maps such labels back to the
canonical schema name.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from cypher_validator import (  # noqa: E402
    _cell_type_token_set,
    _build_cell_type_token_set_map,
    fix_cell_type_references,
)


# ---------------------------------------------------------------------------
# _cell_type_token_set
# ---------------------------------------------------------------------------

def test_token_set_splits_on_plus():
    assert _cell_type_token_set("MUC5B+Ductal") == frozenset({"muc5b", "ductal"})


def test_token_set_strips_generic_words():
    assert _cell_type_token_set("ductal cell") == frozenset({"ductal"})
    assert _cell_type_token_set("pancreatic ductal cell MUC5B+") == frozenset({"ductal", "muc5b"})


def test_token_set_order_independent():
    assert _cell_type_token_set("MUC5B+ Ductal Cell") == _cell_type_token_set("ductal MUC5B")


def test_token_set_keeps_state_qualifiers():
    # "active"/"quiescent" must remain significant so states don't collide
    assert "active" in _cell_type_token_set("pancreatic stellate cell active state")


# ---------------------------------------------------------------------------
# _build_cell_type_token_set_map
# ---------------------------------------------------------------------------

def test_map_resolves_unique_sets():
    names = ["ductal cell", "pancreatic ductal cell MUC5B+", "beta cell"]
    m = _build_cell_type_token_set_map(names)
    assert m[frozenset({"ductal"})] == "ductal cell"
    assert m[frozenset({"ductal", "muc5b"})] == "pancreatic ductal cell MUC5B+"
    assert m[frozenset({"beta"})] == "beta cell"


def test_map_drops_ambiguous_sets():
    # two distinct names reducing to the same token set must NOT be mapped
    names = ["ductal cell", "ductal cells"]  # both -> {ductal}
    m = _build_cell_type_token_set_map(names)
    assert frozenset({"ductal"}) not in m


# ---------------------------------------------------------------------------
# fix_cell_type_references (schema-driven integration)
# ---------------------------------------------------------------------------

def _has_schema():
    try:
        from schema_loader import get_valid_property_values
        v = get_valid_property_values()
        info = v.get("node_properties", {}).get("anatomical_structure", {}).get("name", {})
        return bool(info.get("values") or info.get("examples"))
    except Exception:
        return False


import pytest  # noqa: E402

requires_schema = pytest.mark.skipif(not _has_schema(), reason="anatomical_structure schema not available")


@requires_schema
def test_fix_resolves_bare_ductal():
    out = fix_cell_type_references(
        'MATCH (g:gene)-[r:gene_enriched_in]->(ct:anatomical_structure) WHERE ct.name = "ductal" RETURN nodes'
    )
    assert 'ct.name = "ductal cell"' in out


@requires_schema
def test_fix_resolves_muc5b_ductal():
    out = fix_cell_type_references(
        'MATCH (g:gene)-[r:gene_enriched_in]->(ct:anatomical_structure) WHERE ct.name = "MUC5B+Ductal" RETURN nodes'
    )
    assert 'ct.name = "pancreatic ductal cell MUC5B+"' in out


@requires_schema
def test_fix_corrects_name_in_and_with_id():
    # The 'AND ct.id = ...' previously nullified a correct id with a wrong name guess;
    # correcting the name makes both conjuncts valid.
    out = fix_cell_type_references(
        'MATCH (g:gene)-[r:gene_enriched_in]->(ct:anatomical_structure) '
        'WHERE ct.name = "MUC5B+ Ductal Cell" AND ct.id = "CL_0002079_MUC5B" RETURN nodes'
    )
    assert 'ct.name = "pancreatic ductal cell MUC5B+"' in out
    assert 'ct.id = "CL_0002079_MUC5B"' in out


@requires_schema
def test_fix_leaves_non_cell_type_values_untouched():
    out = fix_cell_type_references('MATCH (g:gene) WHERE g.name = "CFTR" RETURN g')
    assert 'g.name = "CFTR"' in out
