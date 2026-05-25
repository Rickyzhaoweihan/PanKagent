#!/usr/bin/env python3
"""
schema_loader.py
Fast, memoised accessor for the Neo4j schema JSON and optional hints.
Also provides a simplified, distilled view of the schema for LLM prompting.

Usage
-----
from schema_loader import get_schema, get_schema_hints, get_simplified_schema, get_minimal_schema_for_llm
schema = get_schema()                # full dict, loaded once per process
hints = get_schema_hints()           # dict or None, loaded once per process
simple = get_simplified_schema()     # slim dict (labels, properties, relationships)
minimal = get_minimal_schema_for_llm()  # ultra-compact string for small models
"""

from __future__ import annotations
import json, os
from pathlib import Path
from typing import Dict, Any, Optional

# Optional dotenv support - gracefully handle if not installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, continue without it
# Schema path: PanKgraph ADA schema verified against local Neo4j (port 8687)
_SCHEMA_PATH = (Path(__file__).resolve().parent.parent / "data" / "input" / "neo4j_schema_ada.json")

_HINTS_PATH = (Path(__file__).resolve().parent.parent / "data" / "input" / "schema_hints.json")

_VALID_VALUES_PATH = (Path(__file__).resolve().parent.parent / "data" / "input" / "valid_property_values.json")


# ── internal cache --------------------------------------------------------
_cached_schema: Dict[str, Any] | None = None
_cached_hints: Dict[str, Any] | None = None
_cached_simple: Dict[str, Any] | None = None
_cached_minimal: str | None = None
_cached_valid_values: Dict[str, Any] | None = None
_property_cache: Dict[str, str] = {}
_hints_loaded: bool = False
_valid_values_loaded: bool = False

def get_schema() -> Dict[str, Any]:
    """Return the full Neo4j schema as a JSON dict (cached)."""
    global _cached_schema
    if _cached_schema is None:
        with _SCHEMA_PATH.open() as f:
            data = json.load(f)
            # Handle nested "knowledge_graph_schema" wrapper (from RL implementation format)
            if "knowledge_graph_schema" in data:
                _cached_schema = data["knowledge_graph_schema"]
            else:
                _cached_schema = data
    return _cached_schema

def get_schema_hints() -> Optional[Dict[str, Any]]:
    """Return schema hints/clarifications if available (cached)."""
    global _cached_hints, _hints_loaded
    if not _hints_loaded:
        _hints_loaded = True
        if _HINTS_PATH and _HINTS_PATH.exists():
            with _HINTS_PATH.open() as f:
                _cached_hints = json.load(f)
    return _cached_hints

def get_valid_property_values() -> Optional[Dict[str, Any]]:
    """Return valid property values for constrained properties (cached)."""
    global _cached_valid_values, _valid_values_loaded
    if not _valid_values_loaded:
        _valid_values_loaded = True
        if _VALID_VALUES_PATH and _VALID_VALUES_PATH.exists():
            with _VALID_VALUES_PATH.open() as f:
                _cached_valid_values = json.load(f)
    return _cached_valid_values

def get_simplified_schema() -> Dict[str, Any]:
    """Return a distilled schema aligned with the new schema format (node_types, edge_types)."""
    global _cached_simple
    if _cached_simple is None:
        schema = get_schema()

        # Extract node labels and their property names
        nodes = {
            label.split(";")[-1]: list(spec.get("properties", {}).keys())
            for label, spec in schema.get("node_types", {}).items()
        }

        # Extract relationships and source/target node types
        relationships = {
            rel.split(";")[-1]: {
                "source": spec.get("source_node_type", ""),
                "target": spec.get("target_node_type", "")
            }
            for rel, spec in schema.get("edge_types", {}).items()
        }

        # Define preferred lookup keys for quick name matching
        preferred_lookup = {
            "gene": ["name", "id"],
            "anatomical_structure": ["name", "category"],
            "disease": ["name", "id"],
            "gene_ontology": ["name", "id"],
            "kegg": ["name", "id"],
            "reactome": ["name", "id"],
            "snv": ["id"],
            "OCR_peak": ["id"],
            "donor": ["id", "diabetes_type"],
        }

        # Add brief contextual notes for special node types and relationships
        Notes = {
            "gene": "Gene nodes correspond to Ensembl/GENCODE identifiers. Query by name or id.",
            "snv": "Variant nodes (SNV, deletion, insertion, indel). Query by id (rsID).",
            "anatomical_structure": "Replaces old cell_type. Includes tissues, regions, and cell types. Query by name.",
            "OCR_peak": "Open chromatin region peaks with genomic coordinates (chr, start_loc, end_loc).",
            "part_of_QTL_signal": (
                "part_of_QTL_signal connects variants to genes (eQTL). "
                "Use pattern: (s:snv)-[:part_of_QTL_signal]->(g:gene). Properties: pip, tissue_name, gene_name."
            ),
            "function_annotation_unified": (
                "GO terms, KEGG pathways, and Reactome pathways all share the SAME edge "
                "type: `function_annotation` (no semicolon, no backtick escape). "
                "Distinguish them by the target node label: "
                "(g:gene)-[r:function_annotation]->(go:gene_ontology) for GO, "
                "->(k:kegg) for KEGG, ->(rp:reactome) for Reactome. The edge's "
                "`data_source` property is 'Ensembl' (GO), 'KEGG', or 'Reactome'."
            ),
        }

        _cached_simple = {
            "NodeLabels": list(nodes.keys()),
            "NodeProperties": nodes,
            "Relationships": relationships,
            "PreferredLookup": preferred_lookup,
            "Notes": Notes
        }

    return _cached_simple

