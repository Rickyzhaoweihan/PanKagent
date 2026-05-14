"""
text2sql_agent.py
LLM agent that translates natural language to PostgreSQL queries
for the PanKgraph genomic_interval table.

Mirrors text2cypher_agent.py — uses the same vLLM infrastructure.
"""

from __future__ import annotations

import os
import re
import sys

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from .pg_schema_loader import get_pg_schema_for_llm
from .sql_validator import validate_sql, format_validation_report
from .gene_resolver import resolve_gene_names_in_text

load_dotenv()

SQL_SYSTEM_RULES = """You are a PostgreSQL query generator for the PanKgraph genomic coordinate database.

TASK: Generate ONE PostgreSQL SELECT query per question.
- Read the schema carefully — there are FOUR entity-specific tables, NOT one unified table.
- Generate focused, specific queries
- ALWAYS use double-quotes around "chr", "start", "end" (reserved words)

IMPORTANT — Gene Name Resolution:
- If you see an Ensembl ID like ENSG00000254647 in the question, use it directly:
    WHERE id = 'ENSG00000254647'
- If you only have a gene symbol (INS, CFTR, MAFA), query ensembl_genes_node by gene_name:
    WHERE gene_name = 'INS'
- Both paths are valid; prefer gene_name when only a symbol is given.

RULES:
- Generate ONLY read-only SELECT queries
- ALWAYS add LIMIT (default 100) unless the query uses COUNT/SUM/AVG
- Use single quotes for string literals
- Use double-quotes for column names that are reserved words
- Choose the correct table for the entity type — never use a non-existent table

OVERLAP PATTERN (find OCR peaks overlapping a region):
SELECT id, "chr", "start", "end"
FROM ocr_peak_node
WHERE "chr" = '11' AND "start" <= 2162000 AND "end" >= 2160000
LIMIT 100;

ENTITY LOOKUP (find gene by Ensembl ID):
SELECT id, gene_name, "chr", "start", "end"
FROM ensembl_genes_node
WHERE id = 'ENSG00000254647'
LIMIT 10;

ENTITY LOOKUP (find gene by symbol):
SELECT id, gene_name, "chr", "start", "end"
FROM ensembl_genes_node
WHERE gene_name = 'INS'
LIMIT 10;

CROSS-ENTITY OVERLAP (OCR peaks overlapping GWAS SNPs):
SELECT g.id AS gwas_id, o.id AS ocr_id, g."chr", g."start" AS gwas_pos
FROM gwas_snp_id_node g
JOIN ocr_peak_node o ON o."chr" = g."chr" AND o."start" <= g."start" AND o."end" >= g."start"
LIMIT 100;

PROXIMITY (QTL SNPs within 1Mb of a gene):
SELECT q.id, q."chr", q."start", q."end"
FROM qtl_snp_node q
JOIN ensembl_genes_node g ON g.id = 'ENSG00000254647'
WHERE q."chr" = g."chr"
  AND q."start" BETWEEN g."start" - 1000000 AND g."end" + 1000000
LIMIT 100;

COUNT BY CHROMOSOME:
SELECT "chr", count(*) AS n
FROM ensembl_genes_node
GROUP BY "chr"
ORDER BY n DESC;

WRONG (DO NOT DO):
WRONG: SELECT * FROM ocr_peak_node;  (no WHERE, no LIMIT — scans 5.4M rows!)
WRONG: WHERE chr = '11'  (unquoted reserved word — must be "chr")
WRONG: FROM genomic_interval  (table no longer exists — use entity-specific tables)

Schema:
"""


