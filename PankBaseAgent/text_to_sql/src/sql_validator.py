"""
sql_validator.py
Validates and auto-fixes PostgreSQL queries for the genomic_interval table.
Mirrors cypher_validator.py but with SQL-specific rules.
"""

from __future__ import annotations

import re
from typing import Dict, List

VALID_TABLES = {
    "ensembl_genes_node",
    "gwas_snp_id_node",
    "ocr_peak_node",
    "qtl_snp_node",
}

VALID_ENTITY_TYPES = {
    "Ensembl_genes.node",
    "GWAS_snp_id.node",
    "ocr_peak.node",
    "QTL_snp.node",
}

VALID_CHROMOSOMES = {str(i) for i in range(1, 23)} | {"X", "Y"}

DESTRUCTIVE_KEYWORDS = re.compile(
    r"\b(DROP|DELETE|TRUNCATE|INSERT|UPDATE|ALTER|CREATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

# Reserved words that must be double-quoted in PostgreSQL
_RESERVED = {"chr", "start", "end"}

# Match unquoted occurrences of reserved words used as column identifiers.
# Looks for word boundaries around chr/start/end that are NOT already inside
# double-quotes.  We use a negative-lookbehind for '"' and negative-lookahead.
_UNQUOTED_RESERVED_RE = re.compile(
    r'(?<!")(?<!\w)\b(chr|start|end)\b(?!")(?!\w)',
    re.IGNORECASE,
)


def validate_sql(sql: str) -> Dict:
    """Score a SQL query 0-100 and return errors + auto-fixed version.

    Returns::

        {
            "score": int,
            "errors": List[str],
            "fixed_sql": str,
            "original_sql": str,
        }
    """
    score = 100
    errors: List[str] = []
    fixed = sql.strip().rstrip(";").strip()

    # --- Destructive statement check (critical, instant 0) ----------------
    if DESTRUCTIVE_KEYWORDS.search(fixed):
        return {
            "score": 0,
            "errors": ["Query contains destructive statement (DROP/DELETE/INSERT/UPDATE/ALTER)"],
            "fixed_sql": sql,
            "original_sql": sql,
        }

    # --- Basic SELECT check -----------------------------------------------
    if not re.match(r"\s*SELECT\b", fixed, re.IGNORECASE):
        errors.append("Query does not start with SELECT")
        score -= 40

    if not re.search(r"\bFROM\b", fixed, re.IGNORECASE):
        errors.append("Query missing FROM clause")
        score -= 30

    # --- Table name check -------------------------------------------------
    referenced_tables = {t for t in VALID_TABLES if re.search(rf"\b{t}\b", fixed)}
    if not referenced_tables:
        errors.append(
            "Query does not reference any known entity table "
            f"({', '.join(sorted(VALID_TABLES))})"
        )
        score -= 20
    if re.search(r"\bgenomic_interval\b", fixed):
        errors.append(
            "Query references legacy 'genomic_interval' table — use entity-specific tables "
            "(ensembl_genes_node, gwas_snp_id_node, ocr_peak_node, qtl_snp_node)"
        )
        score -= 40

    # --- Reserved-word quoting --------------------------------------------
    unquoted = _UNQUOTED_RESERVED_RE.findall(fixed)
    if unquoted:
        unique = sorted(set(w.lower() for w in unquoted))
        errors.append(f"Unquoted reserved words: {', '.join(unique)} — must use double-quotes")
        score -= 10 * len(unique)
        # Auto-fix: quote them
        def _quote_reserved(m: re.Match) -> str:
            return f'"{m.group(1)}"'
        fixed = _UNQUOTED_RESERVED_RE.sub(_quote_reserved, fixed)

    # --- WHERE clause check -----------------------------------------------
    if not re.search(r"\bWHERE\b", fixed, re.IGNORECASE):
        # Allow aggregate queries (COUNT, SUM, etc.) without WHERE
        if not re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", fixed, re.IGNORECASE):
            errors.append("Query has no WHERE clause — may scan entire table")
            score -= 15

    # --- LIMIT check ------------------------------------------------------
    if not re.search(r"\bLIMIT\b", fixed, re.IGNORECASE):
        # Allow aggregate queries without LIMIT
        if not re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", fixed, re.IGNORECASE):
            errors.append("Missing LIMIT clause")
            score -= 10
            # Auto-fix: append LIMIT 100
            fixed = fixed + " LIMIT 100"

    # --- Entity type validation -------------------------------------------
    entity_literals = re.findall(r"entity_type\s*=\s*'([^']+)'", fixed, re.IGNORECASE)
    for et in entity_literals:
        if et not in VALID_ENTITY_TYPES:
            errors.append(f"Invalid entity_type '{et}' — valid: {', '.join(sorted(VALID_ENTITY_TYPES))}")
            score -= 15

    # --- Chromosome validation --------------------------------------------
    chr_literals = re.findall(r'"chr"\s*=\s*\'([^\']+)\'', fixed)
    for c in chr_literals:
        if c not in VALID_CHROMOSOMES:
            errors.append(f"Invalid chromosome '{c}' — valid: 1-22, X, Y")
            score -= 10

    # Clamp score
    score = max(0, score)

    return {
        "score": score,
        "errors": errors,
        "fixed_sql": fixed + ";",
        "original_sql": sql,
    }


def format_validation_report(result: Dict) -> str:
    """Human-readable summary of a validation result."""
    lines = [f"Score: {result['score']}/100"]
    if result["errors"]:
        lines.append("Errors:")
        for e in result["errors"]:
            lines.append(f"  - {e}")
    if result["fixed_sql"] != result["original_sql"]:
        lines.append(f"Auto-fixed SQL: {result['fixed_sql']}")
    return "\n".join(lines)
