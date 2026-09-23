"""Deterministic outcome wording; retrieval failure is never biological absence."""

def aggregate_outcome_status(steps):
    """Only successful, untruncated checks can make an aggregate complete.

    Explicit failed/blocked/unavailable outcomes are incomplete even when other
    checks succeeded. Unknown or absent status is also incomplete, never a
    default success. The original per-step outcomes remain unchanged.
    """
    steps = list(steps)
    states = [step.get('status') if isinstance(step, dict) else None for step in steps]
    truncated = any(bool(step.get('truncated')) for step in steps if isinstance(step, dict))
    failed_states = {'failed', 'blocked', 'unavailable', 'cancelled', 'interrupted', 'timeout', 'timed_out', 'error'}
    failed_checks = sum(state in failed_states or bool(step.get('error'))
                        for state, step in zip(states, steps) if isinstance(step, dict))
    incomplete = (not steps or truncated or failed_checks
                  or any(state not in {'complete', 'empty'} for state in states))
    return {
        'completeness': 'partial' if incomplete else 'empty' if all(state == 'empty' for state in states) else 'complete',
        'truncated': truncated,
        'failed_checks': failed_checks,
        'incomplete_checks': sum(not isinstance(step, dict) or state not in {'complete', 'empty'}
                                 or bool(step.get('error')) or bool(step.get('truncated'))
                                 for state, step in zip(states, steps)),
    }


def executed_without_records(step):
    execution=step.get('retrieval_execution') or {}
    return (step.get('status') in {'complete','empty','partial'} and bool(step.get('queries'))
        and execution.get('completed') is True and execution.get('cursor_exhausted') is True
        and not step.get('truncated') and not step.get('error')
        and step.get('execution_status')!='skipped_empty_dependency'
        and not any(step.get(k) for k in ('nodes','edges','rows','functional_metadata')))


def outcome_message(evidence):
    steps = list(evidence.values()) if isinstance(evidence, dict) else list(evidence)
    primary = [s for s in steps if s.get('purpose') != 'context'] or steps
    usable = [s for s in primary if s.get('status') in ('complete', 'partial')
              and any(s.get(k) for k in ('nodes', 'edges', 'rows'))]
    if usable:
        return None
    if primary and any(s.get('status')=='partial' for s in primary) and all(executed_without_records(s) for s in primary):
        from .answer_blocks import text
        return '\n\n'.join(text(s.get('title') or s.get('question') or 'Requested check')
            + ': the executed query returned no matching records within its recorded filters. '
            + 'The query was bounded or its requested coverage remains incomplete; this is not an exhaustive absence claim or a retrieval failure. '
            + ('['+s['evidence_id']+']' if s.get('evidence_id') else '') for s in primary)
    failed = any(s.get('status') not in ('complete', 'empty') for s in primary)
    if failed or not primary:
        return ('I couldn’t retrieve the graph evidence needed to answer this question. '
                'This is a retrieval failure, so it does not establish whether the biological relationship is present or absent. '
                'The step outcomes below identify what could not be checked.')
    if any(s.get('execution_status') == 'skipped_empty_dependency' or any(str(r).startswith('empty_dependency:') for v in s.get('validation', []) for r in v.get('reasons', [])) for s in primary):
        return ('The required input returned no matching records. Dependent checks were not executed; '
                'this does not establish zero matches for those independent evidence categories.')
    from .evidence_coverage import complete_empty_message
    checked_absence = complete_empty_message(steps)
    if checked_absence:
        return checked_absence
    return ('No matching records were found in the checked graph for the requested entities and filters. '
            'This describes the checked dataset and scope; it does not establish biological absence. '
            'Inspect the executed query and sources below before drawing a broader conclusion.')



QUERY_READINESS_VERSION = 'query-executed-plan-v5-scoped-empty-outcomes'
PARTIAL_INDEPENDENT_POLICY = 'partial_independent_v1'
TERMINAL_QUERY_STATUSES = frozenset({'complete', 'empty', 'partial', 'failed', 'blocked', 'unavailable'})