def get_minimal_schema_for_llm() -> str:
    """Return ultra-compact schema string optimized for small models (9B with 8k context).
    
    This function drastically reduces token usage by:
    - Using compact string format instead of JSON
    - Including only critical properties (name, id for lookup)
    - Listing only filterable edge properties
    - Providing brief usage notes
    
    Returns a string consuming ~300-400 tokens vs ~2000+ for full schema.
    """
    global _cached_minimal
    if _cached_minimal is None:
        schema = get_schema()
        
        # Define which node types have which critical properties
        node_props = {
            "gene": ["name", "id"],
            "anatomical_structure": ["name", "category"],
            "disease": ["name", "id"],
            "gene_ontology": ["name", "id"],
            "kegg": ["name", "id"],
            "reactome": ["name", "id"],
            "snv": ["id"],
            "OCR_peak": ["id", "chr", "start_loc", "end_loc"],
            "donor": ["id", "diabetes_type", "derived_diabetes_status", "t1d_stage", "aab_state", "hla_status", "age", "gender", "Race", "bmi", "hba1c_percentage", "c_peptide_ng_ml"],
        }

        # Build compact node list
        node_parts = []
        for label, spec in schema.get("node_types", {}).items():
            simple_label = label.split(";")[-1]
            props = node_props.get(simple_label, [])
            if props:
                node_parts.append(f"{simple_label}({','.join(props)})")
            else:
                node_parts.append(simple_label)

        # Define critical filterable properties for edges
        edge_filter_props = {
            "T1D_DEG_in": ["Log2FoldChange", "UpOrDownRegulation", "P_value", "Adjusted_P_value"],
            "gene_detected_in": ["mean_donor_logCPM", "median_pct_cells_expressing", "cell_type"],
            "gene_enriched_in": ["log2FoldChange", "padj", "cell_type_label", "rank_in_cell_type"],
            "part_of_QTL_signal": ["pip", "slope", "nominal_p", "tissue_name", "gene_name"],
            "gene_activity_score_in": ["OCR_GeneActivityScore_mean", "type_1_diabetes__OCR_GeneActivityScore_mean"],
            "part_of_GWAS_signal": ["pip", "p_value", "locus_name", "lead_status"],
        }

        # Build compact edge list with source→target and critical properties
        edge_parts = []
        for rel, spec in schema.get("edge_types", {}).items():
            source = spec.get("source_node_type", "?")
            target = spec.get("target_node_type", "?")

            # For relationship names with semicolons, use backtick syntax hint
            display_rel = rel

            # Add filterable properties if defined
            filter_props = edge_filter_props.get(rel, [])
            if filter_props:
                edge_parts.append(f"{display_rel}({source}→{target})[{','.join(filter_props)}]")
            else:
                edge_parts.append(f"{display_rel}({source}→{target})")

        # Build the compact schema string
        nodes_str = "Nodes: " + ", ".join(node_parts)
        edges_str = "Edges: " + ", ".join(edge_parts)

        # Add brief usage notes
        notes = """
Notes:
- Lookup nodes: gene.name='CFTR', disease.name='type 1 diabetes', anatomical_structure.name='pancreas'
- Ontology/pathway lookups all use the unified `function_annotation` edge (no semicolon, no backtick escape). Distinguish by target node label: `(g:gene)-[r:function_annotation]->(go:gene_ontology)` for GO; `->(k:kegg)` for KEGG; `->(rp:reactome)` for Reactome.
- Filter T1D DEG: r.UpOrDownRegulation='Upregulated in T1D' or 'Downregulated in T1D'
- SNP-gene QTL: (s:snv)-[r:part_of_QTL_signal]->(g:gene), filter r.pip>0.5, r.tissue_name='Islet'
- Disease name: ALWAYS use 'type 1 diabetes' (lowercase, full spelling, not T1D)
- Node labels: gene (not Gene), snv (not snp), OCR_peak (not OCR), anatomical_structure (not cell_type)
- anatomical_structure replaces old cell_type. Examples: query by name or by ID (e.g., CL_0000169)
- Donor queries: use exact property values. diabetes_type='Diabetes (Type I)' for T1D donors, 'Diabetes (Type II)' for T2D, 'Control Without Diabetes' for controls. aab_state uses CONTAINS (e.g. d.aab_state CONTAINS 'GADA positive'). hla_status one of: 'DR3/DR4','DR3/X','DR4/X','X/DR3','X/DR4','X/X'. Example: MATCH (d:donor) WHERE d.diabetes_type = 'Diabetes (Type I)' WITH collect(DISTINCT d) AS nodes, [] AS edges RETURN nodes, edges;"""

        _cached_minimal = nodes_str + "\n" + edges_str + "\n" + notes
    
    return _cached_minimal


