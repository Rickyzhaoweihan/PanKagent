"""Deterministic presentation/policy; no scientific constraints are shortened."""
import re
from copy import deepcopy


def literature_only_revision(instruction):
    return bool(re.fullmatch(r'(?:please\s+)?(?:enable|disable|add|include|use|remove|skip|exclude)\s+(?:the\s+)?(?:literature|papers|publications|literature evidence|literature search)(?:\s+please)?[.!]?', instruction.strip(), re.I))


def enable_literature(plan):
    # Default enrichment must not overwrite a confirmed explicit preference.
    if (plan.get('literature_intent') or {}).get('reason') in {'explicit_opt_out', 'explicit_request', 'inherited_explicit'}:
        return plan
    return {**plan, 'literature': True, 'literature_intent': {'included': True,
        'reason': 'always_enabled', 'policy_version': 'always-enabled-v1',
        'summary': 'Literature evidence will be searched after confirmation to help interpret the graph findings.'}}


def expand_compact_plan(plan):
    result=deepcopy(plan)
    for step in result.get('steps',[]):
        if not step.get('question'):
            if len(result['steps'])!=1 or not result.get('interpreted_question'):
                raise ValueError('missing_step_scope')
            step['question']=result['interpreted_question']
        title = result.get('interpreted_question') if len(result['steps']) == 1 else step['question']
        # Keep biological wording in the existing title, without schema tokens.
        title = re.sub(r'\s*\([A-Z][A-Z_]{3,}\)', '', title or step['question'])
        step.setdefault('title', title)
        step.setdefault('rationale','Check the recorded evidence, study context and supporting sources.')
    return enable_literature(result)


def literature_request_plan(question, history=()):
    """Explicit literature-only requests require no invented graph investigation.

    Follow-up text is passed unchanged alongside history. It does not license
    the application to invent an entity, model, assay or new biological claim.
    """
    literature = re.search(r'\b(?:hirn|literature|papers|publications|pubmed)\b', question, re.I)
    graph = re.search(r'\b(?:pankgraph|pan kgraph|knowledge graph|graph|gwas|qtl|colocalization)\b', question, re.I)
    if not literature or graph:
        return None
    from .literature_policy import apply_literature_policy
    plan = apply_literature_policy({'interpreted_question': question, 'steps': [],
        'clarification': None, 'plan_mode': 'literature_only',
        'planning_route': {'kind': 'explicit_literature_only', 'claude_calls': 0},
        'literature_mode_version': 'independent-literature-v1'}, question)
    return plan if plan['literature'] else None


def unsupported_analysis_plan(question):
    if not re.search(r'\b(?:run|perform|execute|compute|calculate)\b[^.!?]{0,100}\bssgsea\b', question, re.I):
        return None
    from .plan_recovery import mark_failure
    return mark_failure({'interpreted_question':question, 'proposal_issue':'unsupported_analysis:ssgsea',
                         'literature':False, 'planning_route':{'kind':'unsupported_analysis','claude_calls':0}})


def genomic_neighborhood_plan(question):
    """A QTL search cannot answer an unbounded spatial variant question."""
    if re.search(r'\b(?:evidence|association|fine[- ]mapping)\b[^?]{0,60}\bfor\s+rs\d+\b',question,re.I) and not re.search(r'\bgene body\b',question,re.I):
        return None  # A named variant already supplies identity; nearby gene is locus context.
    if not re.search(r'\b(?:SNPs?|variants?)\b',question,re.I):
        return None
    if not re.search(r'\b(?:near|nearby|flanking)\b|\b(?:inside|within)\b.{0,40}\bgene body\b',question,re.I):
        return None
    message=('A genomic-neighborhood search is unavailable for this request: a verified variant-coordinate '
        'and gene-boundary mapping in the same reference assembly is required, with an explicit distance '
        'window for nearby variants. QTL membership is not a substitute for physical proximity. '
        'No neighborhood or gene-body search was executed, and no absence of variants is established.')
    return {'interpreted_question':question,'steps':[],'clarification':message,'literature':False,
        'planning_route':{'kind':'genomic_neighborhood_unavailable','claude_calls':0},
        'recovery':{'category':'scope_needs_clarification','title':'Genomic variant scope is not executable',
                    'message':message,'retryable':False,'suggestions':[]}}
