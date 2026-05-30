"""Unit tests for the sequential KG-chain executor (data-flow combine).

These cover the pure-logic helpers (no Neo4j / vLLM needed) plus the dispatch
fallback to the legacy combine_chain path.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "skills", "query-planner", "scripts")
))

import qp_query_planner as qp  # noqa: E402


# A typical simple per-step query the cypher model emits for the QTL hop.
QTL_STEP_CYPHER = (
    "MATCH (s:snv)-[r:part_of_QTL_signal]->(g:gene)\n"
    "WITH collect(DISTINCT s)+collect(DISTINCT g) AS nodes, collect(DISTINCT r) AS edges\n"
    "RETURN nodes, edges;"
)

GWAS_STEP_CYPHER = (
    "MATCH (s:snv)-[r:part_of_GWAS_signal]->(d:disease) WHERE d.name = \"type 1 diabetes\"\n"
    "WITH collect(DISTINCT s)+collect(DISTINCT d) AS nodes, collect(DISTINCT r) AS edges\n"
    "RETURN nodes, edges;"
)


def _node(elem_id, label, **props):
    return {"__type__": "node", "id": elem_id, "element_id": elem_id,
            "labels": [label], "properties": props}


def _edge(elem_id, etype, start, end):
    return {"__type__": "relationship", "id": elem_id, "element_id": elem_id,
            "type": etype, "start_node_element_id": start, "end_node_element_id": end,
            "properties": {}}


def _kg_result(nodes, edges):
    return {"result": {"records": [{"nodes": nodes, "edges": edges}], "keys": ["nodes", "edges"]}}


# ---------------------------------------------------------------------------
# _find_node_var_for_label
# ---------------------------------------------------------------------------

def test_find_node_var_matches_snv():
    assert qp._find_node_var_for_label(QTL_STEP_CYPHER, ("snv", "sequence_variant")) == "s"


def test_find_node_var_matches_gene():
    assert qp._find_node_var_for_label(QTL_STEP_CYPHER, ("gene",)) == "g"


def test_find_node_var_no_match_returns_none():
    assert qp._find_node_var_for_label(QTL_STEP_CYPHER, ("donor",)) is None


def test_find_node_var_ignores_collect_and_rel():
    # collect(DISTINCT s) and [r:...] must not be mistaken for labeled node patterns
    assert qp._find_node_var_for_label("WITH collect(DISTINCT s) AS nodes", ("snv",)) is None


# ---------------------------------------------------------------------------
# _inject_ids_into_cypher
# ---------------------------------------------------------------------------

def test_inject_adds_where_when_absent():
    out = qp._inject_ids_into_cypher(QTL_STEP_CYPHER, "s", "id", ["rs1", "rs2"])
    assert 'WHERE s.id IN ["rs1", "rs2"]' in out
    # constraint is placed before the collect/RETURN
    assert out.index("WHERE s.id IN") < out.index("WITH collect")


def test_inject_ands_onto_existing_where():
    out = qp._inject_ids_into_cypher(GWAS_STEP_CYPHER, "s", "id", ["rs1"])
    assert "WHERE d.name" in out
    assert 'AND s.id IN ["rs1"]' in out
    assert out.count("WHERE") == 1  # did not add a second WHERE


def test_inject_empty_ids_noop():
    assert qp._inject_ids_into_cypher(QTL_STEP_CYPHER, "s", "id", []) == QTL_STEP_CYPHER


def test_inject_caps_ids():
    ids = [f"rs{i}" for i in range(qp._SEQ_KG_ID_CAP + 50)]
    out = qp._inject_ids_into_cypher(QTL_STEP_CYPHER, "s", "id", ids)
    assert out.count("rs") == qp._SEQ_KG_ID_CAP  # only the cap made it in


def test_inject_strips_quotes_from_ids():
    out = qp._inject_ids_into_cypher(QTL_STEP_CYPHER, "s", "id", ['rs"evil'])
    assert 'rsevil' in out and 'rs"evil' not in out


# ---------------------------------------------------------------------------
# _inject_prior_ids_into_kg_step (deterministic path)
# ---------------------------------------------------------------------------

def test_inject_prior_deterministic_snv():
    step = {"id": 2, "cypher": QTL_STEP_CYPHER, "natural_language": "QTL for snvs"}
    prior = {"snv_ids": ["rs180743", "rs8096138"], "gene_ids": [], "gene_names": [], "donor_ids": []}
    cypher, meta = qp._inject_prior_ids_into_kg_step(step, prior)
    assert meta["mode"] == "deterministic"
    assert meta["bucket"] == "snv_ids" and meta["var"] == "s" and meta["prop"] == "id"
    assert 'WHERE s.id IN ["rs180743", "rs8096138"]' in cypher


def test_inject_prior_prefers_ids_over_names():
    # both gene_ids and gene_names present → ID join wins
    step = {"id": 2, "cypher": "MATCH (g:gene)-[r:function_annotation]->(go:gene_ontology)\n"
                                "WITH collect(DISTINCT g) AS nodes, collect(DISTINCT r) AS edges RETURN nodes, edges;"}
    prior = {"snv_ids": [], "gene_ids": ["ENSG1"], "gene_names": ["TCF7"], "donor_ids": []}
    cypher, meta = qp._inject_prior_ids_into_kg_step(step, prior)
    assert meta["bucket"] == "gene_ids" and "g.id IN" in cypher


# ---------------------------------------------------------------------------
# _cap_rows_before_collect  (join-safe output bound)
# ---------------------------------------------------------------------------

def test_cap_injects_limit_after_where_before_collect():
    q = ("MATCH (s:snv)-[r:part_of_QTL_signal]->(g:gene)\n"
         "WHERE s.id IN [\"rs1\"]\n"
         "WITH collect(DISTINCT s)+collect(DISTINCT g) AS nodes, collect(DISTINCT r) AS edges RETURN nodes, edges;")
    out = qp._cap_rows_before_collect(q, 500)
    # LIMIT sits after the WHERE and before the collect — caps output, not join keys
    assert out.index("WHERE s.id IN") < out.index("LIMIT 500") < out.index("WITH collect")
    assert "LIMIT 500" in out


def test_cap_overwrites_existing_pre_collect_limit():
    # A generic LIMIT 50 injected at translation time must be raised to the tier,
    # else it truncates the join keys feeding a downstream step.
    q = ("MATCH (g:gene)-[r:part_of_QTL_signal]->(s:snv) WHERE s.id IN [\"rs1\"] "
         "WITH g, r, s LIMIT 50 WITH collect(DISTINCT g)+collect(DISTINCT s) AS nodes, "
         "collect(DISTINCT r) AS edges RETURN nodes, edges;")
    out = qp._cap_rows_before_collect(q, 5000)
    assert "LIMIT 5000" in out and "LIMIT 50 " not in out
    # the id filter is preserved
    assert 's.id IN ["rs1"]' in out


def test_cap_noop_without_collect():
    q = "MATCH (s:snv) RETURN s.id LIMIT 3;"
    assert qp._cap_rows_before_collect(q, 500) == q


def test_cap_noop_when_limit_zero():
    q = "MATCH (s:snv) WITH collect(DISTINCT s) AS nodes RETURN nodes;"
    assert qp._cap_rows_before_collect(q, 0) == q


# ---------------------------------------------------------------------------
# _referenced_parent_ids  (terminal vs feeds-downstream)
# ---------------------------------------------------------------------------

def test_referenced_parents_linear_implicit():
    # id-1 implicit predecessors: 1 and 2 feed downstream, 3 (last) is terminal
    steps = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert qp._referenced_parent_ids(steps) == {1, 2}


def test_referenced_parents_explicit_depends_on():
    steps = [{"id": 1}, {"id": 2, "depends_on": 1}, {"id": 3, "depends_on": 1}]
    # both 2 and 3 depend on 1 -> only 1 feeds downstream; 2 and 3 are terminal
    assert qp._referenced_parent_ids(steps) == {1}


# ---------------------------------------------------------------------------
# _count_collected
# ---------------------------------------------------------------------------

def test_count_collected_max_of_nodes_edges():
    res = {"records": [{"nodes": [1, 2, 3], "edges": [1]}]}
    assert qp._count_collected(res) == 3


def test_count_collected_error_returns_none():
    assert qp._count_collected({"error": "x"}) is None


# ---------------------------------------------------------------------------
# _merge_step_results  (union + dedup)
# ---------------------------------------------------------------------------

def test_merge_unions_and_dedups():
    s1 = _kg_result([_node("n1", "snv", id="rs1")], [_edge("e1", "GWAS", "n1", "d1")])
    s2 = _kg_result(
        [_node("n1", "snv", id="rs1"), _node("g1", "gene", id="ENSG1")],  # n1 duplicate
        [_edge("e2", "QTL", "n1", "g1")],
    )
    merged = qp._merge_step_results([s1, s2])
    rec = merged["records"][0]
    assert len(rec["nodes"]) == 2  # n1 deduped, g1 added
    assert len(rec["edges"]) == 2
    assert {n["id"] for n in rec["nodes"]} == {"n1", "g1"}


def test_merge_skips_errors():
    err = {"result": {"error": "boom"}}
    good = _kg_result([_node("n1", "gene", id="ENSG1")], [])
    merged = qp._merge_step_results([err, good])
    assert len(merged["records"][0]["nodes"]) == 1


# ---------------------------------------------------------------------------
# _results_have_data
# ---------------------------------------------------------------------------

def test_results_have_data_true():
    assert qp._results_have_data([_kg_result([_node("n1", "gene", id="x")], [])]) is True


def test_results_have_data_false_on_empty():
    assert qp._results_have_data([_kg_result([], [])]) is False


def test_results_have_data_false_on_error():
    assert qp._results_have_data([{"result": {"error": "x"}}]) is False


# ---------------------------------------------------------------------------
# Dispatch fallback to combine_chain when sequential returns empty
# ---------------------------------------------------------------------------

def test_dispatch_falls_back_to_combine_on_empty(monkeypatch):
    plan = {"plan_type": "chain", "steps": [
        {"id": 1, "cypher": GWAS_STEP_CYPHER},
        {"id": 2, "cypher": QTL_STEP_CYPHER},
    ]}
    events = []
    monkeypatch.setattr(qp, "_SEQUENTIAL_KG_CHAIN", True)
    monkeypatch.setattr(qp, "_SEQ_KG_CHAIN_FALLBACK", True)
    monkeypatch.setattr(qp, "_execute_sequential_kg_chain", lambda p: [_kg_result([], [])])
    sentinel = [{"query": "combined", "result": {"records": [{"nodes": [1], "edges": []}]}}]
    monkeypatch.setattr(qp, "_execute_pure_kg_chain", lambda p: sentinel)
    monkeypatch.setattr(qp, "emit", lambda *a, **k: events.append(a))

    out = qp.execute_plan(plan)
    assert out is sentinel
    assert any("chain_seq_fallback_to_combine" in (a[0],) for a in events)


def test_dispatch_uses_sequential_when_data(monkeypatch):
    plan = {"plan_type": "chain", "steps": [{"id": 1, "cypher": GWAS_STEP_CYPHER}]}
    seq_out = [_kg_result([_node("n1", "gene", id="x")], [])]
    monkeypatch.setattr(qp, "_SEQUENTIAL_KG_CHAIN", True)
    monkeypatch.setattr(qp, "_execute_sequential_kg_chain", lambda p: seq_out)
    monkeypatch.setattr(qp, "_execute_pure_kg_chain",
                        lambda p: pytest.fail("combine_chain should not run when sequential has data"))
    monkeypatch.setattr(qp, "emit", lambda *a, **k: None)
    assert qp.execute_plan(plan) is seq_out


def test_dispatch_uses_combine_when_flag_off(monkeypatch):
    plan = {"plan_type": "chain", "steps": [{"id": 1, "cypher": GWAS_STEP_CYPHER}]}
    sentinel = [_kg_result([_node("n", "snv", id="x")], [])]  # has data -> no parallel fallback
    monkeypatch.setattr(qp, "_SEQUENTIAL_KG_CHAIN", False)
    monkeypatch.setattr(qp, "_execute_sequential_kg_chain",
                        lambda p: pytest.fail("sequential should not run when flag off"))
    monkeypatch.setattr(qp, "_execute_pure_kg_chain", lambda p: sentinel)
    monkeypatch.setattr(qp, "emit", lambda *a, **k: None)
    assert qp.execute_plan(plan) is sentinel


# ---------------------------------------------------------------------------
# Chain -> parallel reliability fallback
# ---------------------------------------------------------------------------

def test_chain_falls_back_to_parallel_when_empty(monkeypatch):
    plan = {"plan_type": "chain", "steps": [
        {"id": 1, "cypher": GWAS_STEP_CYPHER, "depends_on": None},
        {"id": 2, "cypher": QTL_STEP_CYPHER, "depends_on": 1},
    ]}
    events = []
    par_out = [_kg_result([_node("g1", "gene", id="ENSG1")], [])]
    monkeypatch.setattr(qp, "_SEQUENTIAL_KG_CHAIN", True)
    monkeypatch.setattr(qp, "_SEQ_KG_CHAIN_FALLBACK", False)
    monkeypatch.setattr(qp, "_CHAIN_FALLBACK_TO_PARALLEL", True)
    monkeypatch.setattr(qp, "_execute_sequential_kg_chain", lambda p: [_kg_result([], [])])
    monkeypatch.setattr(qp, "_execute_parallel_with_deps", lambda p, *a: par_out)
    monkeypatch.setattr(qp, "emit", lambda *a, **k: events.append(a[0]))

    out = qp.execute_plan(plan)
    assert out is par_out
    assert "chain_fallback_to_parallel" in events
    # plan mutated so downstream treats it as parallel with independent steps
    assert plan["plan_type"] == "parallel"
    assert all(s["depends_on"] is None for s in plan["steps"])


def test_cross_source_chain_falls_back_to_parallel(monkeypatch):
    plan = {"plan_type": "chain", "steps": [
        {"id": 1, "cypher": GWAS_STEP_CYPHER},
        {"id": 2, "source": "genomic", "natural_language": "x", "depends_on": 1},
    ]}
    par_out = [_kg_result([_node("g1", "gene", id="ENSG1")], [])]
    monkeypatch.setattr(qp, "_CHAIN_FALLBACK_TO_PARALLEL", True)
    monkeypatch.setattr(qp, "_execute_cross_source_chain", lambda p, *a: [_kg_result([], [])])
    monkeypatch.setattr(qp, "_execute_parallel_with_deps", lambda p, *a: par_out)
    monkeypatch.setattr(qp, "emit", lambda *a, **k: None)
    assert qp.execute_plan(plan) is par_out
    assert plan["plan_type"] == "parallel"


def test_chain_no_fallback_when_data(monkeypatch):
    plan = {"plan_type": "chain", "steps": [{"id": 1, "cypher": GWAS_STEP_CYPHER}]}
    seq_out = [_kg_result([_node("n", "snv", id="x")], [])]
    monkeypatch.setattr(qp, "_SEQUENTIAL_KG_CHAIN", True)
    monkeypatch.setattr(qp, "_CHAIN_FALLBACK_TO_PARALLEL", True)
    monkeypatch.setattr(qp, "_execute_sequential_kg_chain", lambda p: seq_out)
    monkeypatch.setattr(qp, "_execute_parallel_with_deps",
                        lambda p, *a: pytest.fail("parallel fallback must not run when chain has data"))
    monkeypatch.setattr(qp, "emit", lambda *a, **k: None)
    assert qp.execute_plan(plan) is seq_out
    assert plan["plan_type"] == "chain"  # not mutated


def test_chain_no_fallback_when_flag_off(monkeypatch):
    plan = {"plan_type": "chain", "steps": [{"id": 1, "cypher": GWAS_STEP_CYPHER}]}
    empty = [_kg_result([], [])]
    monkeypatch.setattr(qp, "_SEQUENTIAL_KG_CHAIN", True)
    monkeypatch.setattr(qp, "_SEQ_KG_CHAIN_FALLBACK", False)
    monkeypatch.setattr(qp, "_CHAIN_FALLBACK_TO_PARALLEL", False)
    monkeypatch.setattr(qp, "_execute_sequential_kg_chain", lambda p: empty)
    monkeypatch.setattr(qp, "_execute_parallel_with_deps",
                        lambda p, *a: pytest.fail("parallel fallback disabled"))
    monkeypatch.setattr(qp, "emit", lambda *a, **k: None)
    assert qp.execute_plan(plan) is empty