def required_query_steps(plan):
    """Primary checks and their transitive inputs must be checked before review."""
    steps = plan.get('steps') or []
    by_id = {step['id']: step for step in steps}
    required = {step['id'] for step in steps if step.get('purpose') != 'context'}
    pending = list(required)
    while pending:
        for dependency in by_id.get(pending.pop(), {}).get('depends_on', []):
            if dependency not in required:
                required.add(dependency)
                pending.append(dependency)
    return [step for step in steps if step['id'] in required]


def checked_query_result(step, result, verified):
    """A zero match is valid evidence; a successful HTTP envelope is insufficient."""
    if not isinstance(result, dict) or result.get('status') not in ('complete', 'empty', 'partial'):
        return False
    if result.get('truncated') is not False or result.get('error'):
        return False
    if result.get('status') == 'partial' and step.get('complete') is not False:
        partial_inputs = {dependency for dependency in step.get('depends_on', [])
                          if (verified.get(dependency) or {}).get('status') == 'partial'}
        bounded = result.get('bounded_dependency_step_ids')
        if (not partial_inputs or not isinstance(bounded, list) or not all(isinstance(value, str) for value in bounded)
                or set(bounded) != partial_inputs
                or not all((verified[dependency].get('requested_scope') or {}).get('complete') is False
                           or verified[dependency].get('bounded_dependency_step_ids') for dependency in partial_inputs)):
            return False
    checks = result.get('validation') or []
    if not checks or checks[-1].get('valid') is not True:
        return False
    if any(dependency not in verified for dependency in step.get('depends_on', [])):
        return False
    if step.get('operation'):
        from .composable_planning import verify_derivation
        return verify_derivation(step, result, verified)
    if any(isinstance(query, dict) and isinstance(query.get('cypher'), str) and query['cypher'].strip()
           for query in result.get('queries') or []):
        execution = result.get('retrieval_execution') or {}
        return execution.get('completed') is True and execution.get('cursor_exhausted') is True
    # A verified empty input closes a dependent query without broadening it to
    # unrelated entities. The adapter records this exact, deterministic reason.
    reasons = checks[-1].get('reasons') or []
    for dependency in step.get('depends_on', []):
        parent = verified.get(dependency) or {}
        if ('empty_dependency:' + dependency in reasons and result.get('status') == 'empty'
                and parent.get('status') == 'empty' and not any(parent.get(key) for key in ('nodes', 'edges', 'rows'))
                and not any(result.get(key) for key in ('nodes', 'edges', 'rows'))):
            return True
    return False


def _nonempty_primary_result(step, result):
    """Actual evidence from a checked primary, never an empty context wrapper.

    A legitimate scalar row (including count=0) is an answer to its checked
    question. An outcome explicitly marked empty cannot supply the positive
    admission condition for a partially failed investigation.
    """
    if step.get('purpose') == 'context' or result.get('status') == 'empty':
        return False
    if result.get('nodes') or result.get('edges'):
        return True
    from .semantic_registry import meaningful_row
    return any(meaningful_row(row) for row in result.get('rows') or [])


