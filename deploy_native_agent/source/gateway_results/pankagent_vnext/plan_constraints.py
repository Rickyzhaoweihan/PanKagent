"""Narrow, deterministic repair of explicit common-cell constraints.

Names and IDs were verified against the RL 08_04 release. Ambiguous or modified
cell requests remain the planner's responsibility; this never relaxes a filter.
"""
import json
import re


CELL_TYPES = {
    "alpha": ("alpha cell", "CL_0000171"),
    "beta": ("beta cell", "CL_0000169"),
    "delta": ("delta cell", "CL_0000173"),
    "ductal": ("ductal cell", "CL_0002079"),
    "endothelial": ("endothelial cell", "CL_0000115"),
}
_KINDS = "|".join(CELL_TYPES)
_TARGET = re.compile(r"\b(?:in|within|to)\s+(?:pancreatic\s+)?(" + _KINDS + r")\s+cells?\b", re.I)
_QUOTED = re.compile(r"```[\s\S]*?```|`[^`]*`|\"[^\"]*\"|'[^']*'|“[^”]*”|‘[^’]*’")
_AMBIGUOUS = re.compile(r"\b(?:not|no|neither|nor|except|exclude|excluding|without|or|versus|vs|compare|compared|comparison|between|rather|instead)\b", re.I)
_MODIFIED = re.compile(r"\b(?:positive|negative|expressing|subtype|subtypes|subclass|subclasses|subset|subsets|immature|mature|progenitor)\b|[+-]\s*cells?\b", re.I)


def _has_constraint(constraints, prop, value):
    for constraint in constraints:
        if not isinstance(constraint, dict) or str(constraint.get("property", "")).split(".")[-1] != prop:
            continue
        operator = str(constraint.get("operator", "=")).upper()
        actual = constraint.get("value")
        if operator == "=" and actual == value:
            return True
        if operator == "IN":
            if isinstance(actual, str):
                try:
                    actual = json.loads(actual)
                except ValueError:
                    continue
            if actual == [value]:
                return True
    return False


def repair_step_constraints(step: dict) -> dict:
    """Return a copy, adding at most one certain cell filter without rewriting."""
    repaired = {**step, "constraints": [dict(item) if isinstance(item, dict) else item
                                        for item in step.get("constraints", [])]}
    from .graph_contract import normalize_release_constraints
    repaired = normalize_release_constraints(repaired)
    question = step.get("question")
    if not isinstance(question, str):
        return repaired
    raw_question = question
    question = _QUOTED.sub(lambda match: " " * len(match.group()), question)
    if _AMBIGUOUS.search(question) or _MODIFIED.search(question):
        return repaired
    targets = list(_TARGET.finditer(question))
    # Counting all cell mentions also catches an unsupported second cell type.
    if len(targets) != 1 or len(re.findall(r"\bcells?\b", question, re.I)) != 1:
        return repaired
    if len(re.findall(r"\b(?:" + _KINDS + r")\b", question, re.I)) != 1:
        return repaired
    target = targets[0]
    name, identifier = CELL_TYPES[target.group(1).lower()]
    tail = raw_question[target.end():]
    # A trailing marker/name can identify a narrower graph cell subclass.
    # Only the verified ID or an explicit relation annotation is harmless.
    annotation = r"(?:" + identifier + r"|\(\s*" + identifier + r"\s*\)|\([A-Z][A-Z0-9_]* relation\))"
    if not re.fullmatch(r"\s*(?:" + annotation + r"\s*)?[?.!,;:]*\s*", tail):
        return repaired
    explicit_ids = set(re.findall(r"\bCL_\d+\b", question))
    if explicit_ids and explicit_ids != {identifier}:
        return repaired
    constraints = repaired["constraints"]
    if _has_constraint(constraints, "id", identifier) or _has_constraint(constraints, "name", name):
        return repaired
    prop, value = ("id", identifier) if explicit_ids else ("name", name)
    constraints.append({"property": prop, "operator": "=", "value": value})
    return repaired


