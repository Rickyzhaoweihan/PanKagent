"""Bounded recovery of empty planning output, without blaming the user."""
import asyncio
import inspect

MESSAGE = 'We could not prepare executable search steps. Your question has been retained; you can retry without changing it.'
GENERIC = ('please provide a concrete entity or graph question.',)

def empty_failure(plan):
    return isinstance(plan, dict) and not plan.get('steps') and not plan.get('answer_mode') and (not plan.get('clarification') or str(plan.get('clarification')).lower() in GENERIC)

def mark_failure(plan):
    return {**plan, 'steps': [], 'clarification': MESSAGE, 'recovery': {
        'category': 'planning_failure', 'title': 'We couldn’t prepare this search',
        'message': MESSAGE, 'retryable': True, 'suggestions': []}}

async def recover_empty_plan(gateway, plan, question, history, timeout):
    if not empty_failure(plan):
        return plan
    # Gateways with an internal bounded repair must not recursively repair again.
    if plan.get('proposal_issue'):
        return mark_failure(plan)
    options = {'_repair': True} if '_repair' in inspect.signature(gateway.plan).parameters else {}
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
