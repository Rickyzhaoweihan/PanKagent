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
