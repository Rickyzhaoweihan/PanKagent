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