def query_readiness(plan, preview):
    """Legacy plans require every check; opted-in plans can disclose partial work.

    Partial readiness only opens after all required outcomes are terminal, at
    least one primary result is verified and meaningful, and the normal scope
    and resource guards pass. Failed/truncated steps remain blocked, including
    every dependent check whose inputs were not verified. Callers must retain
    the exact checked snapshot, label missing categories and never rerun failed
    checks implicitly after confirmation.
    """
    if plan.get('plan_mode') in {'literature_only', 'session_summary'} and not plan.get('steps'):
        ready = not plan.get('clarification') and (preview or {}).get('preparation_complete') is True
        return {'version': QUERY_READINESS_VERSION, 'ready': ready, 'partial_ready': False,
                'full_coverage': False, 'all_required_finished': True, 'required_step_ids': [],
                'verified_step_ids': [], 'blocked_step_ids': [], 'no_match_step_ids': [],
                'derived_empty_step_ids': [], 'retained_failed_step_ids': [], 'nonempty_primary_step_ids': [],
                'graph_status': 'not_requested', 'plan_mode': plan['plan_mode']}
    required = required_query_steps(plan)
    outcomes = {step.get('step_id'): step for step in ((preview or {}).get('evidence') or {}).get('steps', [])}
    verified = {}
    for step in required:
        result = outcomes.get(step['id'])
        if checked_query_result(step, result, verified):
            verified[step['id']] = result
    required_ids = [step['id'] for step in required]
    common_guard = (bool(required_ids) and not plan.get('clarification')
                    and (preview or {}).get('preparation_complete') is True
                    and not (preview or {}).get('query_resource_limit_exceeded'))
    full_coverage = bool(common_guard and len(verified) == len(required))
    all_finished = bool(required_ids) and all(
        isinstance(outcomes.get(step_id), dict)
        and outcomes[step_id].get('status') in TERMINAL_QUERY_STATUSES
        for step_id in required_ids)
    nonempty_primary_ids = [step['id'] for step in required
                            if step['id'] in verified and _nonempty_primary_result(step, verified[step['id']])]
    retained_failure_ids = [step_id for step_id in required_ids if step_id not in verified
                            and ((outcomes.get(step_id) or {}).get('status') in {'failed', 'blocked', 'unavailable'}
                                 or ((outcomes.get(step_id) or {}).get('status') == 'partial'
                                     and (outcomes.get(step_id) or {}).get('truncated') is True))]
    # Retain terminal truncations for audit, but exclude their unverified
    # records from synthesis. They never satisfy dependent input verification.
    blocked_are_failures = len(retained_failure_ids) == len(required_ids) - len(verified)
    partial_ready = bool(not full_coverage and common_guard and all_finished and (nonempty_primary_ids or (plan.get('literature_intent') or {}).get('reason') == 'explicit_request')
                         and blocked_are_failures
                         and plan.get('retrieval_policy') == PARTIAL_INDEPENDENT_POLICY)
    return {'version': QUERY_READINESS_VERSION, 'ready': full_coverage or partial_ready,
            'partial_ready': partial_ready, 'full_coverage': full_coverage,
            'all_required_finished': all_finished,
            'nonempty_primary_step_ids': nonempty_primary_ids,
            'retained_failed_step_ids': retained_failure_ids if partial_ready else [],
            'required_step_ids': required_ids, 'verified_step_ids': list(verified),
            'blocked_step_ids': [step_id for step_id in required_ids if step_id not in verified],
            'no_match_step_ids': [step_id for step_id, result in verified.items() if result.get('status') == 'empty' and result.get('queries')],
            'derived_empty_step_ids': [step_id for step_id, result in verified.items() if not result.get('queries')]}


def confirmation_eligible(plan, preview):
    return query_readiness(plan, preview)['ready']


def synthesis_evidence(evidence):
    """Keep failed check identities, never their unverified biological records."""
    from copy import deepcopy
    result = {}
    for key, step in evidence.items():
        checks = step.get('validation') or []
        usable_partial = (step.get('status') == 'partial'
                          and (step.get('retrieval_execution') or {}).get('completed') is True
                          and bool(checks) and checks[-1].get('valid') is True)
        if (step.get('truncated') and not usable_partial) or step.get('status') in {'failed', 'blocked', 'unavailable'}:
            result[key] = {field: deepcopy(step[field]) for field in
                ('step_id', 'evidence_id', 'question', 'title', 'purpose', 'graph_version', 'requested_scope', 'execution_status', 'retrieval_execution') if field in step}
            result[key].update(status='unavailable', truncated=bool(step.get('truncated')),
                               nodes=[], edges=[], rows=[],
                               error={'category': 'retrieval_limit' if step.get('truncated') else 'check_unavailable'})
        else:
            result[key] = step
    return result
