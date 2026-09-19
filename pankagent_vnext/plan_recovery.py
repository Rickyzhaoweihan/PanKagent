"""Bounded recovery of empty planning output, without blaming the user."""
import asyncio
import inspect

MESSAGE = 'We could not prepare executable search steps. Your question has been retained; you can retry without changing it.'
GENE_EXCLUSION_MESSAGE = 'Named gene exclusions are not supported yet. Your question has been retained; revise it to remove the exclusion before running the search.'
GENERIC = ('please provide a concrete entity or graph question.',)

def empty_failure(plan):
    return isinstance(plan, dict) and not plan.get('steps') and not plan.get('answer_mode') and (plan.get('proposal_issue') in {'empty_executable_plan','malformed_plan','malformed_step','invalid_plan_dependencies'} or not plan.get('clarification') or str(plan.get('clarification')).lower() in GENERIC)

def mark_failure(plan):
    if str(plan.get('proposal_issue') or '').startswith('unsupported_gene_exclusion:'):
        return {**plan, 'steps': [], 'clarification': GENE_EXCLUSION_MESSAGE, 'recovery': {
            'category': 'planning_failure', 'title': 'Named gene exclusions are not supported',
            'message': GENE_EXCLUSION_MESSAGE, 'retryable': False, 'suggestions': []}}
    return {**plan, 'steps': [], 'clarification': MESSAGE, 'recovery': {
        'category': 'planning_failure', 'title': 'We couldn’t prepare this search',
        'message': MESSAGE, 'retryable': True, 'suggestions': []}}

async def recover_empty_plan(gateway, plan, question, history, timeout, grounding=None):
    if not empty_failure(plan):
        return plan
    # Gateways with an internal bounded repair must not recursively repair again.
    if plan.get('proposal_issue'):
        return mark_failure(plan)
    options = {'_repair': True} if '_repair' in inspect.signature(gateway.plan).parameters else {}
    if grounding is not None and 'grounding' in inspect.signature(gateway.plan).parameters:
        options['grounding'] = grounding
    guidance = {'role':'system','content':'The previous output had no executable steps for the original question. Repair it once, preserving the requested entities and filters. Return concrete checks, or a specific clarification only if the user intent is genuinely ambiguous. Do not ask for a concrete question when one was supplied.'}
    try:
        repaired = await asyncio.wait_for(gateway.plan(question, history + [guidance], **options), timeout)
    except asyncio.CancelledError:
        raise
    except Exception:
        return mark_failure(plan)
    if empty_failure(repaired):
        return mark_failure(repaired)
    return repaired
