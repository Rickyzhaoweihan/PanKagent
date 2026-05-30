#!/usr/bin/env python3
"""
cypher_validator.py
Comprehensive Cypher query validation and auto-fixing.

Combines validation scoring with automatic query repair using hard-coded rules.

Validation Checks:
   - WITH clause structure (collect() usage, DISTINCT placement)
   - Relationship variable naming
   - Required return format (nodes, edges)
   - DISTINCT in collect() calls
   - Disease naming conventions
   - Property validity against schema
   - Property value constraints
   - Relationship direction validation

Auto-Fix Capabilities (from RL implementation):
   - Single → double quote conversion (CRITICAL for API gateway)
   - Relationship variable addition
   - Undefined variable fixing (scope-aware)
   - Multi-MATCH query handling
   - Entity value normalization (diseases, cell types)
   - Property name case fixing
   - Result LIMIT addition (prevents timeouts)
   - Collection completeness
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Schema and Entity Data Loading (cached)
# ============================================================================

_cached_entity_samples: Dict[str, Any] | None = None

# Path to entity samples (relative to this file)
_ENTITY_SAMPLES_PATH = Path(__file__).resolve().parent.parent / "data" / "input" / "entity_samples.json"


def load_entity_samples() -> Dict[str, Any]:
    """Load entity samples for value validation."""
    global _cached_entity_samples
    if _cached_entity_samples is None:
        if _ENTITY_SAMPLES_PATH.exists():
            with open(_ENTITY_SAMPLES_PATH, 'r') as f:
                _cached_entity_samples = json.load(f)
        else:
            logger.warning(f"Entity samples file not found: {_ENTITY_SAMPLES_PATH}")
            _cached_entity_samples = {}
    return _cached_entity_samples


def clear_entity_cache():
    """Clear cached entity samples."""
    global _cached_entity_samples
    _cached_entity_samples = None


# ============================================================================
# Schema-Based Helpers (using schema_loader.py)
# ============================================================================

def get_relationship_directions() -> Dict[str, Dict[str, str]]:
    """
    Get expected source/target node types for each relationship type.
    
    Returns:
        {rel_type: {'source': 'node_type', 'target': 'node_type'}, ...}
    """
    try:
        from .schema_loader import get_schema
    except ImportError:
        from schema_loader import get_schema

    schema = get_schema()
    directions = {}
    
    for rel_key, rel_spec in schema.get("edge_types", {}).items():
        source = rel_spec.get("source_node_type", "")
        target = rel_spec.get("target_node_type", "")
        # Use full key as relationship type (e.g., "function_annotation;GO")
        directions[rel_key] = {
            'source': source,
            'target': target
        }

    return directions


def get_valid_node_properties() -> Dict[str, Set[str]]:
    """
    Get valid properties for each node type.
    
    Returns:
        {node_label: {'prop1', 'prop2', ...}, ...}
    """
    try:
        from .schema_loader import get_schema
    except ImportError:
        from schema_loader import get_schema

    schema = get_schema()
    valid_props = {}
    
    for node_key, node_spec in schema.get("node_types", {}).items():
        node_label = node_key.split(";")[-1]
        props = set(node_spec.get("properties", {}).keys())
        valid_props[node_label] = props
    
    return valid_props


def get_valid_edge_properties() -> Dict[str, Set[str]]:
    """
    Get valid properties for each edge type.
    
    Returns:
        {edge_type: {'prop1', 'prop2', ...}, ...}
    """
    try:
        from .schema_loader import get_schema
    except ImportError:
        from schema_loader import get_schema

    schema = get_schema()
    valid_props = {}
    
    for edge_key, edge_spec in schema.get("edge_types", {}).items():
        props = set(edge_spec.get("properties", {}).keys())
        # Use full key (e.g., "function_annotation;GO")
        valid_props[edge_key] = props
    
    return valid_props


def get_valid_entity_values() -> Dict[str, Set[str]]:
    """
    Get valid entity values from entity samples.
    
    Returns:
        {
            'gene_names': {'INS', 'CFTR', ...},
            'disease_names': {'type 1 diabetes', ...},
            'cell_type_names': {'Beta Cell', 'Alpha Cell', ...},
            'snp_ids': {'rs7903146', ...},
            'go_names': {'protein binding', ...}
        }
    """
    samples = load_entity_samples()
    
    valid_values = {
        'gene_names': set(),
        'disease_names': set(),
        'anatomical_structure_names': set(),
        'snv_ids': set(),
        'go_names': set()
    }

    # Extract gene names
    for gene in samples.get('genes', []):
        if gene.get('name'):
            valid_values['gene_names'].add(gene['name'])

    # Extract disease names
    for disease in samples.get('diseases', []):
        if disease.get('name'):
            valid_values['disease_names'].add(disease['name'])

    # Extract anatomical structure names (replaces cell_type)
    for item in samples.get('anatomical_structures', samples.get('cell_types', [])):
        if item.get('name'):
            valid_values['anatomical_structure_names'].add(item['name'])

    # Extract variant IDs (snv replaces snp)
    for snv in samples.get('snvs', samples.get('snps', [])):
        if snv.get('id'):
            valid_values['snv_ids'].add(snv['id'])

    # Extract GO term names
    for go in samples.get('gene_ontology', []):
        if go.get('name'):
            valid_values['go_names'].add(go['name'])
    
    return valid_values


# ============================================================================
# Query Analysis Helpers
# ============================================================================

def extract_matched_variables(cypher: str) -> Tuple[Set[str], Set[str]]:
    """
    Extract all node and relationship variables from MATCH clauses.
    
    Returns:
        (node_variables, relationship_variables)
    """
    node_vars = set()
    rel_vars = set()
    
    # Find all MATCH clauses
    match_patterns = re.findall(
        r'\bMATCH\b(.*?)(?=\bWHERE\b|\bWITH\b|\bRETURN\b|\bMATCH\b|$)',
        cypher, re.IGNORECASE | re.DOTALL
    )
    
    for pattern in match_patterns:
        # Find node variables: (var:Label) or (var {props}) or (var)
        node_matches = re.findall(r'\((\w+)(?::\w+)?[^)]*\)', pattern)
        node_vars.update(node_matches)
        
        # Find relationship variables: [var:Type] or [var {props}] or [var]
        rel_matches = re.findall(r'\[(\w+)(?::\w+)?[^]]*\]', pattern)
        rel_vars.update(rel_matches)
    
    return node_vars, rel_vars


def extract_collected_variables(cypher: str) -> Tuple[Set[str], Set[str]]:
    """
    Extract variables collected in WITH clause for nodes and edges.
    
    Returns:
        (collected_nodes, collected_edges)
    """
    collected_nodes = set()
    collected_edges = set()
    
    # Find WITH clause (between WITH and RETURN)
    with_match = re.search(r'\bWITH\b(.*?)\bRETURN\b', cypher, re.IGNORECASE | re.DOTALL)
    if not with_match:
        return collected_nodes, collected_edges
    
    with_clause = with_match.group(1)
    
    # Find what's collected for nodes (... AS nodes)
    nodes_pattern = re.search(r'(.*?)\s+AS\s+nodes', with_clause, re.IGNORECASE | re.DOTALL)
    if nodes_pattern:
        nodes_expr = nodes_pattern.group(1)
        collected = re.findall(r'collect\s*\(\s*(?:DISTINCT\s+)?(\w+)\s*\)', nodes_expr, re.IGNORECASE)
        collected_nodes.update(collected)
    
    # Find what's collected for edges (... AS edges)
    edges_pattern = re.search(r'(.*?)\s+AS\s+edges', with_clause, re.IGNORECASE | re.DOTALL)
    if edges_pattern:
        edges_expr = edges_pattern.group(1)
        collected = re.findall(r'collect\s*\(\s*(?:DISTINCT\s+)?(\w+)\s*\)', edges_expr, re.IGNORECASE)
        collected_edges.update(collected)
    
    return collected_nodes, collected_edges


def extract_node_labels_and_rel_types(cypher: str) -> Tuple[Set[str], Set[str]]:
    """
    Extract node labels and relationship types from query.
    
    Returns:
        (node_labels, relationship_types)
    """
    node_labels = set()
    rel_types = set()
    
    # Extract node labels: (var:Label)
    node_patterns = re.findall(r'\([^)]*:(\w+)[^)]*\)', cypher)
    for label in node_patterns:
        node_labels.add(label.split(';')[-1])
    
    # Extract relationship types: [var:Type]
    rel_patterns = re.findall(r'\[[^]]*:(\w+)[^]]*\]', cypher)
    for rel_type in rel_patterns:
        rel_types.add(rel_type.split(';')[-1])
    
    return node_labels, rel_types


# ============================================================================
# Query Complexity Detection
# ============================================================================

def is_multi_match_query(cypher: str) -> bool:
    """
    Detect if query has multiple MATCH clauses.
    
    Multi-MATCH queries like:
        MATCH ... WITH ... MATCH ... RETURN ...
    
    These are valid Cypher but require different handling in auto-fix
    to avoid breaking their structure.
    
    Returns:
        True if query has multiple MATCH clauses
    """
    match_count = len(re.findall(r'\bMATCH\b', cypher, re.IGNORECASE))
    return match_count > 1


def is_simple_query(cypher: str) -> bool:
    """
    Detect if this is a simple single-MATCH query.
    
    Simple queries have:
    - One MATCH clause
    - One WITH clause (at the end, before RETURN)
    - Standard nodes/edges collection pattern
    
    These are safe for all auto-fix operations.
    """
    match_count = len(re.findall(r'\bMATCH\b', cypher, re.IGNORECASE))
    return match_count == 1


# ============================================================================
# Individual Fix Functions
# ============================================================================

def fix_single_quotes_to_double(cypher: str) -> str:
    """
    Convert single quotes to double quotes for string values in Cypher.
    
    CRITICAL FIX: The Neo4j API gateway strips single quotes from queries,
    causing syntax errors like:
        {name: 'Delta Cell'} → {name: Delta Cell} → ERROR!
        
    This fix converts:
        {name: 'Delta Cell'} → {name: "Delta Cell"}
        {id: 'rs123456'} → {id: "rs123456"}
        .name = 'value' → .name = "value"
    
    Note: This must be applied EARLY in the fix chain before other fixes
    that might depend on the quote style.
    """
    if not cypher:
        return cypher
    
    # Pattern 1: Fix {property: 'value'} patterns
    # Matches: {name: 'value'}, {id: 'value'}, etc.
    def fix_property_value(match):
        prop = match.group(1)
        value = match.group(2)
        return f'{{{prop}: "{value}"}}'
    
    fixed = re.sub(
        r"\{(\w+):\s*'([^']+)'\}",
        fix_property_value,
        cypher
    )
    
    # Pattern 2: Fix .property = 'value' patterns  
    # Matches: .name = 'value', .id = 'value', etc.
    def fix_assignment_value(match):
        prop = match.group(1)
        value = match.group(2)
        return f'.{prop} = "{value}"'
    
    fixed = re.sub(
        r"\.(\w+)\s*=\s*'([^']+)'",
        fix_assignment_value,
        fixed
    )
    
    # Pattern 3: Fix standalone 'value' in WHERE clauses
    # e.g., WHERE n.name = 'value' (if not caught by pattern 2)
    # More conservative - only fix known patterns
    def fix_where_value(match):
        prefix = match.group(1)
        value = match.group(2)
        return f'{prefix}"{value}"'
    
    fixed = re.sub(
        r"(WHERE\s+\w+\.\w+\s*=\s*)'([^']+)'",
        fix_where_value,
        fixed,
        flags=re.IGNORECASE
    )
    
    return fixed


def fix_relationship_variables(cypher: str) -> str:
    """
    Add missing variable names to relationships.

    Fixes: -[:TYPE]- → -[r:TYPE]-

    Skips relationships inside ``EXISTS { ... }`` / ``COUNT { ... }`` subqueries
    — those variables are scoped to the subquery and don't need (and shouldn't
    have) outer-visible names. Naming them only causes outer-scope WITH/RETURN
    references to fail with "Variable not defined".
    """
    # Identify EXISTS/COUNT subquery spans so we can leave their interior alone.
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r'\b(EXISTS|COUNT)\s*\{[^{}]*\}', cypher, flags=re.IGNORECASE):
        spans.append((m.start(), m.end()))

    def in_subquery(pos: int) -> bool:
        return any(a <= pos < b for a, b in spans)

    # Counter for unique variable names
    var_counter = [0]

    def replacement(match):
        rel_content = match.group(1).strip()
        # Skip if this relationship lives inside an EXISTS/COUNT subquery.
        if in_subquery(match.start()):
            return match.group(0)

        # If it starts with : (no variable name), add one
        if rel_content.startswith(':'):
            var_counter[0] += 1
            var_name = f"r{var_counter[0]}"
            return f"-[{var_name}{rel_content}]-"

        # If empty, add variable and placeholder type
        if not rel_content:
            var_counter[0] += 1
            return f"-[r{var_counter[0]}]-"

        return match.group(0)  # No change needed

    # Find and fix relationship patterns
    fixed = re.sub(r'-\[(.*?)\]-', replacement, cypher)

    return fixed


def fix_undefined_collected_variables(cypher: str) -> str:
    """
    Fix references to undefined variables in collect() statements.
    
    Works for both simple and multi-MATCH queries by analyzing scope.
    
    For simple queries:
        MATCH (g:gene)-[r1:TYPE]->(d:disease)
        WITH collect(g) AS nodes, collect(r) AS edges  ← r undefined, becomes r1
    
    For multi-MATCH queries:
        MATCH (a)-[r1:TYPE1]->(b)
        WITH collect(a) AS nodes1, collect(r) AS edges1  ← r becomes r1
        MATCH (c)-[r2:TYPE2]->(d)
        WITH nodes1 + collect(c) AS n2, edges1 + collect(r) AS e2  ← r becomes r2
    """
    # Check if multi-MATCH
    is_multi = is_multi_match_query(cypher)
    
    if is_multi:
        return _fix_undefined_vars_multi_match(cypher)
    else:
        return _fix_undefined_vars_simple(cypher)


def _fix_undefined_vars_simple(cypher: str) -> str:
    """Fix undefined variables for simple single-MATCH queries."""
    # Get all defined variables
    matched_nodes, matched_rels = extract_matched_variables(cypher)
    all_matched = matched_nodes | matched_rels
    
    # Find all variables referenced in collect() statements
    collected_vars = re.findall(r'collect\s*\(\s*(?:DISTINCT\s+)?(\w+)\s*\)', cypher, re.IGNORECASE)
    
    # Identify undefined collected variables
    undefined_vars = [v for v in collected_vars if v not in all_matched]
    
    if not undefined_vars:
        return cypher
    
    fixed = cypher
    
    for undef_var in undefined_vars:
        # Check if it looks like a relationship variable
        is_rel_like = undef_var.lower() in ['r', 'rel', 'r1', 'r2', 'r3', 'edge', 'e']
        
        if is_rel_like and matched_rels:
            actual_rel = sorted(matched_rels)[0]
            fixed = re.sub(
                rf'collect\s*\(\s*(DISTINCT\s+)?{re.escape(undef_var)}\s*\)',
                rf'collect(\1{actual_rel})',
                fixed,
                flags=re.IGNORECASE
            )
        else:
            # Remove undefined collect
            fixed = re.sub(
                rf'\+\s*collect\s*\(\s*(?:DISTINCT\s+)?{re.escape(undef_var)}\s*\)',
                '', fixed, flags=re.IGNORECASE
            )
            fixed = re.sub(
                rf'collect\s*\(\s*(?:DISTINCT\s+)?{re.escape(undef_var)}\s*\)\s*\+',
                '', fixed, flags=re.IGNORECASE
            )
            fixed = re.sub(
                rf',\s*collect\s*\(\s*(?:DISTINCT\s+)?{re.escape(undef_var)}\s*\)\s+AS\s+edges',
                ', [] AS edges', fixed, flags=re.IGNORECASE
            )
    
    return fixed


def _fix_undefined_vars_multi_match(cypher: str) -> str:
    """
    Fix undefined variables for multi-MATCH queries.
    
    Strategy: Parse into MATCH-WITH segments, fix each segment's WITH
    using the relationship variables from that segment's MATCH.
    """
    # Split query into segments at MATCH boundaries
    # Pattern: MATCH ... (up to next MATCH or RETURN)
    segments = re.split(r'(?=\bMATCH\b)', cypher, flags=re.IGNORECASE)
    segments = [s for s in segments if s.strip()]
    
    if len(segments) <= 1:
        return _fix_undefined_vars_simple(cypher)
    
    fixed_segments = []
    
    for segment in segments:
        # Find relationship variables in this segment's MATCH clause
        rel_vars_in_segment = re.findall(r'\[(\w+):\w+\]', segment)
        
        if not rel_vars_in_segment:
            fixed_segments.append(segment)
            continue
        
        # The relationship variable for this segment
        segment_rel = rel_vars_in_segment[0]  # Usually just one per MATCH
        
        # Find undefined 'r' or similar in this segment's collect() calls
        # Pattern: collect(DISTINCT r) where r is not defined in this segment
        def fix_collect_in_segment(match):
            full_match = match.group(0)
            distinct_part = match.group(1) or ''
            var_name = match.group(2)
            
            # Check if this variable is defined in the segment
            # Only match relationship vars [var:TYPE] and node vars (var:Label)
            # NOT collect(var) which would incorrectly mark var as defined
            all_vars_in_segment = set(re.findall(r'\[(\w+):\w+\]', segment))  # [r1:TYPE]
            all_vars_in_segment.update(re.findall(r'\((\w+):\w+', segment))   # (g:gene)
            
            if var_name not in all_vars_in_segment:
                # It's undefined - check if it looks like a relationship var
                if var_name.lower() in ['r', 'rel', 'edge', 'e'] or re.match(r'^r\d*$', var_name, re.IGNORECASE):
                    return f'collect({distinct_part}{segment_rel})'
            
            return full_match
        
        # Fix collect() calls in the WITH clause of this segment
        fixed_segment = re.sub(
            r'collect\s*\(\s*(DISTINCT\s+)?(\w+)\s*\)',
            fix_collect_in_segment,
            segment,
            flags=re.IGNORECASE
        )
        
        fixed_segments.append(fixed_segment)
    
    return ''.join(fixed_segments)


def fix_distinct_in_collect(cypher: str) -> str:
    """
    Add DISTINCT to collect() calls that are missing it.
    
    Fixes: collect(var) → collect(DISTINCT var)
    """
    # Pattern: collect( followed by word (not DISTINCT) followed by )
    fixed = re.sub(
        r'collect\s*\(\s*(?!DISTINCT)(\w+)\s*\)',
        r'collect(DISTINCT \1)',
        cypher,
        flags=re.IGNORECASE
    )
    return fixed


def fix_return_format(cypher: str) -> str:
    """
    Fix RETURN statement to return only nodes and edges.
    
    Fixes: RETURN nodes, edges, extra → RETURN nodes, edges;
    Preserves any existing LIMIT clause.
    """
    # Check if we need to fix
    if not re.search(r'\bRETURN\b', cypher, re.IGNORECASE):
        return cypher
    
    # Already correct format? (with optional LIMIT)
    if re.search(r'RETURN\s+nodes\s*,\s*edges\s*(?:\s+LIMIT\s+\d+)?\s*;?\s*$', cypher, re.IGNORECASE):
        return cypher
    
    # Extract any existing LIMIT before replacing
    limit_match = re.search(r'\bLIMIT\s+(\d+)', cypher, re.IGNORECASE)
    limit_clause = f'\nLIMIT {limit_match.group(1)}' if limit_match else ''
    
    # Fix: replace RETURN statement with correct format, preserving LIMIT
    fixed = re.sub(
        r'\bRETURN\s+.*$',
        f'RETURN nodes, edges{limit_clause};',
        cypher,
        flags=re.IGNORECASE | re.MULTILINE
    )
    
    return fixed


def fix_multi_match_collections(cypher: str) -> str:
    """
    Fix multi-MATCH queries to properly collect all nodes and edges.
    
    This function tracks variable SCOPE properly:
    - After `WITH var1, var2`, only var1 and var2 are in scope
    - Variables from previous MATCHes are LOST unless carried forward
    
    Strategy:
    1. Parse query linearly, tracking scope at each point
    2. Fix INTERMEDIATE WITH clauses to carry forward ALL vars from their preceding MATCH
    3. Track what's actually in scope at the final WITH
    4. Only collect variables that are in scope at the end
    
    Example:
        MATCH (g:gene)-[r:TYPE1]->(d:disease)
        WITH g                                    ← Bug! r and d lost
        MATCH (g)-[r2:TYPE2]->(go:gene_ontology)
        WITH collect(g) AS nodes, collect(r) AS edges  ← r out of scope!
        RETURN nodes, edges;
        
    Becomes:
        MATCH (g:gene)-[r:TYPE1]->(d:disease)
        WITH g, r, d                              ← All vars carried forward
        MATCH (g)-[r2:TYPE2]->(go:gene_ontology)
        WITH collect(DISTINCT d)+collect(DISTINCT g)+collect(DISTINCT go) AS nodes, 
             collect(DISTINCT r)+collect(DISTINCT r2) AS edges
        RETURN nodes, edges;
    """
    if not is_multi_match_query(cypher):
        return cypher
    
    # Helper to extract node vars from a MATCH clause
    def extract_nodes_from_match(text):
        return set(re.findall(r'\((\w+)(?::\w+)?[^)]*\)', text))
    
    # Helper to extract relationship vars from a MATCH clause
    def extract_rels_from_match(text):
        return set(re.findall(r'\[(\w+):\w+[^\]]*\]', text))
    
    # Split query into lines and process
    lines = cypher.strip().split('\n')
    
    # Track scope as we process the query
    current_scope_nodes = set()
    current_scope_rels = set()
    all_node_vars = set()
    all_rel_vars = set()
    
    fixed_lines = []
    i = 0
    
    while i < len(lines):
        line = lines[i].strip()
        
        # Check if this line has a MATCH
        if re.match(r'\bMATCH\b', line, re.IGNORECASE):
            # Extract variables from this MATCH
            new_nodes = extract_nodes_from_match(line)
            new_rels = extract_rels_from_match(line)
            
            # Add to scope and track all vars
            current_scope_nodes.update(new_nodes)
            current_scope_rels.update(new_rels)
            all_node_vars.update(new_nodes)
            all_rel_vars.update(new_rels)
            
            fixed_lines.append(lines[i])
            i += 1
            continue
        
        # Check if this line has WHERE (may have constraints, keep as-is)
        if re.match(r'\bWHERE\b', line, re.IGNORECASE):
            fixed_lines.append(lines[i])
            i += 1
            continue
        
        # Check if this is a WITH line
        if re.match(r'\bWITH\b', line, re.IGNORECASE):
            # Check if there's more after this (another MATCH or RETURN)
            remaining_lines = '\n'.join(lines[i+1:]) if i+1 < len(lines) else ''
            has_more_match = bool(re.search(r'\bMATCH\b', remaining_lines, re.IGNORECASE))
            has_return = bool(re.search(r'\bRETURN\b', remaining_lines, re.IGNORECASE))
            
            with_content = re.sub(r'^\s*WITH\s+', '', line, flags=re.IGNORECASE)
            has_collect = 'collect' in with_content.lower()
            
            if has_more_match and not has_collect:
                # This is an INTERMEDIATE WITH that just passes vars
                # Expand it to include ALL vars currently in scope
                vars_to_carry = sorted(current_scope_nodes | current_scope_rels)
                vars_to_carry = [v for v in vars_to_carry if v not in ('nodes', 'edges')]
                
                if vars_to_carry:
                    fixed_lines.append(f"WITH {', '.join(vars_to_carry)}")
                else:
                    fixed_lines.append(lines[i])
                i += 1
                continue
            
            elif has_more_match and has_collect:
                # This is an INTERMEDIATE WITH with collect() - keep as-is but reset scope
                # After this, only the aliases are in scope
                aliases = re.findall(r'\bAS\s+(\w+)', with_content, re.IGNORECASE)
                # These aliases become the new "variables" in scope (they're collections, not nodes)
                # Clear the node/rel scope - next MATCH will add new vars
                current_scope_nodes.clear()
                current_scope_rels.clear()
                # Track intermediate collection aliases for use in final WITH
                for alias in aliases:
                    if 'node' in alias.lower() or alias.lower().startswith('n'):
                        current_scope_nodes.add(alias)
                    elif 'edge' in alias.lower() or alias.lower().startswith('e') or 'rel' in alias.lower():
                        current_scope_rels.add(alias)
                
                fixed_lines.append(lines[i])
                i += 1
                continue
            
            elif not has_more_match:
                # This is the FINAL WITH clause (or only WITH before RETURN)
                # Build proper collection from in-scope variables
                # Separate intermediate aliases (already collections) from regular vars
                in_scope_nodes = current_scope_nodes - {'nodes', 'edges'}
                in_scope_rels = current_scope_rels - {'nodes', 'edges'}
                
                node_parts = []
                edge_parts = []
                
                for v in sorted(in_scope_nodes):
                    # Check if this is an intermediate alias (already a collection)
                    if re.match(r'^nodes\d*$', v, re.IGNORECASE) or v.lower().startswith('n') and v[1:].isdigit():
                        node_parts.append(v)  # Already a collection, use directly
                    else:
                        node_parts.append(f"collect(DISTINCT {v})")  # Wrap in collect
                
                for v in sorted(in_scope_rels):
                    # Check if this is an intermediate alias (already a collection)
                    if re.match(r'^edges\d*$', v, re.IGNORECASE) or v.lower().startswith('e') and v[1:].isdigit():
                        edge_parts.append(v)  # Already a collection, use directly
                    else:
                        edge_parts.append(f"collect(DISTINCT {v})")  # Wrap in collect
                
                nodes_expr = "+".join(node_parts) if node_parts else "[]"
                edges_expr = "+".join(edge_parts) if edge_parts else "[]"
                
                fixed_lines.append(f"WITH {nodes_expr} AS nodes, {edges_expr} AS edges")
                i += 1
                continue
        
        # Check if this is RETURN
        if re.match(r'\bRETURN\b', line, re.IGNORECASE):
            fixed_lines.append("RETURN nodes, edges;")
            i += 1
            continue
        
        # Default: keep line as-is
        fixed_lines.append(lines[i])
        i += 1
    
    return '\n'.join(fixed_lines)


def fix_missing_collections(cypher: str) -> str:
    """
    Add missing nodes and relationships to collections.
    
    Ensures all MATCH variables are collected in WITH clause.
    """
    # Get matched vs collected variables
    matched_nodes, matched_rels = extract_matched_variables(cypher)
    collected_nodes, collected_edges = extract_collected_variables(cypher)
    
    missing_nodes = matched_nodes - collected_nodes
    missing_rels = matched_rels - collected_edges
    
    if not missing_nodes and not missing_rels:
        return cypher
    
    fixed = cypher
    
    # Add missing nodes to collection
    if missing_nodes:
        # Find the nodes expression in WITH clause
        with_match = re.search(r'(WITH\s+)(.*?)(\s+AS\s+nodes)', fixed, re.IGNORECASE)
        if with_match:
            prefix = with_match.group(1)
            current_expr = with_match.group(2)
            suffix = with_match.group(3)
            
            for var in missing_nodes:
                if not re.search(rf'\bcollect\s*\([^)]*\b{var}\b[^)]*\)', current_expr, re.IGNORECASE):
                    current_expr += f"+collect(DISTINCT {var})"
            
            fixed = fixed[:with_match.start()] + prefix + current_expr + suffix + fixed[with_match.end():]
    
    # Add missing relationships to collection
    if missing_rels:
        # Find the edges expression in WITH clause
        edges_match = re.search(r'(,\s*)(.*?)(\s+AS\s+edges)', fixed, re.IGNORECASE)
        if edges_match:
            prefix = edges_match.group(1)
            current_expr = edges_match.group(2)
            suffix = edges_match.group(3)
            
            for var in missing_rels:
                if var not in current_expr:
                    if current_expr.strip() == '[]':
                        current_expr = f"collect(DISTINCT {var})"
                    else:
                        current_expr += f"+collect(DISTINCT {var})"
            
            fixed = fixed[:edges_match.start()] + prefix + current_expr + suffix + fixed[edges_match.end():]
    
    return fixed


def fix_extra_collections(cypher: str) -> str:
    """
    Merge extra collections into nodes/edges.
    
    Fixes: WITH collect(g) AS nodes, collect(r) AS edges, collect(d) AS diseases
    →      WITH collect(g)+collect(d) AS nodes, collect(r) AS edges
    """
    # Find WITH clause
    with_match = re.search(r'WITH\s+(.*?)\s+RETURN', cypher, re.IGNORECASE | re.DOTALL)
    if not with_match:
        return cypher
    
    with_content = with_match.group(1)
    
    # Count AS clauses
    as_count = len(re.findall(r'\bAS\s+\w+', with_content, re.IGNORECASE))
    if as_count <= 2:
        return cypher  # Already correct
    
    # Extract all collections
    collections = re.findall(r'(collect\([^)]+\))\s+AS\s+(\w+)', with_content, re.IGNORECASE)
    
    node_collections = []
    edge_collections = []
    
    for collect_expr, alias in collections:
        alias_lower = alias.lower()
        if alias_lower in ['edges', 'relationships', 'rels']:
            edge_collections.append(collect_expr)
        elif alias_lower != 'nodes':  # Other aliases are treated as nodes
            node_collections.append(collect_expr)
        else:
            node_collections.insert(0, collect_expr)  # Keep existing nodes first
    
    if len(node_collections) > 1 or edge_collections:
        merged_nodes = '+'.join(node_collections) if node_collections else 'collect(DISTINCT null)'
        merged_edges = '+'.join(edge_collections) if edge_collections else '[]'
        
        new_with = f"WITH {merged_nodes} AS nodes, {merged_edges} AS edges"
        fixed = re.sub(r'WITH\s+.*?(?=\s+RETURN)', new_with, cypher, flags=re.IGNORECASE | re.DOTALL)
        return fixed
    
    return cypher


def fix_disease_naming(cypher: str) -> str:
    """
    Fix disease name references to use 'type 1 diabetes'.
    
    Fixes: T1D, Type 1 Diabetes, etc. → type 1 diabetes
    """
    # Common incorrect patterns and their fixes
    fixes = [
        # T1D → type 1 diabetes
        (r"['\"]\s*T1D\s*['\"]", "'type 1 diabetes'"),
        # Type 1 Diabetes (capitalized) → type 1 diabetes  
        (r"['\"]Type\s+1\s+Diabetes['\"]", "'type 1 diabetes'"),
        # type 1 diabetic → type 1 diabetes
        (r"['\"]\s*type\s+1\s+diabetic\s*['\"]", "'type 1 diabetes'"),
        # diabetes type 1 → type 1 diabetes
        (r"['\"]\s*diabetes\s+type\s+1\s*['\"]", "'type 1 diabetes'"),
        # T1DM → type 1 diabetes
        (r"['\"]\s*T1DM\s*['\"]", "'type 1 diabetes'"),
    ]
    
    fixed = cypher
    for pattern, replacement in fixes:
        fixed = re.sub(pattern, replacement, fixed, flags=re.IGNORECASE)
    
    return fixed


# Generic words dropped before comparing cell-type token sets. Kept minimal so
# state/subtype qualifiers ("active", "quiescent", "MUC5B") stay significant.
_CELL_TYPE_STOPWORDS = frozenset({"cell", "cells", "pancreatic"})


def _cell_type_token_set(value: str) -> frozenset:
    """Tokenize a cell-type label into a comparison set: lowercase, split on any
    non-alphanumeric (so ``+`` separates ``MUC5B+Ductal`` → muc5b, ductal), drop
    generic stopwords."""
    parts = re.split(r'[^a-z0-9]+', (value or "").lower())
    return frozenset(p for p in parts if p and p not in _CELL_TYPE_STOPWORDS)


def _build_cell_type_token_set_map(valid_cell_types) -> dict:
    """Map token-set → canonical name, keeping only sets owned by exactly one name
    (so an ambiguous abbreviation is left untouched rather than mis-resolved)."""
    owners: dict = {}
    for name in valid_cell_types:
        ts = _cell_type_token_set(name)
        if ts:
            owners.setdefault(ts, []).append(name)
    return {ts: names[0] for ts, names in owners.items() if len(names) == 1}


def fix_cell_type_references(cypher: str) -> str:
    """
    Fix anatomical_structure (formerly cell_type) name references.

    Handles:
    1. Old label: cell_type → anatomical_structure
    2. Case corrections for known anatomical structure names
    3. Plurals: "Ductal Cells" → "Ductal Cell" etc.
    """
    # Fix old label references
    cypher = re.sub(r'\bcell_type\b', 'anatomical_structure', cypher)

    try:
        from .schema_loader import get_valid_property_values  # package-relative (runtime)
    except ImportError:
        from schema_loader import get_valid_property_values  # flat import (tests / direct script)

    # Get valid names from schema
    valid_values = get_valid_property_values()
    if not valid_values or 'node_properties' not in valid_values:
        return cypher

    # Try anatomical_structure first, fall back to cell_type for backward compat
    name_info = valid_values.get('node_properties', {}).get('anatomical_structure', {}).get('name', {})
    if not name_info:
        name_info = valid_values.get('node_properties', {}).get('cell_type', {}).get('name', {})
    valid_cell_types = set(name_info.get('values', name_info.get('examples', [])))
    
    if not valid_cell_types:
        return cypher
    
    # Build comprehensive mappings
    # 1. Plural → Singular mapping
    plural_to_singular = {}
    for ct in valid_cell_types:
        plural = ct + "s"  # "Beta Cell" → "Beta Cells"
        plural_to_singular[plural.lower()] = ct

    # 2. Case-insensitive mapping
    case_map = {ct.lower(): ct for ct in valid_cell_types}

    # 3. Short-to-canonical mapping for ADA-schema UBERON/CL long names
    # (e.g. "Beta Cell" → "type B pancreatic cell (beta cell)")
    short_to_canonical = name_info.get('short_to_canonical', {}) or {}
    short_map = {short.lower(): canon for short, canon in short_to_canonical.items()}

    # 4. Combined mapping (handles plural, case, and short→canonical)
    all_fixes = {}
    all_fixes.update(case_map)
    all_fixes.update(plural_to_singular)
    all_fixes.update(short_map)  # short→canonical takes precedence

    # 5. Token-set mapping — resolves abbreviated/reordered cell-type labels the
    #    model emits from display strings (e.g. "ductal" or "MUC5B+Ductal") to the
    #    canonical schema name ("ductal cell" / "pancreatic ductal cell MUC5B+").
    #    Tokens split on non-alphanumerics (so "+" separates), with generic words
    #    stripped; a label maps only when EXACTLY ONE schema name shares its token
    #    set (ambiguity guard). This is what makes id-bearing follow-ups resolve:
    #    the guessed name no longer has to match the stored string verbatim.
    token_set_map = _build_cell_type_token_set_map(valid_cell_types)

    def _resolve(value: str):
        """Return the canonical name for *value*, or None if it can't be resolved."""
        vl = value.lower()
        if vl in all_fixes:
            return all_fixes[vl]
        ts = _cell_type_token_set(value)
        return token_set_map.get(ts)

    def fix_cell_type_value_in_braces(match):
        """Fix a cell type value in {name: "value"} pattern."""
        quote = match.group(1)
        fixed_name = _resolve(match.group(2))
        if fixed_name:
            return f'{{name: {quote}{fixed_name}{quote}}}'
        return match.group(0)

    def fix_cell_type_value_in_assignment(match):
        """Fix a cell type value in .name = "value" pattern."""
        quote = match.group(1)
        fixed_name = _resolve(match.group(2))
        if fixed_name:
            return f'.name = {quote}{fixed_name}{quote}'
        return match.group(0)
    
    # Pattern 1: Match {name: 'value'} or {name: "value"} format
    fixed = re.sub(
        r'\{name:\s*([\'"])([^\'\"]+)\1\}',
        fix_cell_type_value_in_braces,
        cypher,
        flags=re.IGNORECASE
    )
    
    # Pattern 2: Match .name = 'value' or .name = "value" format  
    fixed = re.sub(
        r'\.name\s*=\s*([\'"])([^\'\"]+)\1',
        fix_cell_type_value_in_assignment,
        fixed,
        flags=re.IGNORECASE
    )
    
    return fixed


