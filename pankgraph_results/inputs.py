"""Versioned input adapters. Agent evidence is a persisted snapshot, never a query."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .query import parameters_for


class ResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID | None = None
    phase: Literal["preview", "final"] = "final"
    template_id: str | None = None
    parameters: dict[str, str | None] = Field(default_factory=dict)
    question: str = Field(default="", max_length=6000)

    @model_validator(mode="after")
    def source_is_unambiguous(self):
        if bool(self.run_id) == bool(self.template_id):
            raise ValueError("exactly_one_result_source_required")
        if self.run_id and (self.parameters or self.question):
            raise ValueError("agent_input_comes_from_persisted_run")
        if self.template_id:
            self.parameters = parameters_for(self.template_id, self.parameters)
        return self


def agent_snapshot(run, phase, graph_version):
    evidence = (run.get("preview") or {}).get("evidence") if phase == "preview" else run.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("evidence_not_ready")
    if evidence.get("graph_version") != graph_version:
        raise ValueError("graph_release_mismatch")
    plan = run.get("plan") or {}
    focus = list(dict.fromkeys(entity["id"] for step in plan.get("steps", [])
        for entity in step.get("resolved_entities", [])
        if entity.get("state") == "resolved" and entity.get("id") and entity.get("graph_version") == graph_version))
    return {"kind": "agent", "run_id": run["run_id"], "session_id": run.get("session_id"),
        "phase": phase, "question": run.get("question") or plan.get("question", ""),
        "evidence": evidence, "focus_ids": focus, "answer": (run.get("graph_answer") or "") if phase == "final" else "",
        "literature": (run.get("literature") or []) if phase == "final" else [], "run_status": run.get("status"),
        "retrieval": "persisted_preview" if phase == "preview" else "persisted_final"}


def template_snapshot(body, graph_version):
    return {"kind": "template", "template_id": body.template_id, "parameters": body.parameters,
        "question": body.question or template_question(body.template_id, body.parameters),
        "question_supplied": bool(body.question),
        "focus_ids": list(dict.fromkeys(value for key, value in body.parameters.items()
            if key in {"gene_id", "variant_id", "lead_variant_id", "disease_id", "cell_id"} and value)),
        "graph_version": graph_version}


def template_question(template_id, parameters, evidence=None):
    names = {str(node.get("id")): (node.get("properties") or {}).get("name") or str(node.get("id"))
        for node in (evidence or {}).get("nodes", [])}
    gene = names.get(parameters.get("gene_id"), parameters.get("gene_id", "this gene"))
    variant = parameters.get("variant_id", "this variant")
    disease = names.get(parameters.get("disease_id"), "type 1 diabetes" if parameters.get("disease_id") == "MONDO_0005147" else parameters.get("disease_id", "the selected disease"))
    return {
        "qtl_by_gene": f"Which SNPs serve as lead QTLs for {gene}?",
        "qtl_by_variant_gene": f"Does {variant} serve as a QTL for {gene}?",
        "qtl_by_variant": f"Which genes are linked to {variant} by QTL evidence?",
        "gwas_by_variant": f"Does {variant} have a GWAS association with {disease}?",
        "coloc_by_gene": f"Does the QTL signal for {gene} colocalize with a {disease} GWAS signal?",
        "expression_by_gene": f"How does {gene} expression change in type 1 diabetes versus non-diabetic samples?",
    }[template_id]
