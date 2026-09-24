import asyncio
from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest
from neo4j.graph import Graph, Node

from pankagent_vnext.annotation_selection import allocate_independent_budgets, apply_default
from pankagent_vnext.answer_blocks import catalogue
from pankagent_vnext.app import Runtime
from pankagent_vnext.bounded_paths import (
    MAX_PATH_RECORDS,
    BoundedPathError,
    compile_hla_path_plan,
    compile_query,
    derive_chain_facts,
    extract_path_records,
    is_hla_path_request,
    plan_issue,
)
from pankagent_vnext.graph import GraphAdapter, _public_edge_fingerprint, validate_cypher
from pankagent_vnext.planning_compile import compile_property_owners
from pankagent_vnext.planning_requirements import compile_requested_scope, requirements_issue
from pankagent_vnext.planning_scope import scope_issue
from pankagent_vnext.preplanning_grounding import ground_question
from pankagent_vnext.query_templates import compile_query as compile_one_hop_query
from pankagent_vnext.release_schema import REGISTRY
from pankagent_vnext.revision_context import parent_context

from test_graph import FakeAdapter, FakeSession, FakeTransaction
from test_planning_compiler_gateway import gateway_for
from test_preplanning_grounding import FakeGraph


SCREENSHOT = (
    "What pathways (KEGG/Reactome) is HLA-DRA annotated to, and what genes "
    "physically or genetically interact with HLA-DRA, in the context of antigen "
    "presentation and type 1 diabetes?"
)
STORED = "What pathways and interaction partners connect HLA-DRA to antigen presentation in T1D?"
STORED_NBSP = "What pathways and interaction partners connect\u00a0HLA-DRA\u00a0to antigen presentation in T1D?"
HLA_ID = "ENSG00000204287"


def hla_grounding(question, *, antigen_pathway=True):
    graph = FakeGraph()
    graph.rows["Gene"].append({"id": HLA_ID, "name": "HLA-DRA", "labels": ["Gene"]})
    if antigen_pathway:
        graph.rows["kegg"].append({"id": "hsa-antigen", "name": "antigen presentation",
                                   "labels": ["kegg"]})
    return asyncio.run(ground_question(graph, question))


def scoped_hla_plan(question):
    grounding = hla_grounding(question)
    plan = compile_hla_path_plan(question, grounding)
    assert plan is not None
    plan, issue = compile_property_owners(plan, grounding, question=question)
    assert issue is None
    plan, issue = compile_requested_scope(question, grounding, plan)
    assert issue is None
    assert scope_issue(question, grounding, plan) is None
    assert requirements_issue(question, grounding, plan) is None
    return plan, grounding


def prepared(step, question):
    value = deepcopy(step)
    value["graph_version"] = REGISTRY["release"]
    constraint = value["constraints"][0]
    value["resolved_entities"] = [{
        "constraint_index": 0, "requested": deepcopy(constraint), "state": "resolved",
        "graph_version": REGISTRY["release"], "entity_type": "Gene", "labels": ["Gene"],
        "id": HLA_ID, "name": "HLA-DRA",
    }]
    digest = hashlib.sha256(question.encode()).hexdigest()
    value["request_filter_bindings"] = [{
        "constraint_index": index, "canonical_binding": deepcopy(constraint),
        "source": "immutable_user_request", "request_sha256": digest,
        "graph_release": REGISTRY["release"],
        "authorization_kind": "verified_test_request_filter",
    } for index, constraint in enumerate(value["constraints"])]
    return value


def four_node_step():
    return {
        "id": "four", "question": "Follow a fixed four-node interaction path from HLA-DRA.",
        "relation_types": ["PHYSICAL_INTERACTION", "FUNCTION_ANNOTATION"],
        "depends_on": [], "complete": True, "evidence_combination": "cooccurrence",
        "constraints": [{"property": "id", "operator": "=", "value": HLA_ID,
                         "entity_type": "Gene", "owner_role": "focus"}],
        "semantic_request": {"source": "user_request", "question": STORED,
                             "revision_instruction": ""},
        "path_spec": {"version": "bounded-path-v1", "nodes": [
            {"role": "focus", "entity_types": ["Gene"]},
            {"role": "partner", "entity_types": ["Gene"]},
            {"role": "partner_two", "entity_types": ["Gene"]},
            {"role": "process", "entity_types": ["kegg", "reactome"]},
        ], "edges": [
            {"role": "interaction", "from": "focus", "to": "partner",
             "types_any": ["PHYSICAL_INTERACTION"], "direction": "either"},
            {"role": "interaction_two", "from": "partner", "to": "partner_two",
             "types_any": ["PHYSICAL_INTERACTION"], "direction": "either"},
            {"role": "annotation", "from": "partner_two", "to": "process",
             "types_any": ["FUNCTION_ANNOTATION"], "direction": "out"},
        ]},
    }


@pytest.mark.parametrize("question,grammar", [
    (SCREENSHOT, "screenshot"), (STORED, "stored"), (STORED_NBSP, "stored"),
])
def test_reviewed_hla_grammars_compile_the_same_three_joined_checks(question, grammar):
    plan, _grounding = scoped_hla_plan(question)
    assert [step["id"] for step in plan["steps"]] == [
        "direct_annotations", "interaction_partners", "partner_annotations"]
    assert plan["planning_route"]["grammar"] == grammar
    assert all(step["evidence_combination"] == "cooccurrence" for step in plan["steps"])
    assert all(step.get("path_spec") and not step.get("depends_on") for step in plan["steps"])
    assert all("T1D_DEG_IN" not in step["relation_types"] for step in plan["steps"])
    assert {"MONDO_0005147", "hsa-antigen"} <= set(
        plan["planning_route"]["context_only_entity_ids"])


def test_contextless_pathway_and_partner_wording_is_not_the_joined_hla_route():
    question = (
        "What pathways (KEGG/Reactome) is HLA-DRA annotated to, and what genes "
        "physically or genetically interact with HLA-DRA?"
    )
    assert is_hla_path_request(question) is False
    assert compile_hla_path_plan(question, hla_grounding(question)) is None