def fix_property_names(cypher: str) -> str:
    """
    Fix common property name typos based on schema.
    
    Common fixes:
    - upOrDownRegulation → UpOrDownRegulation
    - log2foldchange → Log2FoldChange
    """
    # Get valid properties
    node_props = get_valid_node_properties()
    edge_props = get_valid_edge_properties()
    
    # Build case-insensitive property mapping
    all_props = set()
    for props in node_props.values():
        all_props.update(props)
    for props in edge_props.values():
        all_props.update(props)
    
    prop_map = {p.lower(): p for p in all_props}
    
    def fix_property_access(match):
        """Fix a property access to use correct case."""
        var = match.group(1)
        prop = match.group(2)
        prop_lower = prop.lower()
        
        if prop_lower in prop_map and prop != prop_map[prop_lower]:
            return f"{var}.{prop_map[prop_lower]}"
        return match.group(0)
    
    # Pattern: var.property
    fixed = re.sub(r'(\w+)\.(\w+)', fix_property_access, cypher)
    
    return fixed


def _match_enum_value(literal: str, valid_values: list) -> Optional[str]:
    """Return the canonical enum value that matches `literal`, or None.

    Matching rules, in priority order:
      1. Exact match (case-sensitive).
      2. Case-insensitive exact match.
      3. Substring match — literal is a substring of exactly one valid value
         (case-insensitive). Resolves "beta cell" → "type B pancreatic cell
         (beta cell)" without ambiguity.
      4. Otherwise None — do not guess.
    """
    if not isinstance(literal, str) or not valid_values:
        return None
    # 1. exact
    if literal in valid_values:
        return literal
    # 2. case-insensitive exact
    low = literal.lower()
    for v in valid_values:
        if isinstance(v, str) and v.lower() == low:
            return v
    # 3. unique substring match
    contains = [v for v in valid_values if isinstance(v, str) and low in v.lower()]
    if len(contains) == 1:
        return contains[0]
    # Reverse: literal contains a unique valid value (handles "T1D" → if
    # one of the valid values is "T1D Stage 3", we want the unique match).
    contained = [v for v in valid_values if isinstance(v, str) and v.lower() in low]
    if len(contained) == 1:
        return contained[0]
    return None


