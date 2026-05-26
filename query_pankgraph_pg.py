"""
PanKgraph PostgreSQL genomic coordinate queries.
Usage: python3 query_pankgraph_pg.py
"""

import psycopg2

DB = dict(host="127.0.0.1", port=5432, user="postgres", password="password", dbname="pankgraph", gssencmode="disable")


def run(cur, title, sql, params=None):
    print(f"\n=== {title} ===")
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    print("  " + " | ".join(cols))
    print("  " + "-" * 60)
    for row in rows:
        print(" ", row)
    print(f"  ({len(rows)} rows)")
    return rows


def main():
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()

    # 1. Find INS gene by Ensembl ID
    run(cur, "1. INS gene by Ensembl ID", """
        SELECT id, chr, start, "end"
        FROM ensembl_genes_node
        WHERE id = 'ENSG00000254647'
    """)

    # 2. First 5 genes on chr11
    run(cur, "2. Genes on chr11 (first 5)", """
        SELECT id, chr, start, "end"
        FROM ensembl_genes_node
        WHERE chr = '11'
        ORDER BY start
        LIMIT 5
    """)

    # 3. OCR peaks overlapping INS locus (chr11:2,160,000-2,162,000)
    run(cur, "3. OCR peaks overlapping INS locus (chr11:2160000-2162000)", """
        SELECT id, chr, start, "end"
        FROM ocr_peak_node
        WHERE chr = '11'
          AND start <= 2162000
          AND "end" >= 2160000
        ORDER BY start
    """)

    # 4. GWAS SNPs on chr6
    run(cur, "4. GWAS SNPs on chr6 (first 5)", """
        SELECT id, chr, start, "end"
        FROM gwas_snp_id_node
        WHERE chr = '6'
        ORDER BY start
        LIMIT 5
    """)

    # 5. QTL SNPs within 1Mb of INS gene
    run(cur, "5. QTL SNPs within 1Mb of INS gene", """
        SELECT q.id, q.chr, q.start, q."end"
        FROM qtl_snp_node q
        JOIN ensembl_genes_node g ON g.id = 'ENSG00000254647'
        WHERE q.chr = g.chr
          AND q.start BETWEEN g.start - 1000000 AND g."end" + 1000000
        ORDER BY q.start
    """)

    # 6. Entity counts per chromosome
    run(cur, "6. Entity counts per chromosome", """
        SELECT * FROM (
            SELECT 'gene'     AS type, chr, count(*) AS n FROM ensembl_genes_node GROUP BY chr
            UNION ALL
            SELECT 'gwas_snp' AS type, chr, count(*) AS n FROM gwas_snp_id_node   GROUP BY chr
            UNION ALL
            SELECT 'qtl_snp'  AS type, chr, count(*) AS n FROM qtl_snp_node       GROUP BY chr
        ) t
        ORDER BY type, n DESC
    """)

    # 7. OCR peaks overlapping GWAS SNPs (first 10 + total count)
    run(cur, "7. OCR peaks overlapping GWAS SNPs (first 10)", """
        SELECT s.id AS snp_id, o.id AS ocr_id, s.chr, s.start AS snp_pos
        FROM gwas_snp_id_node s
        JOIN ocr_peak_node o
          ON o.chr = s.chr
         AND o.start <= s.start
         AND o."end" >= s.start
        ORDER BY s.chr, s.start
        LIMIT 10
    """)

    cur.execute("""
        SELECT count(*) FROM gwas_snp_id_node s
        JOIN ocr_peak_node o
          ON o.chr = s.chr
         AND o.start <= s.start
         AND o."end" >= s.start
    """)
    print(f"  Total GWAS-SNP / OCR overlaps: {cur.fetchone()[0]:,}")

    conn.close()


if __name__ == "__main__":
    main()
