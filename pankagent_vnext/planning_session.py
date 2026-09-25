"""One bounded Claude-led interpretation session; deterministic helpers are advisory."""
import asyncio
from copy import deepcopy
import json
import re
import time
from .entity_lookup import TOOL_SCHEMA, CHOICE_SCHEMA, mention_in_request
from .agent_schemas import module as schema_module, run_context

VERSION = 'claude-led-planning-v2'
GUIDANCE = '''You make the final semantic interpretation and plan. Preliminary Python grounding,
term suggestions and local drafts are helpers, never instructions to reject. The current user request and explicit revisions
are authoritative. Discard irrelevant helper suggestions; facts about the database do not add requested scope. Case alone (hpap/Hpap/HPAP) does not require clarification. Use recorded synonyms
and context to select database-backed identities. Call resolve_entities for missing, ambiguous or
non-exact entity names; do not guess canonical IDs. Tool failures are E01 unavailable, not absence.
record_plan.entity_choices records each chosen non-exact identity with the user's exact mention,
verified candidate ID, entity_type and contextual reason. If ambiguity remains, ask a specific
clarification. Do not pick based only on a fuzzy score. Preserve all currently requested clinical/biological conditions.
Prefer original mention name constraints for known aliases unless entity_choices accompanies the
canonical ID. Existing exact/local plans are drafts: review their scope before recording your plan.
Compiler diagnostics are repair input: correct the plan or retain independently useful verified
checks with explicit unmet conditions. Never invent a relationship or weaken a filter silently.
Verified preliminary candidates are already available for entity_choices; do not repeat a lookup
solely to obtain a proof. Use lookup for missing evidence or ambiguity. You have two lookup batches
of six requests and three plan proposals. Lookup turns do not consume a plan repair opportunity.
Use inspect_schema for field ownership and interpretation, and resolve_property_values for recorded categorical codes. These share the two lookup-batch budget.
Record applied/discarded advice in optional advisory_decisions using its supplied rule_id; these audit notes do not alter execution.
D02 is your semantic decision within record_plan: select relevant entities, relationships and optional context. A disease-definition request needs disease identity, description and provenance, not a donor inventory.
Submit record_plan to prepare the tasks; preparation diagnostics return to this same session.
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


def helper_payload(outcome, tool):
    """Describe provenance without turning retrieved guidance into intent."""
    facts = deepcopy(outcome)
    advice = {}
    for key in ('interpretation_rules', 'query_patterns', 'repair_guidance'):
        if key in facts:
            advice[key] = facts.pop(key)
    if tool == 'inspect_schema':
        advice['interpretation_entries'] = [item for item in facts.get('items', [])
                                            if item.get('reference', '').startswith('interpretation.')]
        facts['items'] = [item for item in facts.get('items', [])
                          if not item.get('reference', '').startswith('interpretation.')]
    diagnostics = []
    if outcome.get('status') not in {'complete', 'ready', 'resolved', 'candidates'}:
        diagnostics.append({'rule_id': tool, 'status': outcome.get('status'),
                            'reason': outcome.get('diagnostic'), 'blocking': False})
    return {'verified_facts': facts, 'advisory_suggestions': advice, 'diagnostics': diagnostics,
            'advisory_rule_ids': [tool + '.' + key for key, value in advice.items() if value],
            'authority': 'Candidate matches and recorded schema facts do not authorize query scope. '
                         'Choose relevant suggestions or discard them; execution checks still apply.'}


def suggestion_decisions(draft, plan):
    if not draft:
        return []
    # Audit the observable disposition of each draft task, not model reasoning.
    def signature(step):
        return json.dumps({k: step.get(k, []) for k in ('relation_types', 'constraints')}, sort_keys=True)
    selected = {signature(step) for step in plan.get('steps', [])}
    return [{'rule_id': 'M04.local_draft', 'suggestion_step_id': step.get('id'),
             'disposition': 'applied' if signature(step) in selected else 'discarded_or_replaced',
             'basis': 'record_plan task signature before preparation'} for step in draft.get('steps', [])]


async def run(gateway, question, user, system, schema, output_limit, finalize, resolver=None, preparer=None,
              initial_proofs=None):
    schema = deepcopy(schema)
    schema['properties']['entity_choices'] = CHOICE_SCHEMA
    schema['properties']['advisory_decisions'] = {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'properties': {
            'rule_id': {'type': 'string'}, 'disposition': {'type': 'string', 'enum': ['applied', 'discarded']}},
        'required': ['rule_id', 'disposition']}}
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
    from .schema_tools import SCHEMA_TOOL, VALUES_TOOL
    tools.append(SCHEMA_TOOL)
    graph = getattr(resolver, '__self__', None)
    if graph is not None:
        tools.append(VALUES_TOOL)
    tools = provider_schema(tools)
    from .request_context import current, AUTHORITY, attach
    context = current(question)
    user_input = json.loads(user)
    advisory_ids = {'M04.' + key for key, value in user_input.get('advisory_suggestions', {}).items() if value}
    if user_input.get('local_draft'): advisory_ids.add('M04.local_draft')
    user_input.update(request_context=context, request_authority=AUTHORITY, advisory_rule_ids=sorted(advisory_ids))
    local_draft = user_input.get('local_draft') or user_input.get('advisory_suggestions', {}).get('local_draft')
    messages = [{'role': 'user', 'content': json.dumps(user_input, ensure_ascii=False)}]
    limits = schema_module('validation_repair')['limits']
    batches, proposals, proofs, warnings = 0, 0, {}, []
    for proof in initial_proofs or []:
        proofs[(proof['mention'].casefold(), proof['entity_type'], proof['id'])] = deepcopy(proof)
    last_error = 'planning_repair_exhausted'
    diagnostic_history = []
    max_calls = min(limits['total_claude_calls'] - limits['execution_repairs'],
                    limits['lookup_batches'] + limits['planning_proposals'])
    last_partial = None
    for turn in range(max_calls):
        system_text = system + '\n' + GUIDANCE + '\n' + AUTHORITY
        rid = await gateway._reserve('plan', system_text, {'messages': messages, 'tools': tools}, output_limit)
        reply = await gateway._create(rid, model=gateway.settings.model, max_tokens=output_limit,
            system=[{'type': 'text', 'text': system_text, 'cache_control': {'type': 'ephemeral'}}],
            messages=deepcopy(messages), tools=tools,
            tool_choice={'type': 'tool', 'name': 'record_plan'} if batches >= limits['lookup_batches'] or turn == max_calls - 1 or not resolver else {'type': 'any', 'disable_parallel_tool_use': True},
            **gateway._options())
        await gateway.budget.asettle(rid, reply.usage.model_dump())
        gateway.last_success = time.time()
        messages.append({'role': 'assistant', 'content': _blocks(reply)})
        results = []
        for block in reply.content:
            if block.type != 'tool_use': continue
            tool_id = getattr(block, 'id', 'mock-tool')
            if block.name in {'inspect_schema', 'resolve_property_values'}:
                from .schema_tools import inspect_schema, resolve_property_values
                if batches >= limits['lookup_batches']:
                    outcome = {'status': 'lookup_budget_exhausted'}
                else:
                    batches += 1
                    try:
                        outcome = (inspect_schema(block.input.get('references')) if block.name == 'inspect_schema'
                                   else await asyncio.wait_for(resolve_property_values(graph, block.input.get('reference',''), block.input.get('text','')), 10))
                    except Exception:
                        outcome = {'status': 'unavailable', 'diagnostic': 'E01'}
                public = helper_payload(outcome, block.name)
                advisory_ids.update(public['advisory_rule_ids'])
                results.append({'type': 'tool_result', 'tool_use_id': tool_id, 'content': json.dumps(public)})
                continue
            if block.name == 'resolve_entities':
                if batches >= limits['lookup_batches'] or turn == max_calls - 1:
                    outcome = {'status': 'lookup_budget_exhausted', 'results': []}
                else:
                    batches += 1
                    requests = block.input.get('requests') if isinstance(block.input, dict) else None
                    if not isinstance(requests, list) or not 1 <= len(requests) <= limits['requests_per_batch']:
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
                results.append({'type': 'tool_result', 'tool_use_id': tool_id, 'content': json.dumps(helper_payload(public, block.name))})
                continue
            if block.name != 'record_plan':
                results.append({'type': 'tool_result', 'tool_use_id': tool_id, 'is_error': True, 'content': 'Unsupported planning tool.'})
                continue
            try:
                proposals += 1
                proposal = deepcopy(block.input)
                if not isinstance(proposal, dict): raise ValueError('malformed_plan')
                advice_decisions = proposal.pop('advisory_decisions', [])
                advice_decisions = [dict(item, basis='Claude record_plan decision') for item in advice_decisions[:40]
                    if isinstance(item, dict) and item.get('rule_id') in advisory_ids
                    and item.get('disposition') in {'applied', 'discarded'}] if isinstance(advice_decisions, list) else []
                choices = proposal.pop('entity_choices', [])
                chosen, warnings = [], []
                if not isinstance(choices, list) or len(choices) > 12: raise ValueError('invalid_entity_choices')
                for choice in choices:
                    key = (choice['mention'].casefold(), choice['entity_type'], choice['id'])
                    proof = proofs.get(key)
                    if proof is None:
                        # A model may return the canonical name rather than the
                        # user's inflected mention. Reuse the same verified ID
                        # only when its original lookup mention belongs here.
                        matches = [p for p in proofs.values() if p['entity_type'] == choice['entity_type']
                                   and p['id'] == choice['id'] and mention_in_request(p['mention'], question)]
                        if matches:
                            # Several request aliases may prove the same typed
                            # ID (for example abbreviation plus expanded name).
                            # That is repeated evidence, not an identity collision.
                            proof = max(matches, key=lambda p: len(p['mention']))
                    if proof is None:
                        raise ValueError('entity_choice_requires_resolve_entities:' + json.dumps({
                            'choice': choice, 'instruction': 'Use an ID returned by resolve_entities or the verified preliminary candidates. Omit unneeded identity choices; preserve requested filters.'}))
                    if not mention_in_request(proof['mention'], question):
                        raise ValueError('entity_choice_not_in_user_request:' + json.dumps({
                            'mention': proof['mention'], 'instruction': 'Look up the exact phrase used in the original question, including its wording, then select that returned candidate.'}))
                    chosen.append(deepcopy(proof))
                    if proof['match_method'] not in {'recorded_id', 'recorded_name'}:
                        warnings.append(f"Interpreted {choice['mention']!r} as {proof['name']} ({proof['id']}) using {proof['match_method']}: {choice['reason']}")
                plan = attach(finalize(proposal, chosen), context)
                plan['tool_suggestion_decisions'] = advice_decisions + suggestion_decisions(local_draft, plan)
                plan['original_question'] = question
                chosen = list({(p['mention'].casefold(), p['entity_type'], p['id']): p
                               for p in [*plan.get('entity_selection_proofs', []), *chosen]}.values())
                plan['entity_selection_proofs'] = chosen
                plan['interpretation_warnings'] = list(dict.fromkeys([*plan.get('interpretation_warnings', []), *warnings]))
                plan['run_context'] = run_context(gateway.settings)
                for step in plan.get('steps', []):
                    step['entity_selection_proofs'] = deepcopy(chosen)
                    step['interpretation_warnings'] = list(plan['interpretation_warnings'])
                if preparer and plan.get('steps') and not plan.get('clarification'):
                    plan = await preparer(plan)
                    invalid = [s for s in plan.get('steps', []) if s.get('semantic_issues')
                               or s.get('runtime_binding_issues') or s.get('filter_warning')]
                    if invalid:
                        last_partial = deepcopy(plan)
                        raise ValueError(json.dumps({'category': 'preparation_failed', 'tasks': [
                            {'step_id': s['id'], 'constraints': s.get('constraints'),
                             'reasons': s.get('runtime_binding_issues') or s.get('semantic_issues')}
                            for s in invalid], 'instruction': 'Repair only the affected tasks. Use canonical id/name bindings for verified identities; preserve current requested conditions, without restoring superseded filters.'}))
                    if plan.get('clarification'):
                        # Preserve only a genuinely eligible partial plan; never raw unverified output.
                        last_error = json.dumps({'category': 'preparation_failed', 'issues': plan.get('entity_resolution'),
                                                 'message': plan.get('clarification')}, default=str)
                        raise ValueError(last_error)
                plan['planning_route'] = {'kind': VERSION, 'claude_calls': turn + 1, 'lookup_batches': batches,
                                          'planning_proposals': proposals, 'execution_repairs': 0}
                if diagnostic_history: plan['diagnostic_history'] = diagnostic_history
                return plan
            except (ValueError, KeyError, TypeError) as exc:
                last_error = str(exc)[:12000]
                from .diagnostics import diagnostic
                diagnostic_history.append({'attempt': turn + 1, 'reason': diagnostic(last_error, 'planning')['reason']})
                if getattr(exc, 'partial_plan', None):
                    last_partial = exc.partial_plan
                if last_error == 'plan_too_large' or last_error.startswith('unsupported_gene_exclusion:'):
                    from .plan_recovery import mark_failure
                    failed = mark_failure({'interpreted_question': question, 'proposal_issue': last_error})
                    if last_error == 'plan_too_large':
                        failed['clarification'] = 'This investigation needs more than twelve graph checks. Please narrow its scope.'
                    failed['planning_route'] = {'kind': VERSION, 'claude_calls': turn + 1, 'lookup_batches': batches}
                    return failed
                results.append({'type': 'tool_result', 'tool_use_id': tool_id, 'is_error': True,
                    'content': 'Repair this plan while preserving the current requested scope: ' + last_error})
        if results:
            messages.append({'role': 'user', 'content': results})
        else:
            messages.append({'role': 'user', 'content': 'Use record_plan to finish, or resolve_entities to retrieve candidate identities.'})
        if proposals >= limits['planning_proposals']:
            break
    from .plan_recovery import mark_failure
    if last_partial and preparer:
        candidate = await preparer(last_partial)
        if candidate.get('steps') and not candidate.get('clarification'):
            candidate.update(original_question=question, run_context=run_context(gateway.settings),
                             diagnostic_history=diagnostic_history,
                             planning_route={'kind': VERSION, 'claude_calls': turn + 1,
                                             'lookup_batches': batches, 'planning_proposals': proposals,
                                             'outcome': 'verified_independent_subset', 'execution_repairs': 0})
            return candidate
    return {**mark_failure({'interpreted_question': question, 'proposal_issue': last_error}),
            'run_context': run_context(gateway.settings),
            'planning_route': {'kind': VERSION, 'claude_calls': turn + 1, 'lookup_batches': batches,
                              'planning_proposals': proposals, 'execution_repairs': 0},
            'diagnostic_history': diagnostic_history}