# Patterns for property=literal expressions inside WHERE/SET/property-match.
# We capture:  <var>.<prop>  <op>  "<literal>"
#         or   <var>.<prop>  <op>  '<literal>'   (single quotes get fixed earlier)
# Operators handled: =, CONTAINS, STARTS WITH, ENDS WITH
_PROP_LITERAL_RE = re.compile(
    r"(?P<var>\w+)\.(?P<prop>\w+)\s*"
    r"(?P<op>=|CONTAINS|STARTS\s+WITH|ENDS\s+WITH)\s*"
    r"(?P<quote>[\"'])(?P<value>[^\"']*?)(?P=quote)",
    re.IGNORECASE,
)


def _build_var_label_map(cypher: str) -> Dict[str, str]:
    """Scan MATCH patterns for `(var:Label)` and `[var:RelType]` and return
    a `{var: simple_label}` map (using the last `;`-token for compound names)."""
    out: Dict[str, str] = {}
    # node vars
    for m in re.finditer(r"\(\s*(\w+)\s*:\s*([A-Za-z_; ]+?)\s*(?:\{|\)|$)", cypher):
        var = m.group(1)
        label = m.group(2).strip().split(";")[-1].strip().rstrip(")")
        if var and label and var not in out:
            out[var] = label
    # rel vars
    for m in re.finditer(r"\[\s*(\w+)\s*:\s*`?([A-Za-z_;0-9 ]+?)`?\s*(?:\{|\])", cypher):
        var = m.group(1)
        rel = m.group(2).strip().split(";")[-1].strip()
        if var and rel and var not in out:
            out[var] = rel
    return out