@pytest.mark.parametrize("question", [SCREENSHOT, STORED, STORED_NBSP])
def test_gateway_reviews_local_hla_draft_with_model(question):
    grounding = hla_grounding(question)
    async def check():
        gateway, calls = gateway_for(lambda _count: deepcopy(compile_hla_path_plan(question, grounding)))
        plan = await gateway.plan(question, [], grounding=grounding)
        assert len(calls) == 1
        assert len(plan["steps"]) == 3
        assert (plan.get("planning_route") or {}).get("kind") == "claude-led-planning-v1"
    asyncio.run(check())


def test_path_defaults_remain_complete_and_budget_favors_joined_path():
    plan, _ = scoped_hla_plan(SCREENSHOT)
    assert all(apply_default(step, SCREENSHOT) == step for step in plan["steps"])
    settings = SimpleNamespace(max_bytes=2_000_000, max_nodes=2000,
                               max_edges=5000, max_rows=1000)
    allocate_independent_budgets(plan, settings)
    direct, partner, joined = [step["retrieval_budget"] for step in plan["steps"]]
    assert direct["max_bytes"] < partner["max_bytes"] < joined["max_bytes"]
    assert [direct, partner, joined] == [
        {"max_bytes": 95_238, "max_nodes": 95, "max_edges": 238, "max_rows": 47},
        {"max_bytes": 571_428, "max_nodes": 571, "max_edges": 1428, "max_rows": 285},
        {"max_bytes": 1_333_333, "max_nodes": 1333,
         "max_edges": 3333, "max_rows": 666},
    ]
    for key, total in (("max_bytes", 2_000_000), ("max_nodes", 2000),
                       ("max_edges", 5000), ("max_rows", 1000)):
        assert sum(step["retrieval_budget"][key] for step in plan["steps"]) <= total


def test_general_four_node_path_keeps_depth_squared_branch_fairness():
    path = four_node_step()
    branch = deepcopy(path)
    branch["id"] = "branch"
    branch["path_spec"]["nodes"] = branch["path_spec"]["nodes"][:2]
    branch["path_spec"]["edges"] = branch["path_spec"]["edges"][:1]
    branch["relation_types"] = ["PHYSICAL_INTERACTION"]
    plan = {"steps": [path, branch], "planning_route": {"kind": "general"}}
    allocate_independent_budgets(
        plan, SimpleNamespace(max_bytes=1000, max_nodes=100,
                              max_edges=100, max_rows=100))
    assert [step["retrieval_budget"]["max_bytes"] for step in plan["steps"]] == [
        900, 100]


def test_hla_budget_override_is_order_independent_and_exact_topology_only():
    plan, _ = scoped_hla_plan(SCREENSHOT)
    plan["steps"].reverse()
    settings = SimpleNamespace(max_bytes=2_000_000, max_nodes=2000,
                               max_edges=5000, max_rows=1000)
    allocate_independent_budgets(plan, settings)
    by_id = {step["id"]: step["retrieval_budget"]["max_bytes"]
             for step in plan["steps"]}
    assert by_id == {
        "direct_annotations": 95_238,
        "interaction_partners": 571_428,
        "partner_annotations": 1_333_333,
    }

    changed = deepcopy(plan)
    interaction = next(step for step in changed["steps"]
                       if step["id"] == "interaction_partners")
    interaction["path_spec"]["edges"][0]["direction"] = "out"
    allocate_independent_budgets(changed, settings)
    fallback = {step["id"]: step["retrieval_budget"]["max_bytes"]
                for step in changed["steps"]}
    assert fallback == {
        "direct_annotations": 333_333,
        "interaction_partners": 333_333,
        "partner_annotations": 1_333_333,
    }


def test_owner_role_resolves_shared_multi_type_edge_property_without_guessing_type():
    raw = four_node_step()
    raw["path_spec"]["edges"][0]["types_any"] = [
        "PHYSICAL_INTERACTION", "GENETIC_INTERACTION"]
    raw["relation_types"].append("GENETIC_INTERACTION")
    raw["constraints"].append({"property": "data_source", "operator": "=",
                               "value": "BioGRID", "entity_type": None,
                               "owner_role": "interaction"})
    grounding = {"status": "ready", "identity": {"graph_release": REGISTRY["release"]},
                 "mentions": []}
    result, issue = compile_property_owners({"steps": [raw]}, grounding,
                                            question=raw["question"])
    assert issue is None
    compiled = result["steps"][0]["constraints"][1]
    assert compiled["owner_kind"] == "relationship"
    assert compiled["owner_role"] == "interaction"
    assert "relationship_type" not in compiled
    assert plan_issue(result["steps"][0]) is None


