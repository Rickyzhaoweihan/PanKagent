"""Recognize only the existing popup's exact terminal-revision retry payload."""

RETRY_INSTRUCTION = (
    "Retry the current investigation without changing any entities, filters, "
    "evidence categories, scope, or explicit preferences."
)
SEPARATOR = "\nRequested change: "


def terminal_revision_retry(run, audit, submitted, *, include_context=True):
    """Return audit bindings, never rewrite the stored question or infer intent."""
    if not include_context or not run or not isinstance(submitted, str):
        return None
    audit = audit or {}
    if not audit.get('parent_run_id') or not isinstance(audit.get('original_question'), str) or not audit['original_question'].strip():
        return None
    status = run.get('status')
    plan = run.get('plan') if isinstance(run.get('plan'), dict) else {}
    preview = run.get('preview') if isinstance(run.get('preview'), dict) else {}
    recovery = plan.get('recovery') or preview.get('recovery')
    if status not in {'failed', 'interrupted'} and not (status == 'partial' and isinstance(recovery, dict) and recovery.get('category') and recovery.get('message')):
        return None
    # A failed plan is the authoritative scope to preserve. Missing historical
    # plans are not silently reconstructed from an isolated revision fragment.
    if not isinstance(plan.get('steps'), list) or not plan['steps']:
        return None
    previous = run.get('question')
    if not isinstance(previous, str) or not previous:
        return None
    if submitted == previous:
        instruction, kind = RETRY_INSTRUCTION, 'exact_retry'
    elif submitted.startswith(previous + SEPARATOR):
        instruction = submitted[len(previous + SEPARATOR):]
        if not instruction.strip():
            return None
        kind = 'retry_with_change'
    else:
        return None
    return {
        'original_question': audit['original_question'],
        'parent_run_id': run['run_id'], 'parent_plan_id': run['plan_id'],
        'revision_mode': 'instruction', 'revision_instruction': instruction,
        'revision_index': (audit.get('revision_index') if isinstance(audit.get('revision_index'), int) else 0) + 1,
        'retry_of_run_id': run['run_id'], 'retry_kind': kind,
        'retry_submitted_text': submitted,
        'retry_prior_revision_instruction': audit.get('revision_instruction'),
        'prior_options': {'include_context': run.get('include_context', True)},
        'requested_options': {'include_context': include_context},
    }


def accepted_term_correction(run, submitted):
    """Accept only a stored suggestion submitted by the existing recovery dialog."""
    if not run or run.get('status') not in {'failed', 'interrupted', 'partial'}:
        return None
    recovery = (run.get('plan') or {}).get('recovery') or {}
    if recovery.get('category') != 'term_clarification':
        return None
    prefix = str(run.get('question') or '') + SEPARATOR
    if not submitted.startswith(prefix):
        return None
    instruction = submitted[len(prefix):]
    suggestion = next((s for s in recovery.get('suggestions', [])
                       if isinstance(s, dict) and s.get('instruction') == instruction), None)
    if not suggestion or not isinstance(suggestion.get('recommended_question'), str):
        return None
    question = suggestion['recommended_question'].strip()
    # Repair historical loops only when every appended instruction repeats the
    # exact same corrected question. Never discard a different requested change.
    pieces = question.split(SEPARATOR)
    if len(pieces) > 1 and all(p == 'Use this corrected question: ' + pieces[0] for p in pieces[1:]):
        question = pieces[0]
    if not question or SEPARATOR in question:
        return None
    return {'question': question, 'audit': {
        'original_question': question, 'raw_original_question': run['question'],
        'parent_run_id': run['run_id'], 'parent_plan_id': run['plan_id'],
        'revision_mode': 'replacement_question', 'revision_instruction': instruction,
        'accepted_term_correction': True, 'retry_submitted_text': submitted}}


def terminal_clarification_revision(run, submitted):
    """Route edited clarification instructions through ordinary interpretation."""
    if not run or run.get('status') not in {'failed', 'interrupted'}:
        return None
    plan = run.get('plan') or {}
    if plan.get('steps') or not plan.get('recovery'):
        return None
    prefix = str(run.get('question') or '') + SEPARATOR
    if not submitted.startswith(prefix) or not submitted[len(prefix):].strip():
        return None
    return {'original_question': run['question'], 'parent_run_id': run['run_id'],
        'parent_plan_id': run['plan_id'], 'revision_mode': 'instruction',
        'revision_instruction': submitted[len(prefix):], 'retry_submitted_text': submitted}