def fix_enum_value_coercion(
    cypher: str, schema_slice: Optional[Dict] = None
) -> Tuple[str, List[str]]:
    """Coerce string literals in `<var>.<prop> = "value"` style expressions to
    their canonical enum values from the given schema slice.

    `schema_slice` is the output of `schema_loader.get_property_enum_map`:
        {
          "node_properties": {<simple_label>: {<prop>: [valid, ...]}, ...},
          "edge_properties": {<simple_rel>:   {<prop>: [valid, ...]}, ...},
        }

    Strategy: build a `{var: simple_label}` map from MATCH patterns, then for
    each `var.prop OP "literal"`, look up the enum list for the matching
    (label, prop). If the literal is not already a valid enum value but
    matches one canonically (case-insensitive or unique substring), replace
    it. Otherwise leave it alone.

    Returns `(fixed_cypher, list_of_per-fix_messages)`. When schema_slice is
    None or empty, this is a no-op.
    """
    if not cypher or not schema_slice:
        return cypher, []
    if not (schema_slice.get("node_properties") or schema_slice.get("edge_properties")):
        return cypher, []

    var_label = _build_var_label_map(cypher)
    node_props = schema_slice.get("node_properties", {}) or {}
    edge_props = schema_slice.get("edge_properties", {}) or {}

    fixes: List[str] = []

    def replace(match: re.Match) -> str:
        var = match.group("var")
        prop = match.group("prop")
        op = match.group("op")
        quote = match.group("quote")
        value = match.group("value")

        label = var_label.get(var)
        if not label:
            return match.group(0)

        # Look up enum list — try node first, then edge
        enum_values = node_props.get(label, {}).get(prop) or edge_props.get(label, {}).get(prop)
        if not enum_values:
            return match.group(0)

        # If the value is already exact, nothing to do
        if value in enum_values:
            return match.group(0)

        canonical = _match_enum_value(value, enum_values)
        if canonical is None or canonical == value:
            return match.group(0)

        fixes.append(
            f"Coerced {var}.{prop} {op.upper()} {quote}{value}{quote} -> {quote}{canonical}{quote}"
        )
        return f"{var}.{prop} {op} {quote}{canonical}{quote}"

    fixed = _PROP_LITERAL_RE.sub(replace, cypher)
    return fixed, fixes


# Donor properties that LOOK numeric but are actually stored as strings.
# Each entry maps to the wrapper needed to coerce the property to a number:
#  - "int_years": value is "<N> years" — extract first token, toInteger
#  - "float":     value is a numeric string like "21.3" — toFloat
_DONOR_STRING_NUMERICS: dict[str, str] = {
    "age": "int_years",
    "diabetes_duration": "int_years",
    "bmi": "float",
    "hba1c_percentage": "float",
    "c_peptide_ng_ml": "float",
}

# var.prop  <op>  <numeric literal>   (e.g. `d.age < 40`)
_NUMERIC_COMPARE_RE = re.compile(
    r"(?P<var>\w+)\.(?P<prop>\w+)\s*(?P<op>(?:<=|>=|<>|!=|<|>|=))\s*(?P<num>-?\d+(?:\.\d+)?)\b"
)


def fix_donor_string_numeric_compares(cypher: str) -> Tuple[str, List[str]]:
    """Rewrite raw `<var>.age < 40` style comparisons on donor numeric-string
    properties to use the proper conversion wrapper.

    `age` is stored in Neo4j as e.g. `"18 years"`, so `d.age < 40` silently
    returns 0 rows (string-vs-number comparison). This pass rewrites those
    patterns to `toInteger(split(d.age, ' ')[0]) < 40` (or `toFloat(d.bmi)`
    for plain-numeric strings).

    Returns ``(fixed_cypher, list_of_per-fix_messages)``. No-op when no
    donor numeric-string compares are found.
    """
    if not cypher:
        return cypher, []
    # Quick exit if none of the target properties appear in the cypher.
    if not any(re.search(rf"\b{p}\b", cypher) for p in _DONOR_STRING_NUMERICS):
        return cypher, []

    fixes: List[str] = []

    def replace(m: re.Match) -> str:
        prop = m.group("prop")
        kind = _DONOR_STRING_NUMERICS.get(prop)
        if not kind:
            return m.group(0)
        var = m.group("var")
        op = m.group("op")
        num = m.group("num")
        # Don't double-wrap if already inside a toInteger/toFloat call (look
        # back a few chars in the surrounding cypher — handled by re.sub
        # context, but defensively skip if the preceding chars are "toInt("
        # or "toFloat(").
        start = m.start()
        preceding = cypher[max(0, start - 18):start]
        if "toInteger(" in preceding or "toFloat(" in preceding:
            return m.group(0)
        if kind == "int_years":
            wrapped = f"toInteger(split({var}.{prop}, ' ')[0])"
        else:
            wrapped = f"toFloat({var}.{prop})"
        fixes.append(
            f"Coerced {var}.{prop} {op} {num} -> {wrapped} {op} {num} "
            f"({prop} is stored as a String)"
        )
        return f"{wrapped} {op} {num}"

    fixed = _NUMERIC_COMPARE_RE.sub(replace, cypher)
    return fixed, fixes


# Properties whose values are numeric-looking strings — comparison with an
# unquoted int returns 0 rows. Quote the literal instead. (Range comparisons
# like < or > on these are ambiguous: chr can be '1'..'22','X','Y','MT'; we
# only fix equality.)
_STRING_QUOTED_NUMERIC_PROPS: set[str] = {
    "chr",          # gene, OCR_peak, variants (values: '1'..'22','X','Y','MT')
    "strand",       # gene (values: '-1','1')
    "credibleset",  # part_of_QTL_signal (string like '1','2',...)
}

# var.prop = <integer>  (equality with unquoted int literal)
_INT_EQ_RE = re.compile(
    r"(?P<var>\w+)\.(?P<prop>\w+)\s*=\s*(?P<num>-?\d+)\b(?!\s*\.)"
)


def fix_string_typed_numeric_equality(cypher: str) -> Tuple[str, List[str]]:
    """Rewrite `<var>.<prop> = <int>` to `<var>.<prop> = '<int>'` when the
    property is one of the numeric-string-typed ones (chr / strand /
    credibleset etc.).

    Example: `WHERE g.chr = 1` → `WHERE g.chr = '1'`.

    Returns ``(fixed_cypher, list_of_per-fix_messages)``.
    """
    if not cypher:
        return cypher, []
    if not any(re.search(rf"\b{p}\b", cypher) for p in _STRING_QUOTED_NUMERIC_PROPS):
        return cypher, []
    fixes: List[str] = []

    def replace(m: re.Match) -> str:
        prop = m.group("prop")
        if prop not in _STRING_QUOTED_NUMERIC_PROPS:
            return m.group(0)
        var = m.group("var")
        num = m.group("num")
        fixes.append(
            f"Quoted {var}.{prop} = {num} -> {var}.{prop} = '{num}' "
            f"({prop} is stored as a String)"
        )
        return f'{var}.{prop} = "{num}"'

    fixed = _INT_EQ_RE.sub(replace, cypher)
    return fixed, fixes


# Boolean-flag properties stored as mixed-case strings ('TRUE','True','FALSE','False',...).
# Comparing with a Cypher boolean (`= true`) returns 0 rows; comparing with a
# specific case (`= 'TRUE'`) misses the rows with other casing. Canonical form
# is `toLower(<var>.<prop>) = 'true'` / `'false'`.
_STRING_BOOLEAN_PROPS: set[str] = {
    "in_3prime_UTR",
    "in_3prime_gene_region",
    "in_5prime_UTR",
    "in_5prime_gene_region",
    "in_intron",
    "in_acceptor_splice_site",
    "in_donor_splice_site",
    "non_synonymous_frameshift",
    "non_synonymous_missense",
    "non_synonymous_nonsense",
}

# Patterns:
#   var.prop = true   |  var.prop = false  (lowercase booleans)
_BARE_BOOL_RE = re.compile(
    r"(?P<var>\w+)\.(?P<prop>\w+)\s*=\s*(?P<val>true|false)\b",
    re.IGNORECASE,
)
# Patterns:
#   var.prop = "TRUE"  |  = 'False'  etc.  (quoted, any casing)
_QUOTED_BOOL_RE = re.compile(
    r"(?P<var>\w+)\.(?P<prop>\w+)\s*=\s*(?P<quote>[\"'])(?P<val>TRUE|True|true|FALSE|False|false)(?P=quote)"
)


def fix_string_typed_boolean_equality(cypher: str) -> Tuple[str, List[str]]:
    """Rewrite boolean-flag comparisons to a case-insensitive form so the
    auto-fix matches every casing (`'TRUE'`, `'True'`, `'true'`, …) of the
    underlying string-typed boolean variant flags.

    Both patterns canonicalise to: ``toLower(<var>.<prop>) = '<true|false>'``.
    Already-canonicalised expressions (`toLower(...)`) are left alone.
    """
    if not cypher:
        return cypher, []
    if not any(re.search(rf"\b{p}\b", cypher) for p in _STRING_BOOLEAN_PROPS):
        return cypher, []
    if "toLower(" in cypher:
        # Defensive — don't double-wrap if already canonicalised somewhere.
        # We still process unrelated occurrences via the regex's per-match check.
        pass

    fixes: List[str] = []

    def _replace_common(var: str, prop: str, raw_val: str, src_repr: str) -> str | None:
        if prop not in _STRING_BOOLEAN_PROPS:
            return None
        bool_word = "true" if raw_val.lower() == "true" else "false"
        fixes.append(
            f"Canonicalised {var}.{prop} = {src_repr} -> "
            f"toLower({var}.{prop}) = '{bool_word}' "
            f"({prop} is a mixed-case String boolean)"
        )
        return f"toLower({var}.{prop}) = '{bool_word}'"

    def replace_bare(m: re.Match) -> str:
        out = _replace_common(m.group("var"), m.group("prop"), m.group("val"), m.group("val"))
        return out if out is not None else m.group(0)

    def replace_quoted(m: re.Match) -> str:
        q = m.group("quote")
        v = m.group("val")
        out = _replace_common(m.group("var"), m.group("prop"), v, f"{q}{v}{q}")
        return out if out is not None else m.group(0)

    fixed = _BARE_BOOL_RE.sub(replace_bare, cypher)
    fixed = _QUOTED_BOOL_RE.sub(replace_quoted, fixed)
    return fixed, fixes