def extract_entities_from_cypher(cypher: str) -> Dict[str, list]:
    """
    Extract node labels and relationship types from Cypher query.
    
    Args:
        cypher: The Cypher query string
        
    Returns:
        {
            'node_labels': ['gene', 'anatomical_structure', ...],
            'relationship_types': ['T1D_DEG_in', 'physical_interaction', ...]
        }
    """
    import re

    node_labels = set()
    relationship_types = set()

    # Extract node labels from patterns like (var:Label) or (:Label)
    node_patterns = re.findall(r'\([^)]*:(\w+)[^)]*\)', cypher)
    for label in node_patterns:
        # Handle compound labels like "ontology;gene_ontology" -> extract last part
        simple_label = label.split(';')[-1]
        node_labels.add(simple_label)

    # Extract relationship types from patterns like [var:Type] or [:Type]
    rel_patterns = re.findall(r'\[[^]]*:(\w+)[^]]*\]', cypher)
    for rel_type in rel_patterns:
        # Handle compound types like "function_annotation;GO" -> extract last part
        simple_type = rel_type.split(';')[-1]
        relationship_types.add(simple_type)
    
    return {
        'node_labels': sorted(list(node_labels)),
        'relationship_types': sorted(list(relationship_types))
    }


def get_detailed_properties(node_labels: list, relationship_types: list) -> str:
    """
    Get detailed property information for specific entities.
    
    Args:
        node_labels: List of node labels to get properties for
        relationship_types: List of relationship types to get properties for
        
    Returns:
        Formatted string with detailed properties for the specified entities
    """
    global _property_cache
    
    # Check cache first
    cache_key = f"{sorted(node_labels)}_{sorted(relationship_types)}"
    if cache_key in _property_cache:
        return _property_cache[cache_key]
    
    schema = get_schema()
    valid_values = get_valid_property_values()
    result_lines = ["Detailed Properties for Query Entities:"]
    
    # Get node properties
    if node_labels:
        result_lines.append("\nNode Properties:")
        for label in node_labels:
            # Find matching node type in schema
            matching_key = None
            for key in schema.get("node_types", {}).keys():
                if key.split(";")[-1] == label:
                    matching_key = key
                    break
            
            if matching_key:
                node_spec = schema["node_types"][matching_key]
                properties = node_spec.get("properties", {})
                
                if properties:
                    result_lines.append(f"\n  {label}:")
                    for prop_name, prop_type in properties.items():
                        result_lines.append(f"    - {prop_name} ({prop_type})")
                        
                        # Add valid values if available
                        if valid_values and label in valid_values.get("node_properties", {}):
                            prop_values = valid_values["node_properties"][label].get(prop_name)
                            if prop_values:
                                result_lines.append(f"      Valid values: {prop_values['values']}")
                                if prop_values.get('note'):
                                    result_lines.append(f"      Note: {prop_values['note']}")
                else:
                    result_lines.append(f"\n  {label}: (no properties)")
    
    # Get relationship properties
    if relationship_types:
        result_lines.append("\nRelationship Properties:")
        for rel_type in relationship_types:
            # Find matching edge type in schema
            matching_key = None
            for key in schema.get("edge_types", {}).keys():
                if key.split(";")[-1] == rel_type:
                    matching_key = key
                    break
            
            if matching_key:
                edge_spec = schema["edge_types"][matching_key]
                properties = edge_spec.get("properties", {})
                source = edge_spec.get("source_node_type", "?")
                target = edge_spec.get("target_node_type", "?")
                
                if properties:
                    result_lines.append(f"\n  {rel_type} ({source}→{target}):")
                    for prop_name, prop_type in properties.items():
                        result_lines.append(f"    - {prop_name} ({prop_type})")
                        
                        # Add valid values if available
                        if valid_values and rel_type in valid_values.get("relationship_properties", {}):
                            prop_values = valid_values["relationship_properties"][rel_type].get(prop_name)
                            if prop_values:
                                result_lines.append(f"      Valid values: {prop_values['values']}")
                                if prop_values.get('note'):
                                    result_lines.append(f"      Note: {prop_values['note']}")
                else:
                    result_lines.append(f"\n  {rel_type} ({source}→{target}): (no properties)")
    
    result = '\n'.join(result_lines)

    # Cache the result
    _property_cache[cache_key] = result

    return result