def test_two_three_and_four_node_paths_compile_and_validate_exactly():
    hla, _ = scoped_hla_plan(STORED)
    steps = [hla["steps"][0], hla["steps"][2], four_node_step()]
    for step in steps:
        value = prepared(step, STORED)
        template = compile_query(value)
        assert validate_cypher(template["cypher"], value, template["parameters"]) == []
        assert template["cypher"].count("LIMIT $path_record_overfetch") == 1
        assert template["parameters"]["path_record_overfetch"] == MAX_PATH_RECORDS + 1
        assert "collect({nodes:" in template["cypher"]
        assert "RETURN collect" in template["cypher"]
        assert template["cypher"].index("ORDER BY") < template["cypher"].index(
            "LIMIT $path_record_overfetch")
        assert "properties(r" not in template["cypher"]
        assert template["cypher"].count(" <> ") >= (
            len(value["path_spec"]["nodes"]) * (len(value["path_spec"]["nodes"]) - 1) // 2)


def test_explicit_node_owner_narrows_multi_type_role_and_uses_its_properties():
    plan, _ = scoped_hla_plan(STORED)
    step = deepcopy(plan["steps"][0])
    step["constraints"].append({
        "property": "description", "operator": "=", "value": "Antigen presentation",
        "entity_type": "kegg", "owner_role": "process",
    })
    value = prepared(step, STORED)
    assert plan_issue(value) is None
    template = compile_query(value)
    assert "n1:`kegg`" in template["cypher"]
    assert "n1.`description`" in template["cypher"]
    assert validate_cypher(template["cypher"], value, template["parameters"]) == []

    conflicting = deepcopy(step)
    conflicting["constraints"].append({
        "property": "name", "operator": "=", "value": "MHC pathway",
        "entity_type": "reactome", "owner_role": "process",
    })
    assert plan_issue(conflicting) == "path_constraint_owner_mismatch"


def test_donor_inclusive_four_node_path_compiles_without_property_map_projection():
    question = "Show disease, donor, sample, and tissue paths for T1D."
    constraint = {"property": "id", "operator": "=", "value": "MONDO_0005147",
                  "entity_type": "disease", "owner_role": "condition"}
    step = {
        "id": "donor_path", "question": question,
        "relation_types": ["HAS_DONOR", "HAS_SAMPLE"], "depends_on": [],
        "complete": True, "evidence_combination": "cooccurrence",
        "constraints": [constraint],
        "semantic_request": {"source": "user_request", "question": question,
                             "revision_instruction": ""},
        "path_spec": {"version": "bounded-path-v1", "nodes": [
            {"role": "condition", "entity_types": ["disease"]},
            {"role": "donor", "entity_types": ["donor"]},
            {"role": "sample", "entity_types": ["Sample_node"]},
            {"role": "tissue", "entity_types": ["anatomical_structure"]},
        ], "edges": [
            {"role": "cohort", "from": "condition", "to": "donor",
             "types_any": ["HAS_DONOR"], "direction": "out"},
            {"role": "donor_sample", "from": "donor", "to": "sample",
             "types_any": ["HAS_SAMPLE"], "direction": "out"},
            {"role": "tissue_sample", "from": "sample", "to": "tissue",
             "types_any": ["HAS_SAMPLE"], "direction": "in"},
        ]},
        "graph_version": REGISTRY["release"],
        "resolved_entities": [{
            "constraint_index": 0, "requested": deepcopy(constraint),
            "state": "resolved", "graph_version": REGISTRY["release"],
            "entity_type": "disease", "labels": ["disease"],
            "id": "MONDO_0005147", "name": "type 1 diabetes mellitus",
        }],
    }
    digest = hashlib.sha256(question.encode()).hexdigest()
    step["request_filter_bindings"] = [{
        "constraint_index": 0, "canonical_binding": deepcopy(constraint),
        "source": "immutable_user_request", "request_sha256": digest,
        "graph_release": REGISTRY["release"],
        "authorization_kind": "verified_test_request_filter",
    }]
    template = compile_query(step)
    assert "elementId(r" in template["cypher"]
    assert "properties(r" not in template["cypher"]
    assert validate_cypher(template["cypher"], step, template["parameters"]) == []


@pytest.mark.parametrize("projection", [
    "WITH head([d]) AS y RETURN properties(y) AS leaked",
    "WITH head(collect(d)) AS y RETURN y{.*} AS leaked",
    "WITH coalesce(d,d) AS y RETURN properties(y) AS leaked",
    "WITH collect(d) AS y RETURN properties(head(y)) AS leaked",
    "WITH collect(d) AS y RETURN head(y){.*} AS leaked",
])
def test_generated_queries_cannot_project_maps_from_concealed_donor_aliases(projection):
    step = {"id": "privacy", "question": "Return donors.", "relation_types": [],
            "depends_on": [], "constraints": [], "complete": True,
            "graph_version": REGISTRY["release"]}
    errors = validate_cypher("MATCH (d:`donor`) " + projection, step, {})
    assert "unrequested_donor_property_map_projection" in errors


@pytest.mark.parametrize("projection", [
    "properties(g) AS y", "g{.*} AS y",
])
def test_any_property_map_projection_is_rejected_when_donor_is_in_scope(projection):
    step = {"id": "privacy", "question": "Return donors and genes.",
            "relation_types": [], "depends_on": [], "constraints": [],
            "complete": True, "graph_version": REGISTRY["release"]}
    query = "MATCH (d:`donor`), (g:`Gene`) RETURN " + projection
    assert "unrequested_donor_property_map_projection" in validate_cypher(query, step, {})


@pytest.mark.parametrize("relation,source,target", [
    ("HAS_DONOR", "disease", "donor"),
    ("HAS_SAMPLE", "donor", "Sample_node"),
])
def test_standard_local_node_edge_aggregate_transport_remains_valid_for_donors(
        relation, source, target):
    constraint = {"property": "id", "operator": "=", "value": "source-id",
                  "entity_type": source}
    step = {"id": "aggregate", "question": "Return linked records.",
            "relation_types": [relation], "depends_on": [],
            "constraints": [constraint], "complete": True,
            "graph_version": REGISTRY["release"]}
    query = (f"MATCH (x:`{source}`)-[r:`{relation}`]->(d:`{target}`) "
             "WHERE x.`id` = $source_id "
             "RETURN collect(DISTINCT x) + collect(DISTINCT d) AS nodes, "
             "collect(DISTINCT r) AS edges")
    assert validate_cypher(query, step, {"source_id": "source-id"}) == []


def test_topology_gate_rejects_focus_annotation_substitution_before_generic_validation():
    plan, _ = scoped_hla_plan(STORED)
    step = prepared(plan["steps"][2], STORED)
    template = compile_query(step)
    changed = template["cypher"].replace(
        "MATCH (n1:`Gene`)-[r1:`FUNCTION_ANNOTATION`]->(n2)",
        "MATCH (n0:`Gene`)-[r1:`FUNCTION_ANNOTATION`]->(n2)")
    assert validate_cypher(changed, step, template["parameters"]) == [
        "bounded_path_query_not_deterministic"]


def test_topology_gate_rejects_extra_match_before_ordinary_validation():
    plan, _ = scoped_hla_plan(STORED)
    step = prepared(plan["steps"][2], STORED)
    template = compile_query(step)
    changed = template["cypher"].replace(
        "\nRETURN collect", "\nMATCH (extra:`Gene`)\nRETURN collect")
    assert validate_cypher(changed, step, template["parameters"]) == [
        "bounded_path_query_not_deterministic"]


def test_plan_contract_rejects_noncooccurrence_depth_five_and_dependencies():
    step = four_node_step()
    step["evidence_combination"] = "independent"
    assert plan_issue(step) == "bounded_path_requires_cooccurrence"
    step = four_node_step()
    step["depends_on"] = ["earlier"]
    assert plan_issue(step) == "bounded_path_dependencies_unsupported"
    step = four_node_step()
    step["path_spec"]["nodes"].append({"role": "fifth", "entity_types": ["Gene"]})
    assert plan_issue(step) == "path_node_depth_out_of_range"
    step = four_node_step()
    step["relation_types"].append("PHYSICAL_INTERACTION")
    assert plan_issue(step) == "path_relation_contract_mismatch"


def test_plan_contract_rejects_wrong_direction_disconnection_unanchored_extra_and_variable_length():
    step = four_node_step()
    step["path_spec"]["edges"][-1]["direction"] = "either"
    assert plan_issue(step) == "undirected_noninteraction_path"

    step = four_node_step()
    step["path_spec"]["edges"][1]["from"] = "focus"
    assert plan_issue(step) == "path_edges_not_in_node_order"

    step = four_node_step()
    step["constraints"] = []
    assert plan_issue(step) == "missing_path_anchor_identity"

    step = four_node_step()
    step["path_spec"]["edges"].append(deepcopy(step["path_spec"]["edges"][-1]))
    assert plan_issue(step) == "path_edge_count_mismatch"

    step = four_node_step()
    step["path_spec"]["edges"][0]["min_hops"] = 1
    assert plan_issue(step) == "malformed_path_edge"

    step = four_node_step()
    step["path_spec"]["nodes"][1]["entity_types"] = ["disease"]
    assert plan_issue(step) == "path_schema_endpoint_mismatch"


def test_mixed_coloc_and_signal_membership_path_fails_closed_like_coloc_guard():
    step = {
        "id": "mixed_coloc", "question": "Join coloc and GWAS signal membership.",
        "relation_types": ["SIGNAL_COLOC_WITH", "PART_OF_GWAS_SIGNAL"],
        "depends_on": [], "complete": True,
        "evidence_combination": "cooccurrence",
        "constraints": [{"property": "id", "operator": "=", "value": HLA_ID,
                         "entity_type": "Gene", "owner_role": "focus"}],
        "path_spec": {"version": "bounded-path-v1", "nodes": [
            {"role": "focus", "entity_types": ["Gene"]},
            {"role": "condition", "entity_types": ["disease"]},
            {"role": "variant", "entity_types": ["variants"]},
        ], "edges": [
            {"role": "coloc", "from": "focus", "to": "condition",
             "types_any": ["SIGNAL_COLOC_WITH"], "direction": "out"},
            {"role": "membership", "from": "condition", "to": "variant",
             "types_any": ["PART_OF_GWAS_SIGNAL"], "direction": "in"},
        ]},
    }
    assert plan_issue(step) == "coloc_requires_independent_evidence_checks"
    with pytest.raises(BoundedPathError, match="coloc_requires_independent_evidence_checks"):
        compile_query({**step, "graph_version": REGISTRY["release"]})


def _path_row(*, partner="P1", process="K1", interaction="PHYSICAL_INTERACTION",
              interaction_fingerprint="i1", annotation_fingerprint="a1"):
    return {"nodes": [{"node_id": HLA_ID}, {"node_id": partner}, {"node_id": process}],
            "edges": [
                {"edge": [HLA_ID, interaction, partner],
                 "fingerprint": interaction_fingerprint},
                {"edge": [partner, "FUNCTION_ANNOTATION", process],
                 "fingerprint": annotation_fingerprint},
            ]}


def _path_nodes():
    return [
        {"id": HLA_ID, "labels": ["Gene"], "properties": {"name": "HLA-DRA"}},
        {"id": "P1", "labels": ["Gene"], "properties": {"name": "PARTNER1"}},
        {"id": "P2", "labels": ["Gene"], "properties": {"name": "PARTNER2"}},
        {"id": "K1", "labels": ["kegg"], "properties": {"name": "Antigen processing"}},
        {"id": "R1", "labels": ["reactome"], "properties": {"name": "MHC presentation"}},
    ]


def test_extracted_paths_preserve_roles_parallel_edges_and_canonical_order():
    plan, _ = scoped_hla_plan(STORED)
    step = plan["steps"][2]
    rows = [{"path_records": [
        _path_row(partner="P2", process="R1", interaction="GENETIC_INTERACTION",
                  interaction_fingerprint="z"),
        _path_row(interaction_fingerprint="b"),
        _path_row(interaction_fingerprint="a"),
    ]}]
    records = extract_path_records(step, rows, _path_nodes())
    assert [record["edges"][0]["fingerprint"] for record in records] == ["a", "b", "z"]
    assert [node["role"] for node in records[0]["nodes"]] == ["focus", "partner", "process"]
    assert [edge["role"] for edge in records[0]["edges"]] == ["interaction", "annotation"]


def test_extraction_rejects_wrong_role_label_cycle_and_extra_projection():
    plan, _ = scoped_hla_plan(STORED)
    step = plan["steps"][2]
    nodes = _path_nodes()
    wrong = deepcopy(nodes)
    wrong[1] = {"id": "P1", "labels": ["kegg"], "properties": {"name": "wrong"}}
    with pytest.raises(BoundedPathError, match="invalid_path_result_node_type"):
        extract_path_records(step, [{"path_records": [_path_row()]}], wrong)
    cyclic = _path_row(partner=HLA_ID)
    cyclic["edges"][0]["edge"] = [HLA_ID, "PHYSICAL_INTERACTION", HLA_ID]
    cyclic["edges"][1]["edge"] = [HLA_ID, "FUNCTION_ANNOTATION", "K1"]
    with pytest.raises(BoundedPathError, match="cyclic_path_result"):
        extract_path_records(step, [{"path_records": [cyclic]}], nodes)
    extra = _path_row()
    extra["nodes"].append({"node_id": "P2"})
    with pytest.raises(BoundedPathError, match="invalid_path_result_shape"):
        extract_path_records(step, [{"path_records": [extra]}], nodes)


def test_retrieve_emits_public_fingerprints_for_parallel_relationships():
    async def check():
        graph = Graph()
        focus = Node(graph, "n0", 0, ["Gene"], {"id": HLA_ID})
        partner = Node(graph, "n1", 1, ["Gene"], {"id": "P1"})
        relation_type = graph.relationship_type("PHYSICAL_INTERACTION")
        first = relation_type(graph, "internal-one", 10, {"biogrid_interaction_id": "B1"})
        second = relation_type(graph, "internal-two", 11, {"biogrid_interaction_id": "B2"})
        for edge in (first, second):
            edge._start_node, edge._end_node = focus, partner
        tx = FakeTransaction([{"paths": [first, second]}])
        adapter = object.__new__(GraphAdapter)
        adapter.settings = SimpleNamespace(graph_timeout=1, max_nodes=10, max_edges=10,
                                           max_rows=1000, max_bytes=100000)
        adapter._session = lambda: FakeSession(tx)
        result = await adapter._retrieve("RETURN paths", {})
        wrappers = result["rows"][0]["paths"]
        assert wrappers[0]["fingerprint"] != wrappers[1]["fingerprint"]
        assert len(result["edges"]) == 2
        assert all("internal" not in wrapper["fingerprint"] for wrapper in wrappers)
        for edge, wrapper in zip(result["edges"], wrappers):
            assert wrapper["fingerprint"] == _public_edge_fingerprint(edge)
    asyncio.run(check())


def test_single_aggregate_row_keeps_1391_paths_below_row_budget():
    async def check():
        row = {"path_records": [{"sequence": index} for index in range(1391)]}
        tx = FakeTransaction([row])
        adapter = object.__new__(GraphAdapter)
        adapter.settings = SimpleNamespace(graph_timeout=1, max_nodes=2000, max_edges=5000,
                                           max_rows=1000, max_bytes=1_333_333)
        adapter._session = lambda: FakeSession(tx)
        result = await adapter._retrieve(
            "RETURN path_records", {}, {"bounded_path_records": True})
        assert result["status"] == "complete"
        assert len(result["rows"]) == 1
        assert len(result["rows"][0]["path_records"]) == 1391
    asyncio.run(check())


def test_truncated_path_aggregate_retains_only_an_atomic_verified_prefix():
    step = scoped_hla_plan(SCREENSHOT)[0]["steps"][1]

    async def check():
        graph = Graph()
        focus = Node(graph, "n0", 0, ["Gene"], {"id": HLA_ID, "name": "HLA-DRA"})
        first_partner = Node(graph, "n1", 1, ["Gene"], {"id": "P1", "name": "P1"})
        second_partner = Node(graph, "n2", 2, ["Gene"], {
            "id": "P2", "name": "P2", "oversized": "x" * 20_000})
        relation_type = graph.relationship_type("PHYSICAL_INTERACTION")
        first = relation_type(graph, "r1", 10, {"source": "first"})
        second = relation_type(graph, "r2", 11, {"source": "second"})
        first._start_node, first._end_node = focus, first_partner
        second._start_node, second._end_node = focus, second_partner
        tx = FakeTransaction([{"path_records": [
            {"nodes": [focus, first_partner], "edges": [first]},
            {"nodes": [focus, second_partner], "edges": [second]},
        ]}])
        adapter = object.__new__(GraphAdapter)
        adapter.settings = SimpleNamespace(
            graph_timeout=1, max_nodes=10, max_edges=10,
            max_rows=1000, max_bytes=4_000)
        adapter._session = lambda: FakeSession(tx)
        result = await adapter._retrieve(
            "RETURN path_records", {}, {"bounded_path_records": True})
        assert result["status"] == "partial"
        assert result["truncated"] is True
        assert result["retrieval_execution"]["cursor_exhausted"] is False
        assert tx.rolled_back is True
        assert result["materialized_bytes"] <= 4_000
        assert {node["id"] for node in result["nodes"]} == {HLA_ID, "P1"}
        assert len(result["edges"]) == 1
        assert len(result["rows"]) == 1
        retained = result["rows"][0]["path_records"]
        assert len(retained) == 1
        assert retained[0]["nodes"] == [
            {"node_id": HLA_ID}, {"node_id": "P1"}]
        assert retained[0]["edges"][0]["edge"] == [
            HLA_ID, "PHYSICAL_INTERACTION", "P1"]
        verified = extract_path_records(step, result["rows"], result["nodes"])
        assert len(verified) == 1
        assert verified[0]["nodes"][1]["role"] == "partner"
    asyncio.run(check())


def test_path_wrapper_bytes_alone_truncate_to_an_atomic_prefix():
    async def retrieve(records, max_bytes):
        graph = Graph()
        focus = Node(graph, "n0", 0, ["Gene"], {"id": HLA_ID})
        partner = Node(graph, "n1", 1, ["Gene"], {"id": "P1"})
        relation_type = graph.relationship_type("PHYSICAL_INTERACTION")
        edge = relation_type(graph, "r1", 10, {"source": "same"})
        edge._start_node, edge._end_node = focus, partner
        path = {"nodes": [focus, partner], "edges": [edge]}
        tx = FakeTransaction([{"path_records": [path] * records}])
        adapter = object.__new__(GraphAdapter)
        adapter.settings = SimpleNamespace(
            graph_timeout=1, max_nodes=10, max_edges=10,
            max_rows=1000, max_bytes=max_bytes)
        adapter._session = lambda: FakeSession(tx)
        result = await adapter._retrieve(
            "RETURN path_records", {}, {"bounded_path_records": True})
        return result, tx

    async def check():
        one, _ = await retrieve(1, 100_000)
        two, _ = await retrieve(2, 100_000)
        assert one["materialized_bytes"] < two["materialized_bytes"]
        cap = (one["materialized_bytes"] + two["materialized_bytes"]) // 2
        result, tx = await retrieve(2, cap)
        assert result["status"] == "partial"
        assert result["materialized_bytes"] <= cap
        assert len(result["rows"][0]["path_records"]) == 1
        assert len(result["nodes"]) == 2 and len(result["edges"]) == 1
        assert result["retrieval_execution"]["cursor_exhausted"] is False
        assert tx.rolled_back is True
    asyncio.run(check())


def test_path_record_that_cannot_fit_leaves_no_orphan_graph_materialization():
    async def check():
        graph = Graph()
        focus = Node(graph, "n0", 0, ["Gene"], {
            "id": HLA_ID, "oversized": "x" * 10_000})
        partner = Node(graph, "n1", 1, ["Gene"], {"id": "P1"})
        relation_type = graph.relationship_type("PHYSICAL_INTERACTION")
        edge = relation_type(graph, "r1", 10, {})
        edge._start_node, edge._end_node = focus, partner
        tx = FakeTransaction([{"path_records": [
            {"nodes": [focus, partner], "edges": [edge]}]}])
        adapter = object.__new__(GraphAdapter)
        adapter.settings = SimpleNamespace(
            graph_timeout=1, max_nodes=10, max_edges=10,
            max_rows=1000, max_bytes=500)
        adapter._session = lambda: FakeSession(tx)
        result = await adapter._retrieve(
            "RETURN path_records", {}, {"bounded_path_records": True})
        assert result["status"] == "partial"
        assert result["nodes"] == [] and result["edges"] == []
        assert result["rows"] == []
        assert result["materialized_bytes"] == 0
        assert tx.rolled_back is True
    asyncio.run(check())


def test_second_path_edge_limit_rolls_back_its_new_partner_node():
    async def check():
        graph = Graph()
        focus = Node(graph, "n0", 0, ["Gene"], {"id": HLA_ID})
        first_partner = Node(graph, "n1", 1, ["Gene"], {"id": "P1"})
        second_partner = Node(graph, "n2", 2, ["Gene"], {"id": "P2"})
        relation_type = graph.relationship_type("PHYSICAL_INTERACTION")
        first = relation_type(graph, "r1", 10, {})
        second = relation_type(graph, "r2", 11, {})
        first._start_node, first._end_node = focus, first_partner
        second._start_node, second._end_node = focus, second_partner
        tx = FakeTransaction([{"path_records": [
            {"nodes": [focus, first_partner], "edges": [first]},
            {"nodes": [focus, second_partner], "edges": [second]},
        ]}])
        adapter = object.__new__(GraphAdapter)
        adapter.settings = SimpleNamespace(
            graph_timeout=1, max_nodes=10, max_edges=1,
            max_rows=1000, max_bytes=100_000)
        adapter._session = lambda: FakeSession(tx)
        result = await adapter._retrieve(
            "RETURN path_records", {}, {"bounded_path_records": True})
        assert result["status"] == "partial"
        assert {node["id"] for node in result["nodes"]} == {HLA_ID, "P1"}
        assert len(result["edges"]) == 1
        assert len(result["rows"][0]["path_records"]) == 1
        assert tx.rolled_back is True
    asyncio.run(check())


def test_execute_exposes_atomic_partial_paths_and_incomplete_chain_facts():
    plan, _ = scoped_hla_plan(STORED)
    step = plan["steps"][1]
    edge = {
        "start_id": HLA_ID, "end_id": "P1", "type": "PHYSICAL_INTERACTION",
        "properties": {"biogrid_interaction_id": "partial"},
    }
    fingerprint = _public_edge_fingerprint(edge)

    async def check():
        adapter = FakeAdapter([])
        adapter.settings.graph_version = REGISTRY["release"]
        adapter.settings.grounded_query_policy = True
        adapter.settings.max_nodes = 5000
        adapter.settings.max_edges = 5000
        adapter.settings.max_rows = 1000
        adapter.settings.max_bytes = 8_000_000
        adapter.settings.cypher_timeout = 1
        adapter.settings.cypher_generation_concurrency = 1
        adapter.answer = {
            "nodes": _path_nodes()[:2], "edges": [edge],
            "rows": [{"path_records": [{
                "nodes": [{"node_id": HLA_ID}, {"node_id": "P1"}],
                "edges": [{"edge": [HLA_ID, "PHYSICAL_INTERACTION", "P1"],
                           "fingerprint": fingerprint}],
            }]}],
            "status": "partial", "truncated": True, "materialized_bytes": 1000,
            "retrieval_execution": {"completed": True, "cursor_exhausted": False,
                                    "mode": "read_only"},
        }
        result = await adapter.execute(step, {}, lambda *_args: asyncio.sleep(0))
        assert result["status"] == "partial" and result["truncated"] is True
        assert result["rows"] == []
        assert len(result["path_records"]) == 1
        assert result["path_records"][0]["nodes"][1]["role"] == "partner"
        assert result["chain_facts"]["record_count"] == 1
        assert len(result["chain_facts"]["records"]) == 1
        assert result["chain_facts"]["complete_for_requested_scope"] is False
    asyncio.run(check())


def test_execute_fails_closed_on_graph_only_partial_bounded_path_evidence():
    plan, _ = scoped_hla_plan(STORED)
    step = plan["steps"][1]

    async def check():
        adapter = FakeAdapter([])
        adapter.settings.graph_version = REGISTRY["release"]
        adapter.settings.grounded_query_policy = True
        adapter.settings.max_nodes = 5000
        adapter.settings.max_edges = 5000
        adapter.settings.max_rows = 1000
        adapter.settings.max_bytes = 8_000_000
        adapter.settings.cypher_timeout = 1
        adapter.settings.cypher_generation_concurrency = 1
        adapter.answer = {
            "nodes": _path_nodes()[:2], "edges": [], "rows": [],
            "status": "partial", "truncated": True, "materialized_bytes": 1000,
            "retrieval_execution": {"completed": True, "cursor_exhausted": False,
                                    "mode": "read_only"},
        }
        result = await adapter.execute(step, {}, lambda *_args: asyncio.sleep(0))
        assert result["status"] == "failed"
        assert result["error"]["category"] == "invalid_bounded_path_evidence"
        assert result["validation"][-1]["reasons"] == [
            "invalid_bounded_path_evidence:missing_path_records_for_materialized_graph"]
        assert "path_records" not in result and "chain_facts" not in result
    asyncio.run(check())


@pytest.mark.parametrize("count,expected,unique,retained", [
    (1391, "complete", True, 1391),
    (MAX_PATH_RECORDS + 1, "partial", True, MAX_PATH_RECORDS),
    (MAX_PATH_RECORDS + 1, "partial", False, 1),
])
def test_execute_uses_local_route_and_marks_raw_overfetch_before_dedup(
        count, expected, unique, retained):
    plan, _ = scoped_hla_plan(STORED)
    step = plan["steps"][1]
    async def check():
        adapter = FakeAdapter([])
        adapter.settings.graph_version = REGISTRY["release"]
        adapter.settings.grounded_query_policy = True
        adapter.settings.max_nodes = 5000
        adapter.settings.max_edges = 5000
        adapter.settings.max_rows = 1000
        adapter.settings.max_bytes = 8_000_000
        adapter.settings.cypher_timeout = 1
        adapter.settings.cypher_generation_concurrency = 1
        adapter.answer = {
            "nodes": _path_nodes()[:3], "edges": [],
            "rows": [{"path_records": [{
                "nodes": [{"node_id": HLA_ID}, {"node_id": "P1"}],
                "edges": [{"edge": [HLA_ID, "PHYSICAL_INTERACTION", "P1"],
                           "fingerprint": f"f{index:05d}" if unique else "same"}],
            } for index in range(count)]}],
            "status": "complete", "truncated": False, "materialized_bytes": 1000,
            "retrieval_execution": {"completed": True, "cursor_exhausted": True,
                                    "mode": "read_only"},
        }
        result = await adapter.execute(step, {}, lambda *_args: asyncio.sleep(0))
        assert not adapter.generated
        assert result["query_route"] == "template"
        assert result["status"] == expected
        assert result["rows"] == []
        assert len(result["path_records"]) == retained
        assert result["chain_facts"]["complete_for_requested_scope"] is (expected == "complete")
        if expected == "partial":
            assert result["truncated"]
            assert result["retrieval_execution"]["path_record_limit_reached"] is True
            assert any("bounded_path_record_limit" in check.get("reasons", [])
                       for check in result["validation"])
    asyncio.run(check())


def test_overfetch_sentinel_is_not_exposed_as_a_node_edge_or_path():
    plan, _ = scoped_hla_plan(STORED)
    step = plan["steps"][1]
    retained_edge = {
        "start_id": HLA_ID, "end_id": "P1", "type": "PHYSICAL_INTERACTION",
        "properties": {"biogrid_interaction_id": "retained"},
    }
    sentinel_edge = {
        "start_id": HLA_ID, "end_id": "P2", "type": "PHYSICAL_INTERACTION",
        "properties": {"biogrid_interaction_id": "sentinel"},
    }
    retained_fingerprint = _public_edge_fingerprint(retained_edge)
    sentinel_fingerprint = _public_edge_fingerprint(sentinel_edge)
    retained_record = {
        "nodes": [{"node_id": HLA_ID}, {"node_id": "P1"}],
        "edges": [{"edge": [HLA_ID, "PHYSICAL_INTERACTION", "P1"],
                   "fingerprint": retained_fingerprint}],
    }
    sentinel_record = {
        "nodes": [{"node_id": HLA_ID}, {"node_id": "P2"}],
        "edges": [{"edge": [HLA_ID, "PHYSICAL_INTERACTION", "P2"],
                   "fingerprint": sentinel_fingerprint}],
    }

    async def check():
        adapter = FakeAdapter([])
        adapter.settings.graph_version = REGISTRY["release"]
        adapter.settings.grounded_query_policy = True
        adapter.settings.max_nodes = 5000
        adapter.settings.max_edges = 5000
        adapter.settings.max_rows = 1000
        adapter.settings.max_bytes = 8_000_000
        adapter.settings.cypher_timeout = 1
        adapter.settings.cypher_generation_concurrency = 1
        adapter.answer = {
            "nodes": _path_nodes()[:3],
            "edges": [retained_edge, sentinel_edge],
            "rows": [{"path_records": [deepcopy(retained_record)
                                         for _index in range(MAX_PATH_RECORDS)]
                                        + [sentinel_record]}],
            "status": "complete", "truncated": False,
            "materialized_bytes": 4321,
            "retrieval_execution": {"completed": True, "cursor_exhausted": True,
                                    "mode": "read_only"},
        }
        result = await adapter.execute(step, {}, lambda *_args: asyncio.sleep(0))
        assert result["status"] == "partial"
        assert result["materialized_bytes"] == 4321
        assert {node["id"] for node in result["nodes"]} == {HLA_ID, "P1"}
        assert result["edges"] == [retained_edge]
        assert len(result["path_records"]) == 1
        assert result["path_records"][0]["nodes"][1]["id"] == "P1"
        assert all(edge["fingerprint"] != sentinel_fingerprint
                   for record in result["path_records"] for edge in record["edges"])
    asyncio.run(check())


def test_unsupported_path_fails_without_gpu_fallback():
    step = four_node_step()
    step["evidence_combination"] = "independent"
    async def check():
        adapter = FakeAdapter([["MATCH (n) RETURN n"]])
        result = await adapter.execute(step, {}, lambda *_args: asyncio.sleep(0))
        assert result["error"]["category"] == "unsupported_bounded_path_spec"
        assert result["validation"][0]["reasons"] == [
            "unsupported_bounded_path_spec:bounded_path_requires_cooccurrence"]
        assert not adapter.generated and not adapter.retrieved
    asyncio.run(check())


def test_legacy_one_hop_step_keeps_template_structure_and_execution_route():
    constraint = {"property": "id", "operator": "=", "value": HLA_ID,
                  "entity_type": "Gene"}
    step = {
        "id": "legacy", "question": "Find HLA-DRA enriched cell types.",
        "relation_types": ["GENE_ENRICHED_IN"], "depends_on": [],
        "constraints": [constraint], "complete": True,
        "evidence_combination": "independent",
        "semantic_request": {"source": "user_request",
                             "question": "Find HLA-DRA enriched cell types."},
    }

    async def check():
        adapter = FakeAdapter([])
        adapter.settings.graph_version = REGISTRY["release"]
        adapter.settings.grounded_query_policy = True
        prepared_step = await adapter._prepare_step(
            deepcopy(step), lambda *_args: asyncio.sleep(0))
        template = compile_one_hop_query(prepared_step)
        assert template is not None
        assert template["template_id"] == "directed_relation_records"
        assert "path_record_overfetch" not in template["parameters"]

        result = await adapter.execute(step, {}, lambda *_args: asyncio.sleep(0))
        assert not adapter.generated
        assert result["query_route"] == "template"
        assert result["query_template"]["template_id"] == "directed_relation_records"
        assert "path_records" not in result
        assert "chain_facts" not in result
    asyncio.run(check())


def test_chain_facts_drive_answer_catalogue_without_top_level_path_spec():
    plan, _ = scoped_hla_plan(STORED)
    step = plan["steps"][2]
    records = extract_path_records(step, [{"path_records": [
        _path_row(),
        _path_row(partner="P2", process="R1", interaction="GENETIC_INTERACTION",
                  interaction_fingerprint="i2", annotation_fingerprint="a2"),
    ]}], _path_nodes())
    chain = derive_chain_facts(step, records, _path_nodes(), status="partial", truncated=True)
    evidence = {"evidence_id": "G1", "step_id": "partner_annotations",
                "question": step["question"], "status": "partial", "truncated": True,
                "graph_version": REGISTRY["release"], "nodes": _path_nodes(), "edges": [],
                "rows": [], "chain_facts": chain,
                "requested_scope": {"original_question": STORED,
                                    "path_spec": deepcopy(step["path_spec"]),
                                    "relation_types": deepcopy(step["relation_types"]),
                                    "constraints": deepcopy(step["constraints"]), "complete": True}}
    facts = catalogue([evidence])
    kinds = [fact["kind"] for fact in facts]
    assert kinds.count("joined_partner_annotations") == 2
    assert any("physical-interaction" in fact["text"] for fact in facts)
    assert any("genetic-interaction" in fact["text"] for fact in facts)
    assert any("bounded-path retrieval is incomplete" in fact["text"] for fact in facts)
    assert any("Antigen presentation and T1D are request context" in fact["text"] for fact in facts)
    assert all(fact["mandatory"] for fact in facts
               if fact["kind"] in {"joined_partner_annotations", "limitation"})


def test_partner_only_chain_retains_unannotated_interaction_partners():
    plan, _ = scoped_hla_plan(STORED)
    step = plan["steps"][1]
    row = {"nodes": [{"node_id": HLA_ID}, {"node_id": "P1"}],
           "edges": [{"edge": [HLA_ID, "PHYSICAL_INTERACTION", "P1"],
                      "fingerprint": "physical-one"}]}
    records = extract_path_records(step, [{"path_records": [row]}], _path_nodes())
    chain = derive_chain_facts(step, records, _path_nodes(),
                               status="complete", truncated=False)
    evidence = {
        "evidence_id": "G1", "step_id": "interaction_partners",
        "question": step["question"], "status": "complete", "truncated": False,
        "graph_version": REGISTRY["release"], "nodes": _path_nodes()[:2],
        "edges": [], "rows": [], "chain_facts": chain,
        "requested_scope": {"original_question": STORED,
                            "path_spec": deepcopy(step["path_spec"]),
                            "relation_types": deepcopy(step["relation_types"]),
                            "constraints": deepcopy(step["constraints"]),
                            "complete": True},
    }
    facts = catalogue([evidence])
    partner_facts = [fact for fact in facts
                     if fact["kind"] == "interaction_path_partners"]
    assert len(partner_facts) == 1
    assert "PARTNER1" in partner_facts[0]["text"]
    assert not any(fact["kind"] == "joined_partner_annotations" for fact in facts)


def test_generic_four_node_chain_catalogue_preserves_ordered_same_path_association():
    step = four_node_step()
    row = {
        "nodes": [{"node_id": HLA_ID}, {"node_id": "P1"},
                  {"node_id": "P2"}, {"node_id": "K1"}],
        "edges": [
            {"edge": [HLA_ID, "PHYSICAL_INTERACTION", "P1"],
             "fingerprint": "physical-focus-partner"},
            {"edge": ["P1", "PHYSICAL_INTERACTION", "P2"],
             "fingerprint": "physical-partners"},
            {"edge": ["P2", "FUNCTION_ANNOTATION", "K1"],
             "fingerprint": "partner-process"},
        ],
    }
    records = extract_path_records(step, [{"path_records": [row]}], _path_nodes())
    chain = derive_chain_facts(step, records, _path_nodes(),
                               status="complete", truncated=False)
    evidence = {
        "evidence_id": "G1", "step_id": "four", "question": step["question"],
        "status": "complete", "truncated": False, "graph_version": REGISTRY["release"],
        "nodes": _path_nodes(), "edges": [], "rows": [], "chain_facts": chain,
        "requested_scope": {"original_question": step["question"],
                            "path_spec": deepcopy(step["path_spec"]),
                            "relation_types": deepcopy(step["relation_types"]),
                            "constraints": deepcopy(step["constraints"]),
                            "complete": True},
    }
    facts = [fact for fact in catalogue([evidence])
             if fact["kind"] == "bounded_path_associations"]
    assert len(facts) == 1 and facts[0]["mandatory"]
    prose = facts[0]["text"].replace("\\_", "_")
    for expected in ("HLA-DRA", HLA_ID, "PARTNER1", "P1", "PARTNER2", "P2",
                     "Antigen processing", "K1", "PHYSICAL_INTERACTION",
                     "FUNCTION_ANNOTATION"):
        assert expected in prose
    assert prose.index("focus=") < prose.index("partner=") < prose.index(
        "partner_two=") < prose.index("process=")


def test_saved_revision_and_failed_step_preserve_path_contract():
    plan, _ = scoped_hla_plan(STORED)
    clean, _summary = parent_context({"plan": plan, "preview": {}})
    assert clean["steps"][2]["path_spec"] == plan["steps"][2]["path_spec"]
    failed = Runtime.failed_step(plan["steps"][2], {"category": "graph_unavailable",
                                                    "message": "unavailable"})
    assert failed["requested_scope"]["path_spec"] == plan["steps"][2]["path_spec"]
    legacy = deepcopy(plan)
    for step in legacy["steps"]:
        step.pop("path_spec")
    legacy_clean, _ = parent_context({"plan": legacy, "preview": {}})
    assert all("path_spec" not in step for step in legacy_clean["steps"])


def test_adapter_identity_includes_bounded_path_contract():
    adapter = object.__new__(GraphAdapter)
    adapter.settings = SimpleNamespace(graph_identity_file="/definitely/missing",
                                       graph_version=REGISTRY["release"])
    adapter.identity_verified = True
    identity = adapter.preview_identity()
    assert identity["bounded_path_version"] == "bounded-path-v1"
    assert len(identity["bounded_path_sha256"]) == 64