def fix_relationship_directions(cypher: str) -> str:
    """
    Fix relationship directions based on schema.
    
    If the direction is wrong, swap it to match schema.
    
    Note: This is a complex fix and may not always be correct.
    Currently just logs a warning for manual review.
    """
    # This is harder to auto-fix without potentially breaking queries
    # For now, we'll leave this as a validation-only check
    # The fix would require swapping node positions which could break WHERE clauses
    
    # Get expected directions
    directions = get_relationship_directions()
    
    # For now, just return the query unchanged
    # A future enhancement could attempt to fix this
    return cypher


_GENE_ACTIVITY_MATCH = re.compile(
    # (gene_var:gene)-[rel_var:gene_activity_score_in]->(cell_var:anatomical_structure...)
    r'\(\s*(?P<g>\w+)\s*:\s*gene\s*\)\s*-\s*\[\s*\w+\s*:\s*gene_activity_score_in\s*\]\s*->\s*\(\s*(?P<c>\w+)\s*:\s*anatomical_structure',
    re.IGNORECASE,
)


def fix_gene_activity_requires_detected(cypher: str) -> str:
    """Inject ``EXISTS { (g)-[:gene_detected_in]->(c) }`` whenever a query
    matches ``gene_activity_score_in`` but does not co-require a
    ``gene_detected_in`` edge between the same gene and cell type.

    Rationale: scATAC-derived gene activity scores carry many false positives
    (ambient signal, pseudobulk artifacts). We never want to report that a
    gene has chromatin activity in a cell type where it isn't actually
    detected/expressed. This is a noise filter — the rule is non-negotiable.

    The function is a best-effort textual transformation:
      - Skips queries that already reference ``gene_detected_in`` anywhere
        (assumed to be handled by the LLM).
      - Skips queries whose MATCH pattern doesn't fit the standard
        ``(g:gene)-[r:gene_activity_score_in]->(c:anatomical_structure)`` shape.
      - Appends to an existing ``WHERE`` clause if present (after the matching
        MATCH), otherwise inserts a new ``WHERE`` immediately after the MATCH.
    """
    if not cypher:
        return cypher
    if not re.search(r'\bgene_activity_score_in\b', cypher, re.IGNORECASE):
        return cypher
    # Already paired with gene_detected_in — trust the LLM
    if re.search(r'\bgene_detected_in\b', cypher, re.IGNORECASE):
        return cypher

    m = _GENE_ACTIVITY_MATCH.search(cypher)
    if not m:
        return cypher

    gene_var, cell_var = m.group('g'), m.group('c')
    exists_clause = f"EXISTS {{ ({gene_var})-[:gene_detected_in]->({cell_var}) }}"

    # m matches up through `(<cell_var>:anatomical_structure` — the opening
    # paren of the cell node is already consumed. We need the closing `)` of
    # that same node (which may have property block braces and even nested
    # parens inside function calls). Walk forward with depth=1, treating `(`
    # as depth-up and `)` as depth-down; the closing of the cell node is the
    # first `)` that returns depth to zero.
    paren_depth = 1
    end_idx = None
    for i in range(m.end(), len(cypher)):
        ch = cypher[i]
        if ch == '(':
            paren_depth += 1
        elif ch == ')':
            paren_depth -= 1
            if paren_depth == 0:
                end_idx = i + 1
                break
    if end_idx is None:
        return cypher

    # Is there a WHERE clause that belongs to this MATCH (i.e. before the next
    # MATCH / WITH / RETURN / OPTIONAL MATCH)?
    next_clause = re.search(
        r'\b(WHERE|WITH|RETURN|MATCH|OPTIONAL\s+MATCH|CALL)\b',
        cypher[end_idx:],
        re.IGNORECASE,
    )
    if next_clause and next_clause.group(1).upper() == 'WHERE':
        # Append to the existing WHERE clause's predicate list.
        where_pos = end_idx + next_clause.end()
        # Insert ` (<existing_predicates>) AND EXISTS { ... }` — to be safe,
        # wrap with parens around the new predicate too.
        return (
            cypher[:where_pos]
            + " "
            + exists_clause
            + " AND"
            + cypher[where_pos:]
        )
    else:
        # No WHERE — insert a fresh one right after the MATCH path.
        return (
            cypher[:end_idx]
            + f"\nWHERE {exists_clause}"
            + cypher[end_idx:]
        )


_HEAVY_RELATIONSHIP_TYPES = re.compile(
    r'\b(OCR_peak_in|gene_activity_score_in|gene_detected_in|physical_interaction|OCR_activity|OCR_locate_in)\b', re.IGNORECASE
)


def _needs_forced_limit(cypher: str) -> bool:
    """Return True if the query involves relationship types that produce
    massive result sets in Neo4j and must always be capped."""
    return bool(_HEAVY_RELATIONSHIP_TYPES.search(cypher))


def _inject_limit(cypher: str, default_limit: int = 50) -> str:
    """Low-level helper: inject a LIMIT clause into *cypher*.

    Strategy:
      - If there is a ``WITH collect(...)`` block, insert
        ``WITH <vars> LIMIT N`` right before it.
      - Otherwise append ``LIMIT N`` after the RETURN.
    """
    collect_match = re.search(r'\bWITH\s+collect\s*\(', cypher, re.IGNORECASE)
    if collect_match:
        prefix = cypher[:collect_match.start()]
        # Strip string literals so parens/brackets inside e.g. "beta cell"
        # don't get mistaken for Cypher node/rel variables.
        prefix_no_strings = re.sub(r'"[^"]*"', '""', prefix)
        prefix_no_strings = re.sub(r"'[^']*'", "''", prefix_no_strings)
        # Strip EXISTS { ... } sub-clauses — variables declared inside them
        # are NOT visible in the outer scope and must not appear in the
        # outer WITH list. Same for COUNT { ... } and OPTIONAL MATCH-scoped
        # subqueries.
        prefix_no_exists = re.sub(
            r'\bEXISTS\s*\{[^{}]*\}', '', prefix_no_strings, flags=re.IGNORECASE,
        )
        prefix_no_exists = re.sub(
            r'\bCOUNT\s*\{[^{}]*\}', '', prefix_no_exists, flags=re.IGNORECASE,
        )
        node_vars = set(re.findall(r'\((\w+)(?::\w+)?[^)]*\)', prefix_no_exists))
        rel_vars = set(re.findall(r'\[(\w+):\w+[^\]]*\]', prefix_no_exists))
        all_vars = node_vars | rel_vars
        if all_vars:
            vars_str = ', '.join(sorted(all_vars))
            insert_pos = collect_match.start()
            return (cypher[:insert_pos] +
                    f"WITH {vars_str} LIMIT {default_limit}\n" +
                    cypher[insert_pos:])

    if re.search(r'\bRETURN\b', cypher, re.IGNORECASE):
        cypher_clean = cypher.rstrip().rstrip(';')
        return f"{cypher_clean}\nLIMIT {default_limit};"

    return cypher


def add_result_limit(cypher: str, default_limit: int = 50) -> str:
    """Add LIMIT to queries that need it to prevent API payload overflow.

    Two cases get a LIMIT injected:
      1. **Heavy relationship queries** (OCR_peak_in, gene_activity_score_in,
         gene_detected_in, physical_interaction) — these produce massive result
         sets and ALWAYS need a cap, even when a WHERE clause is present.
      2. **Unconstrained queries** (no WHERE clause) — broad scans that
         could return everything.

    Queries that already contain a LIMIT are never modified.
    """
    if not cypher:
        return cypher

    if re.search(r'\bLIMIT\s+\d+', cypher, re.IGNORECASE):
        return cypher

    # OCR-related queries always get a LIMIT — the tables are huge
    if _needs_forced_limit(cypher):
        return _inject_limit(cypher, default_limit)

    # Non-OCR queries: only cap if there is no WHERE clause
    if re.search(r'\bWHERE\b', cypher, re.IGNORECASE):
        return cypher

    return _inject_limit(cypher, default_limit)


# ============================================================================
# Main Auto-Fix Function
# ============================================================================

def auto_fix_cypher(
    cypher: str,
    validation: Optional[Dict] = None,
    default_limit: int = 50,
    schema_slice: Optional[Dict] = None,
) -> Tuple[str, List[str]]:
    """
    Automatically fix common Cypher query errors.

    Handles two types of queries:
    - SIMPLE queries (single MATCH): All fixes applied
    - MULTI-MATCH queries: Only safe fixes applied (preserves query structure)

    Fixes for ALL queries:
    0. Convert single quotes to double quotes (CRITICAL - Neo4j API strips single quotes!)
    1. Add missing relationship variable names: -[:TYPE]- → -[r:TYPE]-
    1b. Fix undefined collected variables: collect(r) → collect(r1)
    2. Add DISTINCT to collect() calls
    6. Fix disease naming (T1D → type 1 diabetes)
    7. Fix cell type naming (beta cell → Beta Cell)
    7b. Coerce enum-valued literals using the entity-specific schema_slice
        (only fires when ``schema_slice`` is provided — see
        ``schema_loader.get_property_enum_map``)
    8. Fix property name case

    Additional fixes for SIMPLE queries:
    3. Merge extra collections into nodes/edges
    4. Add missing variables to collections
    5. Fix return format to RETURN nodes, edges;

    Additional fixes for MULTI-MATCH queries:
    3-5. Fix multi-match collections: Rebuilds final WITH to collect ALL nodes/edges
         from ALL MATCH clauses, outputs RETURN nodes, edges;

    Args:
        cypher: The Cypher query to fix
        validation: Optional validation result from validate_cypher()
        default_limit: LIMIT to add to prevent timeouts (default: 100)
        schema_slice: Optional enum map from
            ``schema_loader.get_property_enum_map`` covering only the
            entities the Cypher actually uses. When provided, enables
            ``fix_enum_value_coercion``. When None, that fix is a no-op
            (backwards compatible).

    Returns:
        (fixed_cypher, list_of_fixes_applied)
    """
    if not cypher or not isinstance(cypher, str):
        return cypher, []
    
    fixes_applied = []
    fixed = cypher
    
    # Detect query complexity - multi-MATCH queries need gentler handling
    is_multi = is_multi_match_query(cypher)
    is_simple = is_simple_query(cypher)
    
    if is_multi:
        logger.debug("Detected multi-MATCH query - applying safe fixes only (preserving structure)")
    
    try:
        # Fix 0: Convert single quotes to double quotes (CRITICAL - API strips single quotes!)
        # Must be applied FIRST before other fixes
        original = fixed
        fixed = fix_single_quotes_to_double(fixed)
        if fixed != original:
            fixes_applied.append("Converted single quotes to double quotes (API compatibility)")
        
        # Fix 1: Add missing relationship variable names (SAFE for all queries)
        original = fixed
        fixed = fix_relationship_variables(fixed)
        if fixed != original:
            fixes_applied.append("Added missing relationship variable names")
        
        # Fix 1b: Fix undefined variables in collect() 
        # Works for both simple and multi-MATCH queries (handles scope properly)
        original = fixed
        fixed = fix_undefined_collected_variables(fixed)
        if fixed != original:
            fixes_applied.append("Fixed undefined variables in collect() statements")
        
        # Fix 2: Add DISTINCT to collect() calls (SAFE for all queries)
        original = fixed
        fixed = fix_distinct_in_collect(fixed)
        if fixed != original:
            fixes_applied.append("Added DISTINCT to collect() calls")
        
        # Fix 3-5: Collection and return fixes
        if is_multi:
            # For multi-MATCH: Use special fix that properly collects all nodes/edges
            # while preserving the multi-hop structure
            original = fixed
            fixed = fix_multi_match_collections(fixed)
            if fixed != original:
                fixes_applied.append("Fixed multi-MATCH collections with proper scope tracking")
        else:
            # For simple queries: Use standard fixes
            # Fix 3: Merge extra collections
            original = fixed
            fixed = fix_extra_collections(fixed)
            if fixed != original:
                fixes_applied.append("Merged extra collections into nodes/edges")
            
            # Fix 4: Add missing variables to collections
            original = fixed
            fixed = fix_missing_collections(fixed)
            if fixed != original:
                fixes_applied.append("Added missing variables to collections")
            
            # Fix 5: Fix return format
            original = fixed
            fixed = fix_return_format(fixed)
            if fixed != original:
                fixes_applied.append("Fixed RETURN format")
        
        # Fix 6: Fix disease naming (SAFE for all queries)
        original = fixed
        fixed = fix_disease_naming(fixed)
        if fixed != original:
            fixes_applied.append("Fixed disease naming (T1D → type 1 diabetes)")
        
        # Fix 7: Fix anatomical_structure references (formerly cell_type) (SAFE for all queries)
        original = fixed
        fixed = fix_cell_type_references(fixed)
        if fixed != original:
            fixes_applied.append("Fixed anatomical_structure references (old labels, case)")

        # Fix 7b: Schema-slice-driven enum value coercion.
        # No-op when schema_slice is None (backwards compatible).
        if schema_slice:
            original = fixed
            fixed, enum_fix_msgs = fix_enum_value_coercion(fixed, schema_slice)
            if fixed != original and enum_fix_msgs:
                fixes_applied.extend(enum_fix_msgs)

        # Fix 7c: Coerce numeric comparisons on donor string-numeric properties
        # (donor.age < 40 -> toInteger(split(...)) < 40). The schema stores age
        # / bmi / hba1c_percentage / c_peptide_ng_ml as Strings; raw `<` against
        # an int returns zero rows. Schema-driven, always-on.
        original = fixed
        fixed, donor_fix_msgs = fix_donor_string_numeric_compares(fixed)
        if fixed != original and donor_fix_msgs:
            fixes_applied.extend(donor_fix_msgs)

        # Fix 7d: Quote integer literals when comparing string-typed
        # "numeric" properties (chr, strand, credibleset). They look numeric
        # but are stored as Strings; `g.chr = 1` returns 0 rows whereas
        # `g.chr = '1'` matches.
        original = fixed
        fixed, sq_fix_msgs = fix_string_typed_numeric_equality(fixed)
        if fixed != original and sq_fix_msgs:
            fixes_applied.extend(sq_fix_msgs)

        # Fix 7e: Canonicalise boolean-flag comparisons on variant nodes —
        # the underlying data has mixed casing ('TRUE'/'True'/'true' all
        # exist), so strict equality misses rows. Rewrite to
        # `toLower(<var>.<prop>) = 'true'|'false'`.
        original = fixed
        fixed, bool_fix_msgs = fix_string_typed_boolean_equality(fixed)
        if fixed != original and bool_fix_msgs:
            fixes_applied.extend(bool_fix_msgs)

        # Fix 8: Fix property name case (SAFE for all queries)
        original = fixed
        fixed = fix_property_names(fixed)
        if fixed != original:
            fixes_applied.append("Fixed property name capitalization")

        # Fix 8b: gene_activity_score_in must co-require gene_detected_in
        # (noise filter — no chromatin activity scores for genes not detected
        # in the same cell type).
        original = fixed
        fixed = fix_gene_activity_requires_detected(fixed)
        if fixed != original:
            fixes_applied.append("Injected EXISTS gene_detected_in filter for gene_activity_score_in")

        # Fix 9: Add LIMIT to UNCONSTRAINED queries (no WHERE clause) to prevent timeout
        original = fixed
        fixed = add_result_limit(fixed, default_limit=default_limit)
        if fixed != original:
            if _needs_forced_limit(original):
                fixes_applied.append(f"Added LIMIT {default_limit} to heavy OCR query")
            else:
                fixes_applied.append(f"Added LIMIT {default_limit} to unconstrained query (no WHERE clause)")
        
    except Exception as e:
        # If any fix fails, return the original query with error message
        logger.debug(f"Auto-fix failed: {e}")
        fixes_applied.append(f"ERROR: Auto-fix failed - {str(e)}")
        return cypher, fixes_applied
    
    return fixed, fixes_applied