def build_generation_question(step: dict) -> str:
    """Describe the verified schema only for a simple, fully constrained lookup.

    This is generator input, not Cypher. Validation still uses the original plan.
    A full question match prevents dropping an unrecorded modifier or second task.
    """
    question = str(step.get("question", ""))
    typed = resolved_generation_question(step)
    if typed:
        return typed
    constraints = step.get("constraints") or []
    if step.get("depends_on") or not step.get("complete", True) or len(constraints) != 2:
        return question
    if any(not isinstance(item, dict) or str(item.get("property", "")).split(".")[-1] != "name"
           or item.get("operator") != "=" or not isinstance(item.get("value"), str) for item in constraints):
        return question
    values = {item["value"] for item in constraints}
    cells = [(kind, name) for kind, (name, _) in CELL_TYPES.items() if name in values]
    if len(cells) != 1 or len(values) != 2:
        return question
    kind, cell = cells[0]
    gene = next(iter(values - {cell}))
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,39}", gene):
        return question
    literal_gene = re.escape(gene) + r"\s*(?:\(gene\)\s*)?"
    intent = r"(?:is\s+" + literal_gene + r"(?:specifically\s+)?enriched|show\s+enrichment\s+(?:evidence\s+)?for\s+" + literal_gene + r")"
    target = r"\s+(?:in|within)\s+(?:pancreatic\s+)?" + kind + r"\s+cells?"
    suffix = r"\s*(?:\(GENE_ENRICHED_IN relation\))?\s*[?.!]*\s*"
    if not re.fullmatch(r"\s*" + intent + target + suffix, question, re.I):
        return question
    return (f"Find all GENE_ENRICHED_IN relationships from Gene nodes named {gene} "
            f"to anatomical_structure nodes named {cell}. "
            "Return the gene, cell and relationship with their properties, without LIMIT or list slices.")


def step_relation_types(step: dict) -> list[str]:
    """Prefer the reviewed relation contract; infer only explicit old-plan intent."""
    if step.get("relation_types"):
        return list(dict.fromkeys(step.get("relation_types") or []))
    question = str(step.get("question", ""))
    relations = []
    if re.search(r"\benrich(?:ed|ment)\b|\bGENE_ENRICHED_IN\b", question, re.I):
        relations.append("GENE_ENRICHED_IN")
    if re.search(r"\bdetect(?:ed|ion)\b|\bGENE_DETECTED_IN\b", question, re.I):
        relations.append("GENE_DETECTED_IN")
    return relations


def resolved_lookup(step: dict):
    """Return one fully resolved Gene/cell pair, without dropping other filters."""
    constraints = step.get("constraints") or []
    entities = step.get("resolved_entities") or []
    if len(constraints) != 2 or len(entities) != 2 or step.get("depends_on") or not step.get("complete", True):
        return None
    if any(item.get("state") != "resolved" or item.get("graph_version") != step.get("graph_version")
           or not isinstance(item.get("name"), str) or not item["name"] for item in entities):
        return None
    if {item.get("constraint_index") for item in entities} != {0, 1}:
        return None
    genes = [item for item in entities if item.get("entity_type") == "Gene"]
    cells = [item for item in entities if item.get("entity_type") == "anatomical_structure"]
    relations = step_relation_types(step)
    if len(genes) == len(cells) == len(relations) == 1 and relations[0] in {"GENE_ENRICHED_IN", "GENE_DETECTED_IN"}:
        return genes[0], cells[0], relations[0]
    return None


