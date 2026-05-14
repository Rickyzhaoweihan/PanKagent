"""
Test the text-to-SQL pipeline standalone.

Usage:
    python3 PankBaseAgent/text_to_sql/test_text2sql.py
    python3 PankBaseAgent/text_to_sql/test_text2sql.py "your question here"
"""

import json
import sys
import os
import time

import psycopg2
import psycopg2.extras

# Ensure the package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.text2sql_agent import Text2SQLAgent
from src.sql_validator import validate_sql, format_validation_report

DB_CONFIG = dict(
    host="localhost",
    port=5432,
    user="serviceuser",
    password="password",
    dbname="pankgraph",
)

TEST_QUESTIONS = [
    "What is the genomic location of gene ENSG00000254647?",
    "What is the genomic location of INS?",
    "How many OCR peaks are on chromosome 11?",
    "Which OCR peaks overlap the region chr11:2160000-2162000?",
    "Find GWAS SNPs on chromosome 6",
    "Find QTL SNPs within 1Mb of gene ENSG00000254647",
    "How many genes are there per chromosome?",
]


def execute_sql(sql: str) -> list[dict]:
    """Execute SQL and return rows as dicts."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def run_test(agent: Text2SQLAgent, question: str, index: int) -> dict:
    """Run one test case: generate SQL, validate, execute."""
    print(f"\n{'='*70}")
    print(f"Test {index}: {question}")
    print("=" * 70)

    t0 = time.time()
    result = agent.respond_with_refinement(question)
    gen_time = time.time() - t0

    sql = result["sql"]
    score = result["score"]
    iteration = result["iteration"]

    print(f"\nGenerated SQL (score={score}/100, iter={iteration}, {gen_time:.1f}s):")
    print(f"  {sql}")

    if result["validation_report"]["errors"]:
        print("Validation errors:")
        for e in result["validation_report"]["errors"]:
            print(f"  - {e}")

    # Try executing
    rows = None
    exec_error = None
    try:
        t1 = time.time()
        rows = execute_sql(sql)
        exec_time = time.time() - t1
        print(f"\nExecution: {len(rows)} rows returned ({exec_time:.2f}s)")
        if rows:
            # Show first 3 rows
            for i, row in enumerate(rows[:3]):
                print(f"  [{i+1}] {row}")
            if len(rows) > 3:
                print(f"  ... ({len(rows) - 3} more rows)")
    except Exception as e:
        exec_error = str(e)
        print(f"\nExecution ERROR: {exec_error}")

    return {
        "question": question,
        "sql": sql,
        "score": score,
        "iteration": iteration,
        "gen_time_s": round(gen_time, 2),
        "rows_returned": len(rows) if rows is not None else None,
        "exec_error": exec_error,
    }


def main():
    # Determine questions to test
    if len(sys.argv) > 1:
        questions = [" ".join(sys.argv[1:])]
    else:
        questions = TEST_QUESTIONS

    print("Initializing Text2SQLAgent...")
    try:
        agent = Text2SQLAgent(provider="local")
        print("Agent ready (using local vLLM)")
    except Exception as e:
        print(f"Failed to init local agent: {e}")
        print("Falling back to OpenAI provider...")
        try:
            agent = Text2SQLAgent(provider="openai")
            print("Agent ready (using OpenAI)")
        except Exception as e2:
            print(f"Failed to init OpenAI agent: {e2}")
            sys.exit(1)

    # Verify PostgreSQL connection
    try:
        rows = execute_sql(
            "SELECT (SELECT count(*) FROM ensembl_genes_node) AS genes,"
            "       (SELECT count(*) FROM gwas_snp_id_node) AS gwas,"
            "       (SELECT count(*) FROM ocr_peak_node) AS ocrs,"
            "       (SELECT count(*) FROM qtl_snp_node) AS qtls"
        )
        r = rows[0]
        print(
            f"PostgreSQL connected: genes={r['genes']:,}  gwas={r['gwas']:,}  "
            f"ocrs={r['ocrs']:,}  qtls={r['qtls']:,}"
        )
    except Exception as e:
        print(f"PostgreSQL connection failed: {e}")
        sys.exit(1)

    # Run tests
    results = []
    for i, q in enumerate(questions, 1):
        r = run_test(agent, q, i)
        results.append(r)

    # Summary
    print(f"\n\n{'='*70}")
    print("SUMMARY")
    print("=" * 70)
    for r in results:
        status = "OK" if r["rows_returned"] is not None and r["rows_returned"] > 0 else "FAIL"
        if r["exec_error"]:
            status = "ERROR"
        print(
            f"  [{status:5}] score={r['score']:3}/100 iter={r['iteration']} "
            f"rows={r['rows_returned'] or 'N/A':>6}  {r['question'][:50]}"
        )


if __name__ == "__main__":
    main()