# ============================================================================
# Validation Functions (original from PankBase)
# ============================================================================

def validate_cypher(cypher: str) -> Dict:
    """
    Validate a Cypher query and return a score with detailed feedback.
    
    Args:
        cypher: The Cypher query string to validate
        
    Returns:
        {
            'score': int (0-100),
            'errors': list[str],
            'warnings': list[str],
            'passed_checks': list[str]
        }
    """
    errors = []
    warnings = []
    passed_checks = []
    
    # Normalize whitespace for analysis
    cypher_normalized = ' '.join(cypher.split())
    
    # Check 1: WITH clause structure (CRITICAL - 35 points)
    # This catches: WITH DISTINCT sn, r AS nodes, edges (your example!)
    with_errors = check_with_clause_structure(cypher)
    if with_errors:
        errors.extend(with_errors)
    else:
        passed_checks.append("WITH clause properly structured with collect()")
    
    # Check 2: Relationship variables (CRITICAL - 30 points)
    rel_errors = check_relationship_variables(cypher)
    if rel_errors:
        errors.extend(rel_errors)
    else:
        passed_checks.append("All relationships have variable names")
    
    # Check 3: Return format (CRITICAL - 25 points)
    if not check_return_format(cypher_normalized):
        errors.append("Query must end with: WITH collect(DISTINCT ...) AS nodes, collect(DISTINCT ...) AS edges RETURN nodes, edges;")
    else:
        passed_checks.append("Correct return format with nodes and edges")
    
    # Check 3: DISTINCT in collect (IMPORTANT - 15 points)
    if not check_distinct_in_collect(cypher_normalized):
        errors.append("All collect() statements must use DISTINCT")
    else:
        passed_checks.append("All collect() use DISTINCT")
    
    # Check 4: Disease naming (IMPORTANT - 15 points)
    disease_errors = check_disease_naming(cypher)
    if disease_errors:
        errors.extend(disease_errors)
    else:
        if 'disease' in cypher.lower() or 't1d' in cypher.lower() or 'diabetes' in cypher.lower():
            passed_checks.append("Correct disease naming convention")
    
    # Check 5: Variable consistency (IMPORTANT - 10 points)
    var_errors = check_variable_consistency(cypher_normalized)
    if var_errors:
        warnings.extend(var_errors)
    else:
        passed_checks.append("All collected variables are defined in MATCH")
    
    # Check 5.5: Completeness - all matched variables are collected (CRITICAL - 20 points)
    completeness_errors = check_query_completeness(cypher)
    if completeness_errors:
        errors.extend(completeness_errors)
    else:
        passed_checks.append("All matched nodes and edges are collected and returned")
    
    # Check 5.6: WHERE constraints (IMPORTANT - warning only)
    where_warnings = check_where_constraints(cypher)
    if where_warnings:
        warnings.extend(where_warnings)
    else:
        passed_checks.append("Query has appropriate WHERE constraints")
    
    # Check 6: Property validity (IMPORTANT - 10 points)
    prop_errors = check_property_validity(cypher)
    if prop_errors:
        warnings.extend(prop_errors)
    else:
        if '.' in cypher:  # Only add if properties are used
            passed_checks.append("All properties appear valid")
    
    # Check 7: Property value validity (CRITICAL - 20 points)
    value_errors = check_property_value_validity(cypher)
    if value_errors:
        errors.extend(value_errors)
    else:
        if '=' in cypher and '.' in cypher:  # Only add if property comparisons are used
            passed_checks.append("All property values are valid")
    
    # Check 8: Relationship directions (CRITICAL - 25 points)
    direction_errors = check_relationship_directions(cypher)
    if direction_errors:
        errors.extend(direction_errors)
    else:
        if '-[' in cypher:  # Only add if relationships are used
            passed_checks.append("All relationships have correct source/target node types")
    
    # Calculate score
    score = 100
    
    # Deduct for errors
    # WITH clause structure errors (CRITICAL - 35 points)
    with_error_count = len([e for e in errors if 'with clause' in e.lower() or 'distinct must be inside' in e.lower() or 'must use collect' in e.lower()])
    if with_error_count > 0:
        score -= 35  # Critical error - malformed WITH clause
    
    relationship_error_count = len([e for e in errors if 'relationship' in e.lower() and 'variable name' in e.lower()])
    if relationship_error_count > 0:
        score -= 30  # Critical error - unnamed relationships
    
    if any(('return' in e.lower() or 'format' in e.lower()) and 'with clause' not in e.lower() for e in errors):
        score -= 25  # Critical error - wrong return format
    
    if any('distinct' in e.lower() for e in errors):
        score -= 15  # Important error - missing DISTINCT
    
    if any('disease' in e.lower() or 't1d' in e.lower() for e in errors):
        score -= 15  # Important error - wrong disease naming
    
    # Deduct for invalid property values (CRITICAL - 20 points)
    value_error_count = len([e for e in errors if 'Invalid value' in e])
    if value_error_count > 0:
        score -= 20  # Critical error - wrong values will cause query to return no results
    
    # Deduct for incorrect relationship directions (CRITICAL - 25 points)
    direction_error_count = len([e for e in errors if 'incorrect direction' in e.lower() or 'incorrect node types' in e.lower()])
    if direction_error_count > 0:
        score -= 25  # Critical error - wrong relationship direction will cause wrong results
    
    # Deduct for incomplete queries (CRITICAL - 20 points)
    completeness_error_count = len([e for e in errors if 'not collected' in e.lower() or 'missing from' in e.lower()])
    if completeness_error_count > 0:
        score -= 20  # Critical error - query won't return all matched data
    
    # Deduct for warnings (less severe)
    score -= len(warnings) * 5
    
    score = max(0, score)  # Don't go below 0
    
    return {
        'score': score,
        'errors': errors,
        'warnings': warnings,
        'passed_checks': passed_checks
    }


def check_relationship_variables(cypher: str) -> List[str]:
    """
    Check that all relationships have variable names.
    
    Returns list of error messages for unnamed relationships.
    """
    errors = []
    
    # Pattern to find relationships: -[...]-> or <-[...]-
    # We want to catch [:type] but not [var:type]
    
    # Find all relationship patterns
    rel_patterns = re.findall(r'-\[(.*?)\]-', cypher)
    
    for i, rel_content in enumerate(rel_patterns, 1):
        rel_content = rel_content.strip()
        
        # Empty relationship []
        if not rel_content:
            errors.append(f"Relationship #{i} is empty: -[]- (should have variable and type)")
            continue
        
        # Check if it starts with : (no variable name)
        if rel_content.startswith(':'):
            rel_type = rel_content[1:].split()[0].split('{')[0].strip()
            errors.append(f"Relationship ':{rel_type}' missing variable name (should be [var:{rel_type}])")
    
    return errors


def check_with_clause_structure(cypher: str) -> List[str]:
    """
    Check that WITH clause is properly structured with collect() functions.
    
    This catches errors like:
    - WITH DISTINCT sn, r AS nodes, edges (DISTINCT outside collect)
    - WITH sn AS nodes, r AS edges (missing collect)
    - WITH collect(sn) AS nodes, r AS edges (inconsistent - second missing collect)
    
    Returns list of errors.
    """
    errors = []
    
    # Find WITH clause (between WITH and RETURN)
    with_match = re.search(r'\bWITH\b(.*?)\bRETURN\b', cypher, re.IGNORECASE | re.DOTALL)
    if not with_match:
        return errors  # No WITH clause found
    
    with_clause = with_match.group(1).strip()
    
    # Check 1: DISTINCT should only appear inside collect()
    # Bad: WITH DISTINCT sn, r AS nodes, edges
    # Look for DISTINCT that's not followed by closing paren before next comma/AS
    if re.search(r'\bDISTINCT\b(?!\s+\w+\s*\))', with_clause, re.IGNORECASE):
        errors.append(
            "DISTINCT must be inside collect() functions. "
            "Use: WITH collect(DISTINCT var) AS nodes, ... "
            "Not: WITH DISTINCT var, ..."
        )
    
    # Check 2: Both nodes and edges must be assigned
    has_nodes = re.search(r'\bAS\s+nodes\b', with_clause, re.IGNORECASE)
    has_edges = re.search(r'\bAS\s+edges\b', with_clause, re.IGNORECASE)
    
    if not has_nodes:
        errors.append("WITH clause must define 'nodes' variable (... AS nodes)")
    if not has_edges:
        errors.append("WITH clause must define 'edges' variable (... AS edges)")
    
    # Check 3: Variables assigned to nodes/edges should use collect()
    # Look for pattern: <something> AS nodes where something is NOT collect(...)
    if has_nodes:
        # Extract what's being assigned to nodes
        # Match everything before AS nodes, handling expressions with +
        nodes_pattern = re.search(r'((?:collect\([^)]+\)|\w+)(?:\s*\+\s*(?:collect\([^)]+\)|\w+))*)\s+AS\s+nodes', with_clause, re.IGNORECASE)
        if nodes_pattern:
            assigned_to_nodes = nodes_pattern.group(1).strip()
            # Check if it contains collect (valid) or is just a variable (invalid)
            if 'collect' not in assigned_to_nodes.lower():
                errors.append(
                    f"Variables assigned to 'nodes' must use collect(). "
                    f"Found: {assigned_to_nodes} AS nodes. "
                    f"Should be: collect(DISTINCT ...) AS nodes"
                )
    
    if has_edges:
        # Extract what's being assigned to edges
        edges_pattern = re.search(r'((?:collect\([^)]+\)|\w+|\[\])(?:\s*\+\s*(?:collect\([^)]+\)|\w+))*)\s+AS\s+edges', with_clause, re.IGNORECASE)
        if edges_pattern:
            assigned_to_edges = edges_pattern.group(1).strip()
            # Check if it starts with collect or contains collect (or is empty array [])
            if 'collect' not in assigned_to_edges.lower() and assigned_to_edges != '[]':
                errors.append(
                    f"Variables assigned to 'edges' must use collect() or []. "
                    f"Found: {assigned_to_edges} AS edges. "
                    f"Should be: collect(DISTINCT ...) AS edges"
                )
    
    return errors


