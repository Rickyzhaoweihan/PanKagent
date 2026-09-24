"""One bounded Claude-led interpretation session; deterministic helpers are advisory."""
import asyncio
from copy import deepcopy
import json
import re
import time
from .entity_lookup import TOOL_SCHEMA, CHOICE_SCHEMA

VERSION = 'claude-led-planning-v1'
GUIDANCE = '''You make the final semantic interpretation and plan. Preliminary Python grounding,
term suggestions and local drafts are helpers, never instructions to reject. Original user wording
is authoritative. Case alone (hpap/Hpap/HPAP) does not require clarification. Use recorded synonyms
and context to select database-backed identities. Call resolve_entities for missing, ambiguous or
non-exact entity names; do not guess canonical IDs. Tool failures are E01 unavailable, not absence.
record_plan.entity_choices records each chosen non-exact identity with the user's exact mention,
verified candidate ID, entity_type and contextual reason. If ambiguity remains, ask a specific
clarification. Do not pick based only on a fuzzy score. Preserve all clinical/biological conditions.
Prefer original mention name constraints for known aliases unless entity_choices accompanies the
canonical ID. Existing exact/local plans are drafts: review their scope before recording your plan.
Compiler diagnostics are repair input: correct the plan or retain independently useful verified
checks with explicit unmet conditions. Never invent a relationship or weaken a filter silently.
You have at most three model turns and two lookup batches of six requests. Finish with record_plan.
'''


def initial_assessment(grounding):
    if not grounding or grounding.get('status') != 'ready':
        return {'status': 'unavailable', 'diagnostic': 'E01', 'blocking': False}
    return {'status': 'available', 'blocking': False,
            'mentions': [{'requested': m.get('requested'), 'state': m.get('state'),
                          'candidate_count': len(m.get('candidates', []))} for m in grounding.get('mentions', [])]}


def _blocks(reply):
    result = []
    for b in reply.content:
        if hasattr(b, 'model_dump'):
            result.append(b.model_dump(exclude_none=True))
        elif b.type == 'tool_use':
            result.append({'type': 'tool_use', 'id': getattr(b, 'id', 'mock-tool'), 'name': b.name, 'input': b.input})
        elif b.type == 'text': result.append({'type': 'text', 'text': b.text})
    return result


