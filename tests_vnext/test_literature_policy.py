"""Scientific intent changes only optional literature, never graph scope."""

from copy import deepcopy

import pytest

from pankagent_vnext.literature_policy import apply_literature_policy, POLICY_VERSION


@pytest.mark.parametrize("question", [
    "Is CFTR specifically enriched in ductal cells?",
    "Is\u00a0CFTR\u00a0specifically enriched in ductal cells?",
    "Is GCG enriched in alpha cells?",
    "Is KRT19 selectively expressed in ductal cells?",
    "Does expression of this gene indicate ductal identity?",
    "Is GCG a reliable alpha-cell marker?",
    "Explain why INS expression differs between these conditions.",
    "Does lack of gene detection prove biological absence?",
    "What conflicting evidence explains this mechanism?",
])
def test_scientific_interpretation_repairs_false_model_flag_without_changing_graph(question):
    plan = {"interpreted_question": question, "literature": False,
            "steps": [{"id": "s1", "question": question, "constraints": [], "complete": True}]}
    original = deepcopy(plan)
    decided = apply_literature_policy(plan, question)
    assert decided["literature"] is True
    assert decided["literature_intent"]["included"] is True
    assert decided["literature_intent"]["reason"] == "scientific_interpretation"
    assert decided["literature_intent"]["policy_version"] == POLICY_VERSION
    assert "After confirmation" in decided["literature_intent"]["summary"]
    assert decided["steps"] == original["steps"] and plan == original


@pytest.mark.parametrize("opt_out", [
    "Graph-only.", "Use PanKgraph only.", "Only use the knowledge graph.", "Use only the graph.",
    "Only graph evidence, please.", "No literature.", "Without external papers.",
    "Skip the literature search.", "Don't include literature.",
    "Do not search for publications.", "Do not use external sources.",
])
def test_explicit_opt_out_wins_even_when_model_rewrites_it_away(opt_out):
    biological_question = "Is CFTR specifically enriched in ductal cells?"
    plan = {"interpreted_question": biological_question, "steps": [], "literature": True}
    decided = apply_literature_policy(plan, biological_question + " " + opt_out)
    assert decided["literature"] is False
    assert decided["literature_intent"]["reason"] == "explicit_opt_out"


@pytest.mark.parametrize("question", [
    "What is the Ensembl ID for CFTR?", "Find the gene identifier for INS.",
    "Show the gene symbol for ENSG00000115263.",
])
def test_identifier_lookup_does_not_inherit_spurious_literature_flag(question):
    result = apply_literature_policy({"interpreted_question": question, "literature": True}, question)
    assert result["literature"] is False
    assert result["literature_intent"]["reason"] == "identifier_lookup"


@pytest.mark.parametrize("question", [
    "Which cell types express INS?", "List the GCG enrichment values for alpha cells.",
    "Show CFTR log2 fold change and padj in ductal cells.", "Find genes connected to this pathway.",
    "List the marker genes annotated for ductal cells.",
])
def test_ordinary_data_lookup_stays_graph_only(question):
    assert apply_literature_policy({"literature": False}, question)["literature"] is False


def test_explicit_literature_and_resolved_followup_intent():
    lookup = "What is the Ensembl ID for INS? Include published evidence."
    assert apply_literature_policy({"literature": False}, lookup)["literature"] is True
    followup = apply_literature_policy({"literature": False,
        "interpreted_question": "Is GCG specifically enriched in alpha cells?"}, "What about GCG?")
    assert followup["literature"] is True
    mistaken_rewrite = apply_literature_policy({"literature": False,
        "interpreted_question": "Find INS in the graph only."}, "Find INS and include literature evidence.")
    assert mistaken_rewrite["literature"] is True


@pytest.mark.parametrize("question", [
    "Why is there no published evidence for exclusive expression?",
    "Is there no literature supporting this mechanism?",
    "No papers support this interpretation; is that evidence of absence?",
])
def test_questions_about_missing_literature_are_not_opt_outs(question):
    result = apply_literature_policy({"literature": False}, question)
    assert result["literature"] is True
    assert result["literature_intent"]["reason"] != "explicit_opt_out"