# ---------------------------------------------------------------------------
# Lazy entity-aware schema retrieval (added for the 2-stage cypher flow)
# ---------------------------------------------------------------------------

_enum_map_cache: dict[tuple, Dict[str, Any]] = {}


def _resolve_compound_key(schema_section: Dict[str, Any], simple_label: str) -> Optional[str]:
    """Find the compound-label key in a schema section whose last token matches."""
    for key in schema_section.keys():
        if key.split(";")[-1] == simple_label:
            return key
    return None


def get_property_enum_map(
    node_labels: list, relationship_types: list
) -> Dict[str, Dict[str, Dict[str, list]]]:
    """Return a machine-readable enum map for the specified entities.

    Only properties whose schema entry carries a ``values`` array (i.e. true
    enums after the resync's ≤ ENUM_THRESHOLD filter) are included. Properties
    with only ``examples`` (high-cardinality free text) are skipped — they
    cannot be coerced safely.

    Output shape::

        {
          "node_properties": {
            "<simple_label>": {"<prop>": [value, value, ...], ...},
            ...
          },
          "edge_properties": {
            "<simple_rel_type>": {"<prop>": [value, value, ...], ...},
            ...
          }
        }

    The keys use the *simple* label/rel name (last `;`-token), matching what
    `extract_entities_from_cypher` returns.
    """
    cache_key = (tuple(sorted(node_labels)), tuple(sorted(relationship_types)))
    if cache_key in _enum_map_cache:
        return _enum_map_cache[cache_key]

    schema = get_schema()
    node_section = schema.get("node_types", {})
    edge_section = schema.get("edge_types", {})

    def _extract_enums(properties: Dict[str, Any]) -> Dict[str, list]:
        out: Dict[str, list] = {}
        for prop_name, prop_spec in properties.items():
            if isinstance(prop_spec, dict) and prop_spec.get("values"):
                values = prop_spec["values"]
                if isinstance(values, list) and values:
                    out[prop_name] = list(values)
        return out

    node_out: Dict[str, Dict[str, list]] = {}
    for simple in node_labels:
        compound = _resolve_compound_key(node_section, simple)
        if not compound:
            continue
        props = node_section[compound].get("properties", {})
        enums = _extract_enums(props)
        if enums:
            node_out[simple] = enums

    edge_out: Dict[str, Dict[str, list]] = {}
    for simple in relationship_types:
        compound = _resolve_compound_key(edge_section, simple)
        if not compound:
            continue
        props = edge_section[compound].get("properties", {})
        enums = _extract_enums(props)
        if enums:
            edge_out[simple] = enums

    result = {"node_properties": node_out, "edge_properties": edge_out}
    _enum_map_cache[cache_key] = result
    return result


def get_detailed_schema_for_cypher(draft_cypher: str) -> tuple[str, Dict[str, Any]]:
    """Lazy slice: parse `draft_cypher`, then pull detailed schema for just
    the entities it touched.

    Returns ``(detailed_text, enum_map)``:
      - ``detailed_text`` is the human-readable text form produced by
        ``get_detailed_properties`` — suitable for inclusion in a refinement
        LLM prompt.
      - ``enum_map`` is the machine-readable form produced by
        ``get_property_enum_map`` — consumed by ``auto_fix_cypher`` for
        deterministic enum-value coercion.

    Both views describe the SAME entity set, derived from the draft Cypher's
    node-label and relationship-type usages via
    ``extract_entities_from_cypher``.
    """
    entities = extract_entities_from_cypher(draft_cypher)
    labels = entities.get("node_labels", [])
    rels = entities.get("relationship_types", [])
    detailed_text = get_detailed_properties(labels, rels)
    enum_map = get_property_enum_map(labels, rels)
    return detailed_text, enum_map
