#!/usr/bin/env python3
"""Read-only six-template acceptance; stdout contains sanitized JSON only.

Run with the isolated results Python, --env-file pointing to the existing
protected vNext environment and --app-dir pointing to the results checkout.
No model API, deployment tool, service restart or database write is invoked.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time
import warnings


async def run(app_dir):
    from pankagent_vnext.config import Settings
    from pankgraph_results.query import QueryService

    settings = Settings()
    service = QueryService(settings)
    started = time.monotonic()
    report = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "graph_version": settings.graph_version, "inference_calls": 0,
              "query_source_sha256": hashlib.sha256((app_dir / "pankgraph_results/query.py").read_bytes()).hexdigest(),
              "tests": []}
    try:
        await service.graph._ensure_identity()
        lookup = service.graph._small_query
        qtl = await lookup("MATCH (v:sequence_variant)-[r:PART_OF_QTL_SIGNAL]->(g:Gene) "
                           "WHERE g.name IN $names RETURN g.id AS gene_id,v.id AS variant_id,"
                           "r.credible_set AS credible_set_id,r.data_source AS data_source LIMIT 1",
                           {"names": ["GCLC", "CFTR", "INS"]})
        gwas = await lookup("MATCH (v:sequence_variant)-[r:PART_OF_GWAS_SIGNAL]->(d:disease) "
                            "RETURN v.id AS variant_id,d.id AS disease_id,r.credible_set_id AS credible_set_id,"
                            "r.data_source AS data_source LIMIT 1")
        coloc = await lookup("MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease) WHERE g.name IN $names "
                             "RETURN g.id AS gene_id,d.id AS disease_id,r.data_source AS data_source LIMIT 1",
                             {"names": ["ADCY3", "GCLC", "CFTR", "INS"]})
        expression = await lookup("MATCH (g:Gene {name:$name})-[r:GENE_DETECTED_IN|GENE_ENRICHED_IN]->"
                                  "(c:anatomical_structure) RETURN g.id AS gene_id LIMIT 1", {"name": "INS"})
        selections = {"qtl": bool(qtl), "gwas": bool(gwas), "coloc": bool(coloc), "expression": bool(expression)}
        report["bounded_seed_lookup"] = selections
        if not all(selections.values()):
            report.update(status="blocked", reason="known_instance_unavailable")
            return report
        tests = [
            ("qtl_by_gene", {"gene_id": qtl[0]["gene_id"]}, False),
            ("qtl_by_variant_gene", qtl[0], False),
            ("qtl_by_variant", {key: value for key, value in qtl[0].items() if key != "gene_id"}, False),
            ("gwas_by_variant", gwas[0], False),
            ("coloc_by_gene", coloc[0], False),
            ("expression_by_gene", expression[0], False),
            ("expression_by_gene", {**expression[0], "cell_id": "__acceptance_missing_cell__"}, True),
            ("gwas_by_variant", {**gwas[0], "disease_id": "__acceptance_missing_disease__"}, True),
        ]
        for template, params, expect_empty in tests:
            begin = time.monotonic()
            row = {"template": template, "case": "empty_filter" if expect_empty else "known_match"}
            try:
                evidence = await asyncio.wait_for(service.execute(template, params), 45)
                row.update(node_count=len(evidence["nodes"]), edge_count=len(evidence["edges"]),
                           row_count=len(evidence["rows"]), completeness=evidence["completeness"],
                           truncated=evidence["truncated"], query_count=len(evidence["queries"]),
                           steps=[{"purpose": step["purpose"], "status": step["status"],
                                   "nodes": len(step["nodes"]), "edges": len(step["edges"]),
                                   "expansion": step.get("expansion"), "error": step.get("error")}
                                  for step in evidence["steps"]])
                row["passed"] = (evidence["completeness"] == "empty" and not evidence["nodes"] and not evidence["edges"]
                                 if expect_empty else bool(evidence["nodes"] and evidence["edges"]) and
                                 all(step["status"] != "failed" for step in evidence["steps"]))
            except Exception as exc:
                row.update(passed=False, error_category=type(exc).__name__)
            row["elapsed_ms"] = round((time.monotonic()-begin)*1000, 2)
            report["tests"].append(row)
        report["query_count"] = service.calls
        report["status"] = "passed" if all(row["passed"] for row in report["tests"]) else "failed"
        return report
    finally:
        await service.close()
        report["elapsed_ms"] = round((time.monotonic()-started)*1000, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--app-dir", required=True, type=Path)
    args = parser.parse_args()
    # Literal dotenv parsing only. Do not source shell code or read Claude keys.
    for line in args.env_file.read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.startswith(("PANK_VNEXT_NEO4J_", "PANK_VNEXT_GRAPH_")):
            words = shlex.split(value)
            os.environ[key] = words[0] if words else ""
    sys.path.insert(0, str(args.app_dir))
    warnings.filterwarnings("ignore")
    try:
        report = asyncio.run(run(args.app_dir))
    except Exception as exc:
        report = {"status": "failed", "error_category": type(exc).__name__, "inference_calls": 0}
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
