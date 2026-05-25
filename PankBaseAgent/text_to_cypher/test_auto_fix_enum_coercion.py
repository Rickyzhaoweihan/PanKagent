"""Tests for the schema-slice-driven enum value coercion in auto_fix_cypher."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from src.cypher_validator import auto_fix_cypher, fix_enum_value_coercion
from src.schema_loader import get_property_enum_map


def test_upordownregulation_lowercase_gets_coerced():
    """`r.UpOrDownRegulation = "upregulated"` -> `"Upregulated in T1D"`."""
    cypher = (
        'MATCH (g:gene)-[r:T1D_DEG_in]->(c:anatomical_structure) '
        'WHERE r.UpOrDownRegulation = "upregulated" RETURN g, r, c'
    )
    emap = get_property_enum_map(["gene", "anatomical_structure"], ["T1D_DEG_in"])
    fixed, msgs = fix_enum_value_coercion(cypher, emap)
    assert 'r.UpOrDownRegulation = "Upregulated in T1D"' in fixed
    assert any("UpOrDownRegulation" in m and "Upregulated in T1D" in m for m in msgs)


def test_anatomical_structure_short_form_coerces_to_canonical():
    """`c.name = "Beta Cell"` -> canonical bare lowercase form `'beta cell'`
    that actually exists in the live KG (the legacy long UBERON form
    `'type B pancreatic cell (beta cell)'` no longer exists after the KG
    refresh)."""
    cypher = (
        'MATCH (g:gene)-[r:T1D_DEG_in]->(c:anatomical_structure) '
        'WHERE c.name = "Beta Cell" RETURN g, r, c'
    )
    emap = get_property_enum_map(["gene", "anatomical_structure"], ["T1D_DEG_in"])
    fixed, _ = auto_fix_cypher(cypher, schema_slice=emap)
    assert '"beta cell"' in fixed
    assert '"Beta Cell"' not in fixed
    # Ensure we don't regress to the old dead canonical
    assert "type B pancreatic cell" not in fixed


def test_anatomical_structure_already_canonical_left_alone():
    """A query already using the canonical `'beta cell'` form must NOT be
    rewritten."""
    cypher = (
        'MATCH (g:gene)-[r:T1D_DEG_in]->(c:anatomical_structure) '
        'WHERE c.name = "beta cell" RETURN g, r, c'
    )
    fixed, _ = auto_fix_cypher(cypher)
    assert '"beta cell"' in fixed


def test_exact_enum_value_left_alone():
    """An already-canonical enum value must NOT be modified."""
    cypher = (
        'MATCH (g:gene)-[r:T1D_DEG_in]->(c:anatomical_structure) '
        'WHERE r.UpOrDownRegulation = "Upregulated in T1D" RETURN g, r, c'
    )
    emap = get_property_enum_map(["gene", "anatomical_structure"], ["T1D_DEG_in"])
    fixed, msgs = fix_enum_value_coercion(cypher, emap)
    assert fixed == cypher
    assert msgs == []


def test_non_enum_property_left_alone():
    """`Log2FoldChange` is a Float (no enum) — must not be touched."""
    cypher = (
        'MATCH (g:gene)-[r:T1D_DEG_in]->(c:anatomical_structure) '
        'WHERE r.Log2FoldChange > 1.5 RETURN g, r, c'
    )
    emap = get_property_enum_map(["gene", "anatomical_structure"], ["T1D_DEG_in"])
    fixed, msgs = fix_enum_value_coercion(cypher, emap)
    assert fixed == cypher
    assert msgs == []


def test_unknown_variable_left_alone():
    """If the var doesn't appear in any MATCH pattern, leave it alone."""
    cypher = 'RETURN x.foo = "bar" AS test'  # synthetic — no MATCH
    emap = get_property_enum_map([], ["T1D_DEG_in"])
    fixed, msgs = fix_enum_value_coercion(cypher, emap)
    assert fixed == cypher
    assert msgs == []


def test_backward_compat_no_slice_is_noop_for_coercion():
    """auto_fix_cypher without schema_slice must still produce the same legacy
    fixes (cell-type ref, property name case) but no enum coercion entries."""
    cypher = (
        'MATCH (g:gene)-[r:T1D_DEG_in]->(c:anatomical_structure) '
        'WHERE r.UpOrDownRegulation = "upregulated" RETURN g, r, c'
    )
    fixed_no_slice, fixes_no_slice = auto_fix_cypher(cypher)
    # Without the slice, no coercion message for UpOrDownRegulation
    assert not any("Coerced" in f and "UpOrDownRegulation" in f for f in fixes_no_slice)
    # The literal stays uncanonical
    assert '"upregulated"' in fixed_no_slice


def test_with_slice_emits_coercion_message():
    """auto_fix_cypher WITH a slice must emit the coercion as a 'fixes' entry."""
    cypher = (
        'MATCH (g:gene)-[r:T1D_DEG_in]->(c:anatomical_structure) '
        'WHERE r.UpOrDownRegulation = "upregulated" RETURN g, r, c'
    )
    emap = get_property_enum_map(["gene", "anatomical_structure"], ["T1D_DEG_in"])
    fixed, fixes = auto_fix_cypher(cypher, schema_slice=emap)
    assert any("UpOrDownRegulation" in f and "Upregulated in T1D" in f for f in fixes)
    assert '"Upregulated in T1D"' in fixed
