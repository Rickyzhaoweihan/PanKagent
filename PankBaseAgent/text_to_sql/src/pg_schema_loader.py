"""
pg_schema_loader.py
Compact PostgreSQL schema string for the text-to-SQL LLM prompt.
Mirrors schema_loader.py from text_to_cypher but for the genomic_interval table.
"""

from __future__ import annotations

_cached_schema: str | None = None


def get_pg_schema_for_llm() -> str:
    """Return ultra-compact PostgreSQL schema string optimized for small models.

    Keeps token usage low (~250 tokens) while providing all necessary context
    for generating correct SQL against the four entity-specific tables.
    """
    global _cached_schema
    if _cached_schema is None:
        _cached_schema = (
            "Tables:\n"
            '  ensembl_genes_node(id text, entity_type text, "chr" text, "start" bigint, "end" bigint, gene_name text)\n'
            "    — 78,687 rows. id is Ensembl gene ID (e.g. ENSG00000254647). gene_name is HGNC symbol (e.g. 'INS', 'CFTR').\n"
            '  gwas_snp_id_node(id text, entity_type text, "chr" text, "start" bigint, "end" bigint)\n'
            "    — 1,615 rows. id is rsID (e.g. rs1050976).\n"
            '  ocr_peak_node(id text, entity_type text, "chr" text, "start" bigint, "end" bigint)\n'
            "    — 5,294,421 rows. id like CL_0000169_1_100008394_100008769.\n"
            '  qtl_snp_node(id text, entity_type text, "chr" text, "start" bigint, "end" bigint)\n'
            "    — 19,422 rows. id is rsID (e.g. rs10004120).\n"
            "\n"
            "Chromosomes: 1..22, X, Y\n"
            "\n"
            "Notes:\n"
            '- ALWAYS double-quote "chr", "start", "end" — PostgreSQL reserved words\n'
            '- Overlap test between tables a and b: a."chr" = b."chr" AND a."start" <= b."end" AND a."end" >= b."start"\n'
            "- entity_type column exists in each table for compatibility but is redundant — the table IS the entity\n"
            "- ensembl_genes_node has gene_name — prefer WHERE gene_name = 'INS' when the user gives a symbol\n"
            "- Always add LIMIT for exploratory queries (default LIMIT 100)\n"
        )
    return _cached_schema