def make_llm(provider: str = "local"):
    """Return a Chat LLM instance."""
    if provider == "local":
        vllm_port = os.environ.get("VLLM_PORT", "8002")
        return ChatOpenAI(
            base_url=f"http://localhost:{vllm_port}/v1",
            api_key="EMPTY",
            model="cypher-writer",
            temperature=0,
        )
    elif provider == "openai":
        return ChatOpenAI(
            base_url=os.environ.get("OPENAI_API_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.environ.get("OPENAI_API_MODEL", "gpt-4o"),
            temperature=0,
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")


class Text2SQLAgent:
    """Single-LLM agent that translates NL to PostgreSQL for the genomic coordinate tables."""

    def __init__(
        self,
        provider: str = "local",
        enable_refinement: bool = True,
        max_refinement_iterations: int = 5,
        min_acceptable_score: int = 90,
    ):
        self.provider = provider
        self.enable_refinement = enable_refinement
        self.max_refinement_iterations = max_refinement_iterations
        self.min_acceptable_score = min_acceptable_score

        self.schema = get_pg_schema_for_llm()
        system_prompt = SQL_SYSTEM_RULES + self.schema

        self.llm = make_llm(provider)
        self.prompt = ChatPromptTemplate.from_messages([
            ("human", system_prompt + "\nQuestion: {user_input}\n\n"),
        ])
        self.chain = self.prompt | self.llm

    def _resolve_genes(self, text: str) -> tuple[str, dict[str, str]]:
        """Replace gene symbols in *text* with Ensembl IDs via RDS Lambda."""
        try:
            return resolve_gene_names_in_text(text)
        except Exception:
            return text, {}

    def respond(self, user_text: str) -> str:
        """Generate a SQL query from natural language.

        Gene names in the question are automatically resolved to Ensembl IDs
        before the LLM sees the prompt.
        """
        resolved_text, _ = self._resolve_genes(user_text)
        result = self.chain.invoke({"user_input": resolved_text})
        return self._clean_sql(result.content)

    def respond_with_refinement(self, user_text: str, max_iterations: int | None = None) -> dict:
        """Generate SQL with iterative validation + refinement.

        Gene names in the question are automatically resolved to Ensembl IDs
        before the LLM sees the prompt.

        Returns::

            {
                "sql": str,
                "score": int,
                "iteration": int,
                "all_attempts": list,
                "validation_report": dict,
                "resolved_genes": dict,
            }
        """
        if max_iterations is None:
            max_iterations = self.max_refinement_iterations

        # Pre-resolve gene names → Ensembl IDs
        resolved_text, resolved_genes = self._resolve_genes(user_text)

        all_attempts: list[dict] = []
        best_result = None
        best_score = -1

        # Iteration 1: initial generation
        sql = self.respond(resolved_text)
        validation = validate_sql(sql)

        # Use auto-fixed version if available
        if validation["fixed_sql"] != validation["original_sql"]:
            sql = validation["fixed_sql"]
            validation = validate_sql(sql)

        attempt = {
            "iteration": 1,
            "sql": sql,
            "score": validation["score"],
            "validation": validation,
        }
        all_attempts.append(attempt)

        if validation["score"] > best_score:
            best_score = validation["score"]
            best_result = attempt

        if validation["score"] >= self.min_acceptable_score:
            return self._make_result(best_result, all_attempts, resolved_genes)

        # Iterations 2-N: refinement loop
        for iteration in range(2, max_iterations + 1):
            prev = all_attempts[-1]
            if prev["score"] >= self.min_acceptable_score and not prev["validation"]["errors"]:
                break

            refinement_prompt = self._build_refinement_prompt(
                user_text, prev["sql"], prev["validation"]
            )
            sql = self._generate_with_refinement_prompt(refinement_prompt)
            validation = validate_sql(sql)

            if validation["fixed_sql"] != validation["original_sql"]:
                sql = validation["fixed_sql"]
                validation = validate_sql(sql)

            attempt = {
                "iteration": iteration,
                "sql": sql,
                "score": validation["score"],
                "validation": validation,
            }
            all_attempts.append(attempt)

            if validation["score"] > best_score:
                best_score = validation["score"]
                best_result = attempt

            if validation["score"] >= self.min_acceptable_score:
                break

        return self._make_result(best_result, all_attempts, resolved_genes)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_result(best: dict, all_attempts: list[dict],
                     resolved_genes: dict | None = None) -> dict:
        result = {
            "sql": best["sql"],
            "score": best["score"],
            "iteration": best["iteration"],
            "all_attempts": all_attempts,
            "validation_report": best["validation"],
        }
        if resolved_genes:
            result["resolved_genes"] = resolved_genes
        return result

    @staticmethod
    def _clean_sql(raw: str) -> str:
        """Strip markdown fences, prefixes, and whitespace from model output."""
        sql = raw.strip()

        # Remove markdown code fences
        if sql.startswith("```"):
            sql = sql.split("\n", 1)[1] if "\n" in sql else sql[3:]
        if sql.endswith("```"):
            sql = sql.rsplit("```", 1)[0]
        sql = sql.strip()

        # Remove common prefixes
        for prefix in ("sql", "SQL", "postgresql", "PostgreSQL"):
            if sql.lower().startswith(prefix.lower()):
                sql = sql[len(prefix):].strip()

        # Remove echoed question
        query_prefix = re.match(r'^(?:Query|Question):\s*[\'"].*?[\'"]\s*\n?', sql, re.IGNORECASE)
        if query_prefix:
            sql = sql[query_prefix.end():].strip()

        return sql

    def _build_refinement_prompt(self, original_question: str,
                                  previous_sql: str, validation: dict) -> str:
        errors_text = "\n".join(f"  - {e}" for e in validation["errors"])
        if not errors_text:
            errors_text = "  - Low score but no specific errors detected"

        return (
            f"Previous attempt (Score: {validation['score']}/100):\n"
            f"{previous_sql}\n\n"
            f"Fix these errors:\n{errors_text}\n\n"
            f"Question: {original_question}\n\n"
            f"Generate corrected PostgreSQL query."
        )

    def _generate_with_refinement_prompt(self, refinement_prompt: str) -> str:
        schema_section = f"Schema:\n{self.schema}\n\n"
        full_prompt = schema_section + refinement_prompt + "\n\n"
        result = self.llm.invoke(full_prompt)
        return self._clean_sql(result.content)


if __name__ == "__main__":
    agent = Text2SQLAgent()
    try:
        while True:
            txt = input("You> ").strip()
            if not txt:
                continue
            result = agent.respond_with_refinement(txt)
            print(f"SQL (score {result['score']}/100, iter {result['iteration']}):")
            print(result["sql"])
            if result["validation_report"]["errors"]:
                print("Warnings:", result["validation_report"]["errors"])
            print()
    except (KeyboardInterrupt, EOFError):
        pass