def check_query_completeness(cypher: str) -> List[str]:
    """
    Check that all nodes and relationships matched in the query are collected and returned.
    
    This ensures the query doesn't lose data by forgetting to collect some matched variables.
    
    For example, this is incomplete:
        MATCH (g:gene)-[r:genetic_interaction]->(g2:gene)-[r2:T1D_DEG_in]->(ct:anatomical_structure)
        WITH collect(DISTINCT g) AS nodes, collect(DISTINCT r) AS edges
        RETURN nodes, edges;
    
    Missing: g2, r2, ct are matched but not collected.
    
    EXCEPTION: Nodes used ONLY for filtering in WHERE clause don't need to be collected.
    For example, this is CORRECT:
        MATCH (g:gene)-[r:effector_gene_of]->(d:disease) WHERE d.name = 'type 1 diabetes'
        WITH collect(DISTINCT g) AS nodes, collect(DISTINCT r) AS edges
        RETURN nodes, edges;
    
    Here, 'd' is used only to filter - we want genes, not the disease node itself.
    
    Returns list of errors for missing variables.
    """
    errors = []
    
    # Extract all node and relationship variables from MATCH clauses
    matched_nodes = set()
    matched_relationships = set()
    
    # Find all MATCH clauses
    match_patterns = re.findall(r'\bMATCH\b(.*?)(?=\bWHERE\b|\bWITH\b|\bRETURN\b|\bMATCH\b|$)', 
                                cypher, re.IGNORECASE | re.DOTALL)
    
    for pattern in match_patterns:
        # Find node variables: (var:Label) or (var {props})
        # Match patterns like (g:gene), (g), (g {name: "x"})
        node_matches = re.findall(r'\((\w+)(?::\w+)?[^)]*\)', pattern)
        matched_nodes.update(node_matches)
        
        # Find relationship variables: [var:Type] or [var {props}]
        # Match patterns like [r:regulation], [r], [r {prop: "x"}]
        rel_matches = re.findall(r'\[(\w+)(?::\w+)?[^]]*\]', pattern)
        matched_relationships.update(rel_matches)
    
    # Extract nodes used in WHERE clause (these are filter nodes - don't require collection)
    filter_nodes = set()
    where_match = re.search(r'\bWHERE\b(.*?)(?=\bWITH\b|\bRETURN\b|$)', cypher, re.IGNORECASE | re.DOTALL)
    if where_match:
        where_clause = where_match.group(1)
        # Find node variables used in WHERE: d.name, g.id, etc.
        filter_vars = re.findall(r'\b(\w+)\.\w+\s*(?:=|<|>|<>|!=|CONTAINS|STARTS|ENDS|IN)', 
                                  where_clause, re.IGNORECASE)
        filter_nodes.update(filter_vars)
    
    # Extract collected variables from WITH clause
    collected_nodes = set()
    collected_relationships = set()
    
    # Find WITH clause (between WITH and RETURN)
    with_match = re.search(r'\bWITH\b(.*?)\bRETURN\b', cypher, re.IGNORECASE | re.DOTALL)
    if with_match:
        with_clause = with_match.group(1)
        
        # Find what's collected for nodes (... AS nodes)
        nodes_pattern = re.search(r'(.*?)\s+AS\s+nodes', with_clause, re.IGNORECASE | re.DOTALL)
        if nodes_pattern:
            nodes_expr = nodes_pattern.group(1)
            # Extract variables from collect(DISTINCT var) or collect(var)
            collected = re.findall(r'collect\s*\(\s*(?:DISTINCT\s+)?(\w+)\s*\)', nodes_expr, re.IGNORECASE)
            collected_nodes.update(collected)
        
        # Find what's collected for edges (... AS edges)
        edges_pattern = re.search(r'(.*?)\s+AS\s+edges', with_clause, re.IGNORECASE | re.DOTALL)
        if edges_pattern:
            edges_expr = edges_pattern.group(1)
            # Extract variables from collect(DISTINCT var) or collect(var)
            collected = re.findall(r'collect\s*\(\s*(?:DISTINCT\s+)?(\w+)\s*\)', edges_expr, re.IGNORECASE)
            collected_relationships.update(collected)
    
    # Check for missing nodes (exclude filter-only nodes)
    # A node is "filter-only" if it's in filter_nodes but NOT in collected_nodes
    # and NOT at the "source" of the relationship we're querying
    missing_nodes = matched_nodes - collected_nodes - filter_nodes
    if missing_nodes:
        errors.append(
            f"Node variable(s) matched but not collected: {', '.join(sorted(missing_nodes))}. "
            f"Add them to the nodes collection: collect(DISTINCT {' OR '.join(sorted(missing_nodes))})"
        )
    
    # Check for missing relationships
    missing_relationships = matched_relationships - collected_relationships
    if missing_relationships:
        errors.append(
            f"Relationship variable(s) matched but not collected: {', '.join(sorted(missing_relationships))}. "
            f"Add them to the edges collection: collect(DISTINCT {' OR '.join(sorted(missing_relationships))})"
        )
    
    return errors


def check_where_constraints(cypher: str) -> List[str]:
    """
    Check that queries have WHERE constraints to avoid returning ALL nodes.
    
    Warns if MATCH has no WHERE clause (likely returns too many results).
    """
    warnings = []
    
    # Check if there's a MATCH without a WHERE
    # Pattern: MATCH ... (no WHERE before WITH/RETURN)
    cypher_upper = cypher.upper()
    
    # Find MATCH clauses
    match_positions = [m.start() for m in re.finditer(r'\bMATCH\b', cypher_upper)]
    
    for match_pos in match_positions:
        # Find the next WITH, RETURN, or MATCH after this MATCH
        rest_of_query = cypher_upper[match_pos:]
        
        # Look for WHERE before the next clause
        next_clause_match = re.search(r'\b(WITH|RETURN|MATCH)\b', rest_of_query[6:])  # Skip the MATCH itself
        
        if next_clause_match:
            segment = rest_of_query[:next_clause_match.start() + 6]
        else:
            segment = rest_of_query
        
        # Check if this segment has WHERE
        if 'WHERE' not in segment:
            # Extract what's being matched (for better error message)
            match_content = segment[:100].strip()
            warnings.append(
                f"Query may return too many results: MATCH without WHERE constraint. "
                f"Consider adding WHERE clause to filter by name, id, or properties. "
                f"Segment: {match_content}..."
            )
    
    return warnings


def check_return_format(cypher: str) -> bool:
    """
    Verify the query ends with the required format:
    WITH collect(DISTINCT ...) AS nodes, collect(DISTINCT ...) AS edges RETURN nodes, edges;
    """
    # Normalize and check for required pattern
    cypher_lower = cypher.lower()
    
    # Must have WITH ... AS nodes ... AS edges
    if 'as nodes' not in cypher_lower or 'as edges' not in cypher_lower:
        return False
    
    # Must end with RETURN nodes, edges (allowing for semicolon and whitespace)
    if not re.search(r'return\s+nodes\s*,\s*edges\s*;?\s*$', cypher_lower):
        return False
    
    return True


def check_distinct_in_collect(cypher: str) -> bool:
    """
    Check that all collect() statements use DISTINCT.
    """
    # Find all collect() calls
    collect_calls = re.findall(r'collect\s*\([^)]+\)', cypher, re.IGNORECASE)
    
    if not collect_calls:
        return True  # No collect calls, pass by default
    
    # Check each collect call has DISTINCT
    for call in collect_calls:
        if 'distinct' not in call.lower():
            return False
    
    return True


def check_disease_naming(cypher: str) -> List[str]:
    """
    Check that disease references use 'type 1 diabetes' not T1D or other variants.
    """
    errors = []
    
    # Check for common incorrect variants
    # Pattern format: (regex_pattern, error_name, case_insensitive_flag)
    incorrect_patterns = [
        (r'\bT1D\b', "T1D", True),
        (r'\bType\s*1\s*Diabetes\b', "Type 1 Diabetes (should be lowercase)", False),  # case-sensitive to not match 'type 1 diabetes'
        (r'\btype\s*1\s*diabetic\b', "type 1 diabetic (should be 'type 1 diabetes')", True),
        (r'\bdiabetes\s*type\s*1\b', "diabetes type 1 (should be 'type 1 diabetes')", True),
    ]
    
    for pattern, name, case_insensitive in incorrect_patterns:
        flags = re.IGNORECASE if case_insensitive else 0
        if re.search(pattern, cypher, flags):
            errors.append(f"Use 'type 1 diabetes' instead of '{name}'")
    
    return errors


def check_variable_consistency(cypher: str) -> List[str]:
    """
    Check that all variables used in collect() are defined in MATCH clause.
    """
    warnings = []
    
    # Extract variables from MATCH clauses
    match_vars = set()
    match_patterns = re.findall(r'match\s+.*?(?=where|with|return|$)', cypher, re.IGNORECASE | re.DOTALL)
    
    for pattern in match_patterns:
        # Find node variables: (var:Label) or (var)
        node_vars = re.findall(r'\((\w+)(?::\w+)?[^)]*\)', pattern)
        match_vars.update(node_vars)
        
        # Find relationship variables: [var:Type] or [var]
        rel_vars = re.findall(r'\[(\w+)(?::\w+)?[^]]*\]', pattern)
        match_vars.update(rel_vars)
    
    # Extract variables from collect() calls
    collect_vars = set()
    collect_calls = re.findall(r'collect\s*\(\s*distinct\s+(\w+)\s*\)', cypher, re.IGNORECASE)
    collect_vars.update(collect_calls)
    
    # Check for undefined variables
    undefined = collect_vars - match_vars
    if undefined:
        warnings.append(f"Variables collected but not defined in MATCH: {', '.join(undefined)}")
    
    return warnings


def check_property_validity(cypher: str) -> List[str]:
    """
    Check that properties used in query exist in the schema.
    
    Returns list of warnings for potentially invalid properties.
    """
    warnings = []
    
    try:
        # Import schema loader to check properties
        try:
            from .schema_loader import get_schema, extract_entities_from_cypher
        except ImportError:
            from schema_loader import get_schema, extract_entities_from_cypher

        # Extract entities to know what to validate
        entities = extract_entities_from_cypher(cypher)
        schema = get_schema()
        
        # Build a map of valid properties for each entity
        valid_node_props = {}
        for node_label in entities['node_labels']:
            for key, spec in schema.get("node_types", {}).items():
                if key.split(";")[-1] == node_label:
                    valid_node_props[node_label] = set(spec.get("properties", {}).keys())
                    break
        
        valid_rel_props = {}
        for rel_type in entities['relationship_types']:
            for key, spec in schema.get("edge_types", {}).items():
                if key.split(";")[-1] == rel_type:
                    valid_rel_props[rel_type] = set(spec.get("properties", {}).keys())
                    break
        
        # Extract property accesses from cypher (pattern: var.property)
        property_accesses = re.findall(r'(\w+)\.(\w+)', cypher)
        
        for var, prop in property_accesses:
            # Try to determine if this is a node or relationship variable
            # This is heuristic-based and may not be perfect
            found_valid = False
            
            # Check against all known valid properties
            for props in valid_node_props.values():
                if prop in props:
                    found_valid = True
                    break
            
            if not found_valid:
                for props in valid_rel_props.values():
                    if prop in props:
                        found_valid = True
                        break
            
            # If property not found in any entity, warn
            if not found_valid and prop not in ['name', 'id']:  # Common properties
                warnings.append(f"Property '{prop}' on variable '{var}' may not exist in schema")
        
    except Exception:
        # If validation fails, don't block - just skip this check
        pass
    
    return warnings