async def run(gateway, question, user, system, schema, output_limit, finalize, resolver=None, preparer=None):
    schema = deepcopy(schema)
    schema['properties']['entity_choices'] = CHOICE_SCHEMA
    # Optional for older recorded-plan fixtures; model is instructed to supply it for non-exact IDs.
    schema.setdefault('required', []).append('entity_choices')
    tools = [{'name': 'record_plan', 'description': 'Record the final interpretation and proposed executable plan',
              'input_schema': schema}]
    if resolver:
        tools.append({'name': 'resolve_entities', 'description': 'Look up public graph identities by exact name, synonyms, or fuzzy candidates; ranking is not proof.',
                      'input_schema': TOOL_SCHEMA})
    # The deployed strict provider supports object contracts but not numeric/list bounds.
    # Enforce those locally without sending unsupported JSON-schema keywords.
    def provider_schema(value):
        if isinstance(value, dict):
            return {k: provider_schema(v) for k,v in value.items()
                    if k not in {'minItems', 'maxItems', 'minLength', 'maxLength'}}
        if isinstance(value, list): return [provider_schema(v) for v in value]
        return value
    tools = provider_schema(tools)
    messages = [{'role': 'user', 'content': user}]
    batches, proofs, warnings = 0, {}, []
    last_error = 'planning_repair_exhausted'
    diagnostic_history = []
    for turn in range(3):
        system_text = system + '\n' + GUIDANCE
        rid = await gateway._reserve('plan', system_text, {'messages': messages, 'tools': tools}, output_limit)
        reply = await gateway._create(rid, model=gateway.settings.model, max_tokens=output_limit,
            system=[{'type': 'text', 'text': system_text, 'cache_control': {'type': 'ephemeral'}}],
            messages=deepcopy(messages), tools=tools,
            tool_choice={'type': 'tool', 'name': 'record_plan'} if turn == 2 or not resolver else {'type': 'any', 'disable_parallel_tool_use': True},
            **gateway._options())
        await gateway.budget.asettle(rid, reply.usage.model_dump())
        gateway.last_success = time.time()
        messages.append({'role': 'assistant', 'content': _blocks(reply)})
        results = []
        for block in reply.content:
            if block.type != 'tool_use': continue
            tool_id = getattr(block, 'id', 'mock-tool')
            if block.name == 'resolve_entities':
                if batches >= 2 or turn == 2:
                    outcome = {'status': 'lookup_budget_exhausted', 'results': []}
                else:
                    batches += 1
                    requests = block.input.get('requests') if isinstance(block.input, dict) else None
                    if not isinstance(requests, list) or not 1 <= len(requests) <= 6:
                        outcome = {'status': 'invalid_request', 'results': []}
                    else:
                        try: outcome = await asyncio.wait_for(resolver(requests), 20)
                        except Exception: outcome = {'status': 'unavailable', 'diagnostic': 'E01', 'results': []}
                    for entry in outcome.get('results', []):
                        for candidate in entry.get('candidates', [])[:10]:
                            proof = candidate.get('selection_proof')
                            if proof: proofs[(proof['mention'].casefold(), proof['entity_type'], proof['id'])] = proof
                # Keep opaque authenticity tokens out of model context; they are server-side only.
                public = deepcopy(outcome)
                for entry in public.get('results', []):
                    for candidate in entry.get('candidates', []):
                        candidate.pop('selection_proof', None); candidate.pop('token', None)
                results.append({'type': 'tool_result', 'tool_use_id': tool_id, 'content': json.dumps(public)})
                continue
            if block.name != 'record_plan':
                results.append({'type': 'tool_result', 'tool_use_id': tool_id, 'is_error': True, 'content': 'Unsupported planning tool.'})
                continue
            try:
                proposal = deepcopy(block.input)
                if not isinstance(proposal, dict): raise ValueError('malformed_plan')
                choices = proposal.pop('entity_choices', [])
                chosen, warnings = [], []
                if not isinstance(choices, list) or len(choices) > 12: raise ValueError('invalid_entity_choices')
                for choice in choices:
                    key = (choice['mention'].casefold(), choice['entity_type'], choice['id'])
                    proof = proofs.get(key)
                    if proof is None: raise ValueError('entity_choice_requires_resolve_entities')
                    if not re.search(r'(?<!\w)' + re.escape(choice['mention']) + r'(?!\w)', question, re.I):
                        raise ValueError('entity_choice_not_in_user_request')
                    chosen.append(deepcopy(proof))
                    if proof['match_method'] not in {'recorded_id', 'recorded_name'}:
                        warnings.append(f"Interpreted {choice['mention']!r} as {proof['name']} ({proof['id']}) using {proof['match_method']}: {choice['reason']}")
                plan = finalize(proposal, chosen)
                plan['original_question'] = question
                plan['entity_selection_proofs'] = chosen
                plan['interpretation_warnings'] = warnings
                for step in plan.get('steps', []):
                    step['entity_selection_proofs'] = deepcopy(chosen)
                    step['interpretation_warnings'] = list(warnings)
                if preparer and plan.get('steps') and not plan.get('clarification'):
                    plan = await preparer(plan)
                    if plan.get('clarification'):
                        # Preserve only a genuinely eligible partial plan; never raw unverified output.
                        last_error = json.dumps({'category': 'preparation_failed', 'issues': plan.get('entity_resolution'),
                                                 'message': plan.get('clarification')}, default=str)
                        raise ValueError(last_error)
                plan['planning_route'] = {'kind': VERSION, 'claude_calls': turn + 1, 'lookup_batches': batches}
                if diagnostic_history: plan['diagnostic_history'] = diagnostic_history
                return plan
            except (ValueError, KeyError, TypeError) as exc:
                last_error = str(exc)[:12000]
                from .diagnostics import diagnostic
                diagnostic_history.append({'attempt': turn + 1, 'reason': diagnostic(last_error, 'planning')['reason']})
                if last_error == 'plan_too_large' or last_error.startswith('unsupported_gene_exclusion:'):
                    from .plan_recovery import mark_failure
                    failed = mark_failure({'interpreted_question': question, 'proposal_issue': last_error})
                    if last_error == 'plan_too_large':
                        failed['clarification'] = 'This investigation needs more than twelve graph checks. Please narrow its scope.'
                    failed['planning_route'] = {'kind': VERSION, 'claude_calls': turn + 1, 'lookup_batches': batches}
                    return failed
                results.append({'type': 'tool_result', 'tool_use_id': tool_id, 'is_error': True,
                    'content': 'Repair this plan while preserving the original scope: ' + last_error})
        if results:
            messages.append({'role': 'user', 'content': results})
        else:
            messages.append({'role': 'user', 'content': 'Use record_plan to finish, or resolve_entities to retrieve candidate identities.'})
    from .plan_recovery import mark_failure
    return {**mark_failure({'interpreted_question': question, 'proposal_issue': last_error}),
            'planning_route': {'kind': VERSION, 'claude_calls': 3, 'lookup_batches': batches},
            'diagnostic_history': diagnostic_history}