def _simple_resolved_scope(step: dict) -> bool:
    """Unknown prose modifiers keep the full original generator request intact."""
    text = str(step.get("question", ""))
    # A planner may make the evidence basis explicit without changing scope.
    # Match this complete trailing phrase, not arbitrary "based on" clauses.
    text = re.sub(r",?\s+based on measured enrichment evidence(?=[?.!]*\s*$)", "", text, flags=re.I)
    # This exact planner-added clause asks for returned properties, not extra
    # retrieval or filtering. Unknown fields, cutoffs and trailing tasks retain
    # the original request instead of being silently discarded.
    report = re.search(r"[?.!]\s+Report (?:the )?measured enrichment values\s*\(([^()]*)\)\s+supporting this[?.!]*\s*$",
                       text, flags=re.I)
    if report and step_relation_types(step) == ["GENE_ENRICHED_IN"]:
        fields = {" ".join(field.lower().split()) for field in report[1].split(",")}
        allowed_fields = {"log2 fold change", "log2_fold_change", "adjusted p-value", "adjusted p value", "padj",
                          "p-value", "pvalue", "condition", "rank in cell type", "rank_in_cell_type"}
        if fields and fields <= allowed_fields:
            text = text[:report.start()] + "?"
    literals = [str(item[key]) for item in step.get("resolved_entities") or [] for key in ("name", "id") if item.get(key)]
    literals += [literal + "s" for literal in literals if literal.endswith(" cell")]
    literals += [str(item.get("value")) for item in step.get("constraints") or [] if item.get("value")]
    for literal in sorted(set(literals), key=len, reverse=True):
        text = re.sub(r"(?<!\w)" + re.escape(literal) + r"(?!\w)", " ", text, flags=re.I)
    words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text.lower()))
    allowed = set("is are was does do did show find tell me whether what which how much evidence for of the gene genes cell cells type types anatomical_structure pancreatic specifically specific enriched enrichment detected detection expression expressed in within to relation relationships relationship named name id all return gene_enriched_in gene_detected_in".split())
    return words <= allowed and not re.search(r"[<>=0-9]", text)


def resolved_generation_question(step: dict) -> str | None:
    lookup = resolved_lookup(step)
    if lookup and _simple_resolved_scope(step):
        gene, cell, relation = lookup
        return (f"Find all {relation} relationships from Gene nodes named {gene['name']} "
                f"to anatomical_structure nodes named {cell['name']}. Return the gene, cell and relationship "
                "with their properties, without LIMIT or list slices.")
    entities = step.get("resolved_entities") or []
    if (step.get("purpose") == "context" and step.get("context_kind") in {"enrichment_across_cells", "detection_across_cells"}
            and len(entities) == len(step.get("constraints") or []) == 1
            and entities[0].get("state") == "resolved" and entities[0].get("entity_type") == "Gene"
            and entities[0].get("graph_version") == step.get("graph_version")):
        relation = "GENE_DETECTED_IN" if step["context_kind"] == "detection_across_cells" else "GENE_ENRICHED_IN"
        return (f"Find all {relation} relationships from Gene nodes named {entities[0]['name']} "
                "to anatomical_structure nodes. Return the gene, cell and relationship with their properties, "
                "without LIMIT or list slices.")
    return None


def related_context_step(plan: dict) -> dict | None:
    """Propose at most one explicit, independent context read before review."""
    steps = plan.get("steps") or []
    if not plan.get("include_context", True) or len(steps) != 1 or steps[0].get("purpose") == "context":
        return None
    source = steps[0]
    lookup = resolved_lookup(source)
    if not lookup or not _simple_resolved_scope(source):
        return None
    gene, cell, relation = lookup
    gene_filter = dict(source["constraints"][gene["constraint_index"]])
    if relation == "GENE_ENRICHED_IN":
        relation, kind = "GENE_DETECTED_IN", "detection_across_cells"
        question = f"Inspect detection evidence for {gene['name']} across cell types alongside the enrichment result."
        rationale = "Detection elsewhere helps distinguish enrichment from exclusive expression. These measurements may have different cohorts and sources."
    else:
        relation, kind = "GENE_ENRICHED_IN", "enrichment_across_cells"
        question = f"Inspect enrichment evidence for {gene['name']} across cell types alongside the detection result."
        rationale = "Detection alone does not establish specificity. Enrichment is a separate measurement and may use different cohorts and sources."
    return {"id": str(source["id"]) + "_context", "question": question,
            "title": f"Related {'detection' if kind == 'detection_across_cells' else 'enrichment'} context for {gene['name']}", "rationale": rationale,
            "purpose": "context", "context_for": source["id"], "context_kind": kind,
            "constraints": [gene_filter], "depends_on": [], "complete": False, "max_context_nodes": 20,
            "relation_types": [relation]}