def check_property_value_validity(cypher: str) -> List[str]:
    """
    Check that property values match the constrained valid values from valid_property_values.json.
    
    Returns list of errors for invalid property values.
    """
    errors = []
    
    try:
        # Import schema loader to get valid values
        try:
            from .schema_loader import get_valid_property_values, extract_entities_from_cypher
        except ImportError:
            from schema_loader import get_valid_property_values, extract_entities_from_cypher
        
        valid_values = get_valid_property_values()
        if not valid_values:
            return errors  # No constraints defined
        
        entities = extract_entities_from_cypher(cypher)
        
        # Build a map of variable names to their node types from MATCH patterns
        # Pattern: (var:Label) -> {'var': 'Label'}
        var_to_node_type = {}
        node_patterns = re.findall(r'\((\w+):(\w+)\)', cypher)
        for var_name, node_type in node_patterns:
            var_to_node_type[var_name] = node_type
        
        # Build a map of variable names to their relationship types from MATCH patterns
        # Pattern: [var:Type] -> {'var': 'Type'}
        var_to_rel_type = {}
        rel_patterns = re.findall(r'\[(\w+):(\w+)\]', cypher)
        for var_name, rel_type in rel_patterns:
            var_to_rel_type[var_name] = rel_type
        
        # Extract property value comparisons (pattern: var.property = 'value' or var.property='value')
        # Matches: ct.name='Beta Cell', d.name = 'type 1 diabetes', deg.UpOrDownRegulation='up'
        value_patterns = re.findall(r'(\w+)\.(\w+)\s*=\s*["\']([^"\']+)["\']', cypher, re.IGNORECASE)
        
        for var, prop, value in value_patterns:
            # Determine entity type by looking up the variable
            # Check if it's a node variable
            if var in var_to_node_type:
                node_label = var_to_node_type[var]
                if node_label in valid_values.get("node_properties", {}):
                    node_constraints = valid_values["node_properties"][node_label]
                    if prop in node_constraints:
                        constraint = node_constraints[prop]
                        valid_vals = constraint.get("values", [])
                        if valid_vals and value not in valid_vals:
                            note = constraint.get("note", "")
                            error_msg = f"Invalid value '{value}' for {node_label}.{prop}. Valid values: {valid_vals}"
                            if note:
                                error_msg += f". Note: {note}"
                            errors.append(error_msg)
            
            # Check if it's a relationship variable
            elif var in var_to_rel_type:
                rel_type = var_to_rel_type[var]
                if rel_type in valid_values.get("relationship_properties", {}):
                    rel_constraints = valid_values["relationship_properties"][rel_type]
                    if prop in rel_constraints:
                        constraint = rel_constraints[prop]
                        valid_vals = constraint.get("values", [])
                        if valid_vals and value not in valid_vals:
                            note = constraint.get("note", "")
                            error_msg = f"Invalid value '{value}' for {rel_type}.{prop}. Valid values: {valid_vals}"
                            if note:
                                error_msg += f". Note: {note}"
                            errors.append(error_msg)
        
    except Exception as e:
        # If validation fails, don't block - just skip this check
        pass
    
    return errors


def check_relationship_directions(cypher: str) -> List[str]:
    """
    Check that relationships connect the correct source and target node types according to schema.
    
    For example:
    - effector_gene_of should go from gene to disease
    - T1D_DEG_in should go from gene to anatomical_structure
    - part_of_QTL_signal should go from snv to gene
    
    Returns list of errors for incorrect relationship directions.
    """
    errors = []
    
    try:
        # Import schema loader
        try:
            from .schema_loader import get_schema
        except ImportError:
            from schema_loader import get_schema

        schema = get_schema()

        # Build a map of relationship types to their expected source/target
        rel_directions = {}
        for rel_key, rel_spec in schema.get("edge_types", {}).items():
            rel_type = rel_key.split(";")[-1]  # Get simple name
            source = rel_spec.get("source_node_type", "")
            target = rel_spec.get("target_node_type", "")
            rel_directions[rel_type] = {
                'source': source,
                'target': target,
                'full_key': rel_key
            }
        
        # Extract relationship patterns from Cypher
        # Patterns to match:
        # 1. (source:SourceLabel)-[rel:RelType]->(target:TargetLabel)
        # 2. (source:SourceLabel)<-[rel:RelType]-(target:TargetLabel)
        # 3. Complex patterns with multiple hops
        
        # Pattern 1: Forward direction (source)-[rel:Type]->(target)
        forward_patterns = re.findall(
            r'\((\w+):(\w+)\)\s*-\s*\[(?:\w+):(\w+)(?:[^\]]*)\]\s*->\s*\((\w+):(\w+)\)',
            cypher
        )
        
        for source_var, source_label, rel_type, target_var, target_label in forward_patterns:
            if rel_type in rel_directions:
                expected = rel_directions[rel_type]
                expected_source = expected['source']
                expected_target = expected['target']
                
                # Check if source and target match schema
                if source_label != expected_source or target_label != expected_target:
                    errors.append(
                        f"Relationship '{rel_type}' has incorrect direction. "
                        f"Found: ({source_label})-[:{rel_type}]->({target_label}), "
                        f"Expected: ({expected_source})-[:{rel_type}]->({expected_target})"
                    )
        
        # Pattern 2: Backward direction (target)<-[rel:Type]-(source)
        backward_patterns = re.findall(
            r'\((\w+):(\w+)\)\s*<-\s*\[(?:\w+):(\w+)(?:[^\]]*)\]\s*-\s*\((\w+):(\w+)\)',
            cypher
        )
        
        for target_var, target_label, rel_type, source_var, source_label in backward_patterns:
            if rel_type in rel_directions:
                expected = rel_directions[rel_type]
                expected_source = expected['source']
                expected_target = expected['target']
                
                # Check if source and target match schema (note: reversed in pattern)
                if source_label != expected_source or target_label != expected_target:
                    errors.append(
                        f"Relationship '{rel_type}' has incorrect direction. "
                        f"Found: ({target_label})<-[:{rel_type}]-({source_label}), "
                        f"Expected: ({expected_source})-[:{rel_type}]->({expected_target})"
                    )
        
        # Pattern 3: Undirected (should warn if relationship is directional in schema)
        undirected_patterns = re.findall(
            r'\((\w+):(\w+)\)\s*-\s*\[(?:\w+):(\w+)(?:[^\]]*)\]\s*-\s*\((\w+):(\w+)\)',
            cypher
        )
        
        # Filter out patterns already caught by forward/backward
        for node1_var, node1_label, rel_type, node2_var, node2_label in undirected_patterns:
            # Check if this pattern was already validated as directed
            already_checked = False
            for _, source_label, checked_rel, _, target_label in forward_patterns:
                if (checked_rel == rel_type and 
                    ((source_label == node1_label and target_label == node2_label) or
                     (source_label == node2_label and target_label == node1_label))):
                    already_checked = True
                    break
            
            if not already_checked and rel_type in rel_directions:
                expected = rel_directions[rel_type]
                expected_source = expected['source']
                expected_target = expected['target']
                
                # Check both possible directions
                if not ((node1_label == expected_source and node2_label == expected_target) or
                        (node2_label == expected_source and node1_label == expected_target)):
                    errors.append(
                        f"Relationship '{rel_type}' connects incorrect node types. "
                        f"Found: ({node1_label})-[:{rel_type}]-({node2_label}), "
                        f"Expected: ({expected_source})-[:{rel_type}]->({expected_target})"
                    )
        
    except Exception as e:
        # If validation fails, don't block - just skip this check
        pass
    
    return errors


def format_validation_report(validation: Dict) -> str:
    """
    Format validation results as a human-readable string for refinement prompts.
    """
    report = []
    
    report.append(f"Validation Score: {validation['score']}/100")
    
    if validation['errors']:
        report.append("\nERRORS (must fix):")
        for i, error in enumerate(validation['errors'], 1):
            report.append(f"  {i}. {error}")
    
    if validation['warnings']:
        report.append("\nWARNINGS (should fix):")
        for i, warning in enumerate(validation['warnings'], 1):
            report.append(f"  {i}. {warning}")
    
    if validation['passed_checks']:
        report.append("\nPASSED CHECKS:")
        for check in validation['passed_checks']:
            report.append(f"  ✓ {check}")
    
    return '\n'.join(report)


# ============================================================================
# Convenience Functions
# ============================================================================

def validate_and_fix_cypher(
    cypher: str,
    auto_fix: bool = True,
    default_limit: int = 100
) -> Dict[str, Any]:
    """
    Validate and optionally auto-fix a Cypher query.
    
    Args:
        cypher: The Cypher query to validate/fix
        auto_fix: Whether to apply automatic fixes (default: True)
        default_limit: LIMIT to add to prevent timeouts (default: 100)
    
    Returns:
        {
            'original_query': str,
            'fixed_query': str,
            'fixes_applied': list[str],
            'validation_before': dict,
            'validation_after': dict,
            'improvement': int  # Score improvement
        }
    """
    # Validate original query
    validation_before = validate_cypher(cypher)
    
    if not auto_fix:
        return {
            'original_query': cypher,
            'fixed_query': cypher,
            'fixes_applied': [],
            'validation_before': validation_before,
            'validation_after': validation_before,
            'improvement': 0
        }
    
    # Apply fixes
    fixed_query, fixes_applied = auto_fix_cypher(cypher, validation_before, default_limit)
    
    # Validate fixed query
    validation_after = validate_cypher(fixed_query)
    
    improvement = validation_after['score'] - validation_before['score']
    
    return {
        'original_query': cypher,
        'fixed_query': fixed_query,
        'fixes_applied': fixes_applied,
        'validation_before': validation_before,
        'validation_after': validation_after,
        'improvement': improvement
    }


if __name__ == "__main__":
    # Test cases
    test_queries = [
        # Good query
        """
        MATCH (g1:gene)-[reg:physical_interaction]->(g2:gene)
        WITH collect(DISTINCT g1)+collect(DISTINCT g2) AS nodes, collect(DISTINCT reg) AS edges
        RETURN nodes, edges;
        """,
        
        # Bad query - unnamed relationships
        """
        MATCH (g:gene)-[:`function_annotation;GO`]->(fo:gene_ontology)-[:T1D_DEG_in]->(ct:anatomical_structure)
        WITH collect(DISTINCT g)+collect(DISTINCT fo)+collect(DISTINCT ct) AS nodes,
             collect(DISTINCT r1)+collect(DISTINCT r2) AS edges
        RETURN nodes, edges;
        """,
        
        # Bad query - missing DISTINCT
        """
        MATCH (g:gene)-[r:physical_interaction]->(g2:gene)
        WITH collect(g)+collect(g2) AS nodes, collect(r) AS edges
        RETURN nodes, edges;
        """,
        
        # Bad query - incomplete (missing nodes and edges)
        """
        MATCH (g:gene)-[r:genetic_interaction]->(g2:gene)-[r2:T1D_DEG_in]->(ct:anatomical_structure)
        WHERE g.name = 'INS'
        WITH collect(DISTINCT g) AS nodes, collect(DISTINCT r) AS edges
        RETURN nodes, edges;
        """,
        
        # Bad query - single quotes (API gateway issue)
        """
        MATCH (g:gene)-[r:gene_detected_in]->(ct:anatomical_structure)
        WHERE g.name = 'INS'
        WITH collect(DISTINCT ct)+collect(DISTINCT g) AS nodes, collect(DISTINCT r) AS edges
        RETURN nodes, edges;
        """,
    ]
    
    for i, query in enumerate(test_queries, 1):
        print(f"\n{'='*60}")
        print(f"Test Query #{i}")
        print('='*60)
        
        result = validate_and_fix_cypher(query, auto_fix=True)
        
        print(f"\nOriginal Query:\n{result['original_query'].strip()}")
        print(f"\nValidation Score (Before): {result['validation_before']['score']}/100")
        
        if result['fixes_applied']:
            print(f"\nFixes Applied:")
            for fix in result['fixes_applied']:
                print(f"  • {fix}")
            
            print(f"\nFixed Query:\n{result['fixed_query'].strip()}")
            print(f"\nValidation Score (After): {result['validation_after']['score']}/100")
            print(f"Improvement: +{result['improvement']} points")
        else:
            print("\nNo fixes needed!")
        
        print(f"\n{format_validation_report(result['validation_after'])}")
