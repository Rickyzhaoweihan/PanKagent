"""Bounded recovery of empty planning output, without blaming the user."""
import asyncio
import inspect
import re

MESSAGE = 'We could not prepare executable search steps. Your question has been retained; you can retry without changing it.'
GENE_EXCLUSION_MESSAGE = 'Named gene exclusions are not supported yet. Your question has been retained; revise it to remove the exclusion before running the search.'
GENERIC = ('please provide a concrete entity or graph question.',)

def contains_tool_markup(value):
    if isinstance(value, str):
        compact = re.sub(r'\s+', '', value).lower()
        return bool(re.search(r'<\/?(?:antml:)?(?:parameter|invoke|function_calls|tool_use|interpreted_question)(?:name=|[=>:/]|$)', compact))
    if isinstance(value, dict):
        return any(contains_tool_markup(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_tool_markup(v) for v in value)
    return False


def empty_failure(plan):
    if contains_tool_markup(plan):
        return True
    return isinstance(plan, dict) and not plan.get('steps') and not plan.get('answer_mode') and plan.get('plan_mode') not in {'literature_only', 'session_summary'} and (plan.get('proposal_issue') in {'empty_executable_plan','malformed_plan','malformed_step','invalid_plan_dependencies'} or not plan.get('clarification') or str(plan.get('clarification')).lower() in GENERIC)

def mark_failure(plan):
    if contains_tool_markup(plan):
        plan = {'interpreted_question':'The original question is retained with this run.', 'proposal_issue':'malformed_plan'}
    if plan.get('proposal_issue') == 'unsupported_analysis:ssgsea':
        message = 'ssGSEA execution is not available in this agent. No ssGSEA analysis was run. Effector-gene retrieval can be requested separately.'
        return {**plan, 'steps':[], 'clarification':message, 'recovery':{'category':'planning_failure',
            'title':'ssGSEA execution is not supported', 'message':message, 'retryable':False, 'suggestions':[]}}
    if str(plan.get('proposal_issue') or '').startswith('unsupported_gene_exclusion:'):
        return {**plan, 'steps': [], 'clarification': GENE_EXCLUSION_MESSAGE, 'recovery': {
            'category': 'planning_failure', 'title': 'Named gene exclusions are not supported',
            'message': GENE_EXCLUSION_MESSAGE, 'retryable': False, 'suggestions': []}}
    issue = str(plan.get('proposal_issue') or '')
    if any(part in issue for part in ('path_', 'chain_', 'dependency_', 'final_join')):
        message = ('The requested connected-path condition could not be applied because its roles, '
                   'links, or dependency bindings could not be verified. Every requested filter is retained; '
                   'no relaxed search was executed.')
        question = str(plan.get('interpreted_question') or '').strip()
        recommended = question + ' Preserve every filter and return only complete connected paths.'
        return {**plan, 'steps': [], 'clarification': message, 'recovery': {
            'category': 'planning_failure', 'title': 'The connected path needs a valid plan',
            'message': message, 'retryable': True,
            'suggestions': [{'label': 'Retry the complete path request', 'instruction': recommended}] if question else [],
            'evidence': {'condition': 'connected_path', 'reason': issue}}}
    return {**plan, 'steps': [], 'clarification': MESSAGE, 'recovery': {
        'category': 'planning_failure', 'title': 'We couldn’t prepare this search',
        'message': MESSAGE, 'retryable': True, 'suggestions': []}}

async def recover_empty_plan(gateway, plan, question, history, timeout, grounding=None):
    if not empty_failure(plan):
        return plan
    if contains_tool_markup(plan) and plan.get('proposal_issue'):
        return mark_failure({'interpreted_question':question,'proposal_issue':'malformed_plan'})
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
        return mark_failure({'interpreted_question':question,'proposal_issue':'malformed_plan'} if contains_tool_markup(repaired) else repaired)
    return repaired
