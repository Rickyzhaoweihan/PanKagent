"""Cheap literature intent safeguards; they never change the graph investigation."""

import re


POLICY_VERSION = "scientific-intent-v2"
_GRAPH = r"(?:pan\s*k\s*graph|knowledge\s+graph|graph|kg)"
_LITERATURE = r"(?:literature|papers?|publications?|published\s+(?:studies|evidence)|external\s+(?:sources|evidence))"
_OPTOUT = re.compile(
    rf"\b{_GRAPH}\s*[- ]\s*only\b|\bonly\s+(?:the\s+)?{_GRAPH}\s+(?:evidence|results|data)\b"
    rf"|\bonly\s+(?:use|query|consult|search|check)\s+(?:the\s+)?{_GRAPH}\b"
    rf"|\b(?:use|query|consult|search|check)\s+only\s+(?:the\s+)?{_GRAPH}\b"
    rf"|\b(?:use|using|from)\s+(?:the\s+)?{_GRAPH}(?:\s+(?:evidence|results|data))?\s+only\b"
    rf"|(?:^|[.!?;,(])\s*no\s+(?:(?:the|any|additional|external|published)\s+)*{_LITERATURE}"
    rf"(?:\s+(?:search|enrichment))?(?:\s+please)?(?=$|[.!?;,)])"
    rf"|\b(?:without|skip|exclude|avoid|disable|remove)\s+(?:(?:the|any|additional|external|published)\s+)*{_LITERATURE}\b"
    rf"|\b(?:do\s+not|don't|dont)\s+(?:use|search|include|consult|retrieve|add)\s+"
    rf"(?:(?:the|any|for|additional|external)\s+)*{_LITERATURE}\b",
    re.I,
)
_EXPLICIT = re.compile(r"\b(?:literature|papers?|publications?|pubmed|published\s+(?:studies|evidence|research))\b", re.I)
_EXPLANATION = re.compile(r"\b(?:why|explain|explanation|mechanisms?|interpret|interpretation|biological\s+(?:meaning|significance)|conflicting\s+evidence|alternative\s+explanations?)\b", re.I)
_BIOLOGY = re.compile(r"\b(?:express(?:ed|ion|ing)?|enrich(?:ed|ment)?|detect(?:ed|ion)?|genes?|cells?|tissues?|proteins?|markers?)\b", re.I)
_SPECIFICITY = re.compile(r"\b(?:specific(?:ally|ity)?|selective(?:ly)?|exclusive(?:ly)?|unique(?:ly)?)\b", re.I)
_INFERENCE = re.compile(r"\b(?:mean|means|imply|implies|prove|proves|support|supports|indicate|indicates|absence|reliable)\b", re.I)
_ENRICHMENT_QUESTION = re.compile(r"\b(?:is|are|was|were|does|do|whether)\b[^?;\n]{0,180}\benrich(?:ed|ment)?\b", re.I)
_SIMPLE_ID = re.compile(
    r"^(?:(?:what|which)\s+(?:is|are)\s+|(?:find|show|get|retrieve|look\s+up|give(?:\s+me)?)\s+)"
    r".{0,140}\b(?:identifiers?|ids?|ensembl\s+ids?|entrez\s+ids?|gene\s+symbols?)\b", re.I)


def apply_literature_policy(plan: dict, question: str) -> dict:
    """Honor explicit exclusions, then repair false negatives for interpretation.

    The original request remains authoritative if a model rewrites it. Interpreted
    wording also supplies context for short follow-ups. Ordinary graph lookups
    retain the planner's choice; a simple identifier lookup defaults graph-only.
    """
    original = " ".join(str(question).split()).replace("’", "'")
    interpreted = " ".join(str(plan.get("interpreted_question") or "").split()).replace("’", "'")
    text = original + "\n" + interpreted
    explicit_opt_out = _OPTOUT.search(original) or (not _EXPLICIT.search(original) and _OPTOUT.search(interpreted))
    if explicit_opt_out:
        included, reason, summary = False, "explicit_opt_out", "Use graph evidence only, as requested."
    elif _EXPLICIT.search(text):
        included, reason, summary = True, "explicit_request", "After confirmation, retrieve literature evidence and linked references."
    elif _SIMPLE_ID.search(original) and not _EXPLANATION.search(original):
        included, reason, summary = False, "identifier_lookup", "A simple identifier lookup uses graph evidence."
    elif (_EXPLANATION.search(text) or _ENRICHMENT_QUESTION.search(text)
          or (_BIOLOGY.search(text) and (_SPECIFICITY.search(text) or _INFERENCE.search(text)))):
        included, reason, summary = True, "scientific_interpretation", (
            "After confirmation, add literature context and alternative explanations to help interpret the graph evidence; "
            "keep the graph answer and linked literature perspectives separate.")
    elif plan.get("literature") is True:
        included, reason, summary = True, "planner_request", "After confirmation, add literature context and linked references."
    else:
        included, reason, summary = False, "graph_lookup", "This lookup uses graph evidence; literature enrichment is not requested."
    return {**plan, "literature": included,
            "literature_intent": {"included": included, "reason": reason, "summary": summary,
                                  "policy_version": POLICY_VERSION}}


def explicit_preference(question):
    text = ' '.join(str(question).split()).replace('’', "'")
    if _OPTOUT.search(text):
        return False
    if re.search(r'\b(?:enable|add|include|use|search|retrieve)\s+(?:the\s+)?'+_LITERATURE, text, re.I):
        return True
    return None


def preserve_revision_preference(plan, parent_plan, instruction):
    choice = explicit_preference(instruction)
    if choice is None and (parent_plan.get('literature_intent') or {}).get('reason') in {'explicit_opt_out', 'explicit_request', 'inherited_explicit'}:
        choice = parent_plan.get('literature', False)
    if choice is None:
        return plan
    # A narrowly recognized preference-only edit cannot change the biology.
    # Mixed instructions remain with the structured planner and its full context.
    if re.fullmatch(r'(?:please\s+)?(?:disable|remove|skip|exclude|enable|add|include|use)\s+(?:the\s+)?'+_LITERATURE+r'(?:\s+please)?[.!]?', instruction.strip(), re.I):
        from copy import deepcopy
        for key in ('steps', 'entities', 'interpreted_question', 'clarification'):
            if key in parent_plan:
                plan[key] = deepcopy(parent_plan[key])
    return {**plan, 'literature': choice, 'literature_intent': {
        'included': choice, 'reason': 'explicit_request' if choice else 'explicit_opt_out',
        'summary': 'Include linked literature evidence as requested.' if choice else 'Use graph evidence only, as requested.',
        'policy_version': POLICY_VERSION}}
