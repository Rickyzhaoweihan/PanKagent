"""Offline routing timings with synthetic canonical graph records only.

No model client, credentials, network connection, or graph database is used.
Constructor/file loading, first selection, and warmed selection are measured
separately. Fresh router instances do not imply an uncached operating-system
filesystem; this benchmark does not flush OS caches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


NODE_LABELS = (
    "Gene", "variants", "OCR_peak", "GO_term", "kegg", "reactome",
    "anatomical_structure", "Sample", "donor", "data_modality",
)
EDGE_TEMPLATES = (
    ("gene_detected_in", "Gene", "anatomical_structure"),
    ("gene_enriched_in", "Gene", "anatomical_structure"),
    ("T1D_DEG_in", "Gene", "anatomical_structure"),
    ("gene_activity_score_in", "Gene", "anatomical_structure"),
    ("OCR_peak_in", "OCR_peak", "anatomical_structure"),
    ("effector_gene_of", "Gene", "variants"),
    ("physical_interaction", "Gene", "Gene"),
    ("genetic_interaction", "Gene", "Gene"),
    ("function_annotation", "Gene", "GO_term"),
    ("fGSEA_enriched_in", "reactome", "anatomical_structure"),
    ("marker_gene_of", "Gene", "anatomical_structure"),
    ("part_of_GWAS_signal", "variants", "variants"),
    ("signal_COLOC_with", "variants", "variants"),
    ("part_of_QTL_signal", "variants", "Gene"),
    ("pathway_annotation", "Gene", "kegg"),
    ("CMDKP_effector_gene_of", "Gene", "variants"),
    ("pathway_annotation;reactome", "Gene", "reactome"),
    ("ASSOCIATED_WITH_GO", "Gene", "GO_term"),
    ("gene_detected_in", "Gene", "anatomical_structure"),
    ("gene_activity_score_in", "Gene", "anatomical_structure"),
)


def build_evidence(node_count: int, edge_count: int) -> dict:
    """Canonical record shape with valid endpoint IDs, not biological claims."""
    if node_count < len(NODE_LABELS) or edge_count < 0:
        raise ValueError("fixture requires at least ten nodes and nonnegative edges")
    nodes, groups = [], defaultdict(list)
    for index in range(node_count):
        label = NODE_LABELS[index % len(NODE_LABELS)]
        identity = f"synthetic-node-{index:05d}"
        properties = {
            "name": f"Synthetic {label} {index}",
            "data_source": "offline-routing-fixture",
            "data_version": "fixture-v1",
            "description": "Synthetic schema coverage; not a scientific observation.",
        }
        if label == "donor":
            properties.update({"t1d_stage": "ND", "INS-G 16.7 SI": 1.25,
                               "trait_meta": {"feature_name": "GCG-KCl 20 SI", "unit": "fold change"}})
        nodes.append({"id": identity, "labels": [label], "properties": properties})
        groups[label].append(identity)
    edges = []
    for index in range(edge_count):
        kind, source_label, target_label = EDGE_TEMPLATES[index % len(EDGE_TEMPLATES)]
        source, target = groups[source_label], groups[target_label]
        group_index = index // len(EDGE_TEMPLATES)
        edges.append({
            "start_id": source[group_index % len(source)],
            "end_id": target[(group_index * 7 + 1) % len(target)],
            "type": kind,
            "properties": {
                "data_source": "offline-routing-fixture", "data_version": "fixture-v1",
                "condition": "ND", "cell_type": "synthetic context",
                "nominal_p": 0.05, "effect_direction": "synthetic",
                "fixture_edge_index": index,
            },
        })
    return {"synthetic-step": {
        "step_id": "synthetic-step", "question": "Offline schema-routing benchmark.",
        "status": "complete", "truncated": False, "graph_version": "synthetic-fixture-v1",
        "nodes": nodes, "edges": edges, "rows": [], "validation": [{"valid": True}],
    }}


def summary(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "samples": len(values),
        "median": round(statistics.median(values), 6),
        "p95": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 6),
        "min": round(ordered[0], 6),
        "max": round(ordered[-1], 6),
    }


def select_sample(router, evidence):
    started = time.perf_counter_ns()
    result = router.select(evidence)
    wall_ms = (time.perf_counter_ns() - started) / 1_000_000
    if not isinstance(result.guidance, str) or not isinstance(result.profile, dict):
        raise ValueError("router returned an invalid RoutedSkills contract")
    timings = result.profile.get("timing_ms", {})
    if any(not isinstance(timings.get(key), (int, float)) for key in ("scan", "compile", "total")):
        raise ValueError("router profile must report numeric scan/compile/total timing_ms")
    encoded = result.guidance.encode("utf-8")
    return {
        "wall_ms": wall_ms, "timing_ms": dict(timings),
        "cache_hit": result.profile.get("cache_hit") is True,
        "guidance_bytes": len(encoded), "guidance_sha256": hashlib.sha256(encoded).hexdigest(),
        "profile": result.profile,
    }


def summarize_samples(samples: list[dict]) -> dict:
    return {
        "selection_wall_ms": summary([sample["wall_ms"] for sample in samples]),
        "profile_timing_ms": {key: summary([sample["timing_ms"][key] for sample in samples]) for key in ("scan", "compile", "total")},
        "cache_hits": sum(sample["cache_hit"] for sample in samples),
        "cache_misses": sum(not sample["cache_hit"] for sample in samples),
        "guidance_bytes": {"min": min(sample["guidance_bytes"] for sample in samples), "max": max(sample["guidance_bytes"] for sample in samples)},
        "guidance_identical_across_runs": len({sample["guidance_sha256"] for sample in samples}) == 1,
        "guidance_sha256": samples[-1]["guidance_sha256"],
        "last_observed_profile": samples[-1]["profile"],
    }


def benchmark_case(router_type, node_count: int, edge_count: int, iterations: int) -> dict:
    fixture_started = time.perf_counter_ns()
    evidence = build_evidence(node_count, edge_count)
    fixture_ms = (time.perf_counter_ns() - fixture_started) / 1_000_000
    fixture = evidence["synthetic-step"]
    constructor_samples, cold_samples = [], []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        router = router_type()
        constructor_samples.append((time.perf_counter_ns() - started) / 1_000_000)
        cold_samples.append(select_sample(router, evidence))
    started = time.perf_counter_ns()
    warmed_router = router_type()
    warm_constructor_ms = (time.perf_counter_ns() - started) / 1_000_000
    prime = select_sample(warmed_router, evidence)
    warm_samples = [select_sample(warmed_router, evidence) for _ in range(iterations)]
    cold, warm = summarize_samples(cold_samples), summarize_samples(warm_samples)
    return {
        "fixture": {
            "node_count": node_count, "edge_count": edge_count,
            "node_labels": sorted({label for node in fixture["nodes"] for label in node["labels"]}),
            "edge_types": sorted({edge["type"] for edge in fixture["edges"]}),
            "construction_ms": round(fixture_ms, 6),
            "distribution": "Synthetic mixed modern schema; endpoint IDs exist, but biological relationships are not evaluated.",
        },
        "fresh_router_first_selection": {
            "constructor_filesystem_load_ms": summary(constructor_samples),
            **cold,
        },
        "warm_router_repeated_selection": {
            "one_time_constructor_filesystem_load_ms": round(warm_constructor_ms, 6),
            "excluded_prime": {key: prime[key] for key in ("wall_ms", "timing_ms", "cache_hit", "guidance_bytes")},
            **warm,
        },
        "cold_and_warm_guidance_match": cold["guidance_sha256"] == warm["guidance_sha256"],
    }


def benchmark(iterations: int = 200) -> dict:
    if not 1 <= iterations <= 10000:
        raise ValueError("iterations must be between 1 and 10000")
    import_started = time.perf_counter_ns()
    from .answer_router import AnswerSkillRouter
    import_ms = (time.perf_counter_ns() - import_started) / 1_000_000
    started = time.perf_counter_ns()
    cases = {
        "typical_size_10_nodes_20_edges": benchmark_case(AnswerSkillRouter, 10, 20, iterations),
        "ceiling_size_2000_nodes_5000_edges": benchmark_case(AnswerSkillRouter, 2000, 5000, iterations),
    }
    return {
        "version": 1,
        "benchmark": "offline_answer_skill_router",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": sys.version.split()[0], "system": platform.system(), "machine": platform.machine()},
        "iterations_per_phase_per_fixture": iterations,
        "router_module_import_ms": round(import_ms, 6),
        "total_benchmark_ms": round((time.perf_counter_ns() - started) / 1_000_000, 6),
        "units": "milliseconds unless field name says bytes",
        "method": {
            "initialization": "Constructors read/verify the pinned local skill bundle; reported separately from selection.",
            "cold": "First select on each fresh router instance, with actual cache-hit flags reported.",
            "warm": "Repeated selects after one explicitly excluded priming selection on the same instance.",
            "filesystem": "OS filesystem caches are not flushed; fresh-instance routing is not a physical cold-disk benchmark.",
            "profile_compile": "The router's compile interval also includes signature/cache bookkeeping and profile construction; warm-cache time is not necessarily zero.",
            "p95": "Nearest rank over measured samples; median uses statistics.median.",
            "exclusions": "No network, inference, credentials, database query, or request context compaction. Fixture construction and module import are outside per-request selection timings.",
        },
        "cases": cases,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--output", type=Path, help="Optional new JSON report path; stdout always contains the aggregate report.")
    args = parser.parse_args(argv)
    if not 1 <= args.iterations <= 10000:
        parser.error("--iterations must be between 1 and 10000")
    if args.output and args.output.exists():
        parser.error("--output already exists; choose a new report path")
    report = benchmark(args.iterations)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
