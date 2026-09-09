"""Recognize known interpretation-only requests without generating definitions.

Terms are limited to the versioned BIM interpretation/glossary bundle. This
classifier only prevents an unnecessary literature request; it does not create
an answer, skip graph execution, or establish scientific support.
"""
import re

VERSION = 'known-definition-intent-1'
_TERM = (r'(?:GENE[ _-]+DETECTED[ _-]+IN|GENE[ _-]+ENRICHED[ _-]+IN|'
         r'PIP|PP[._ ]?H[34](?:[.]abf)?|posterior inclusion probability|'
         r'credible sets?|NES|normalized enrichment score|log2err|'
         r'IBA|ISS|IEA|TAS|one[ _-]+(?:vs|versus)[ _-]+rest|'
         r'gene detection|expression detection|expression enrichment)')
_KNOWN = re.compile(r'\b' + _TERM + r'\b', re.I)
_EXPLICIT_DEFINITION = re.compile(r'\b(?:explain|define|clarify)\b|\b(?:help me understand|how (?:do|should) I interpret|meaning of)\b|\bwhat (?:does|do)\b.+\bmean\b', re.I)
_SIMPLE_DEFINITION = re.compile(r'^\s*(?:please\s+)?what\s+(?:is|are)\s+(?:a\s+|the\s+)?' + _TERM + r'(?:\s+(?:statistic|score|value|term))?(?:\s+(?:in|for)\s+(?:this |the |my )?(?:graph|result|analysis|fine[ -]mapping|colocalization))?\s*[?.!]*\s*$', re.I)
_RETRIEVAL_ACTION = re.compile(r'\b(?:find|retrieve|search|fetch|query|list|count|rank|identify|calculate|compute|estimate|filter|select|look up|look for)\b|\bshow\s+(?:me\s+)?(?:the\s+)?(?:genes|variants|donors|samples|records|pathways|associations|signals)\b|\bwhich\s+(?:genes|variants|donors|samples|records|pathways|associations|signals)\b|\bhow\s+many\b', re.I)
_COMPARE_DEFINITIONS = re.compile(r'^\s*(?:please\s+)?(?:compare|explain the difference between|what is the difference between)\s+(?:the meanings of\s+)?' + _TERM + r'\s+(?:and|versus|vs[.]?)\s+' + _TERM + r'\s*[?.!]*\s*$', re.I)
_MIXED_QUESTION = re.compile(r'(?:\band\b|\bthen\b|;|\?)\s*(?:what is|what are|is |are |does |do )', re.I)


def definition_only_question(value):
    if not isinstance(value, str) or not value.strip() or not _KNOWN.search(value):
        return False
    if _COMPARE_DEFINITIONS.fullmatch(value):
        return True
    if _RETRIEVAL_ACTION.search(value) or re.search(r'\bcompare\b', value, re.I) or _MIXED_QUESTION.search(value):
        return False
    return bool(_EXPLICIT_DEFINITION.search(value) or _SIMPLE_DEFINITION.fullmatch(value))


def definition_only_plan(plan):
    if not isinstance(plan, dict):
        return False
    interpreted = plan.get('interpreted_question')
    original = plan.get('original_question')
    trace = plan.get('revision_trace')
    if isinstance(trace, dict) and trace:
        instruction = trace.get('instruction')
        # An added retrieval request cannot disappear into a definition-only
        # interpretation. Explicit definition comparisons are the exception.
        if isinstance(instruction, str) and _RETRIEVAL_ACTION.search(instruction) and not definition_only_question(instruction):
            return False
        return definition_only_question(interpreted or instruction)
    # A legacy planner may rewrite a pure definition into an unnecessary KG
    # lookup. The original root request remains authoritative in that case.
    return definition_only_question(original if isinstance(original, str) and original.strip() else interpreted)
