"""Deterministic outcome wording; retrieval failure is never biological absence."""

def outcome_message(evidence):
    steps = list(evidence.values()) if isinstance(evidence, dict) else list(evidence)
    primary = [s for s in steps if s.get('purpose') != 'context'] or steps
    usable = [s for s in primary if s.get('status') in ('complete', 'partial')
              and any(s.get(k) for k in ('nodes', 'edges', 'rows'))]
    if usable:
        return None
    failed = any(s.get('status') not in ('complete', 'empty') for s in primary)
    if failed or not primary:
        return ('I couldn’t retrieve the graph evidence needed to answer this question. '
                'This is a retrieval failure, so it does not establish whether the biological relationship is present or absent. '
                'The step outcomes below identify what could not be checked.')
    from .evidence_coverage import complete_empty_message
    checked_absence = complete_empty_message(steps)
    if checked_absence:
        return checked_absence
    return ('No matching records were found in the checked graph for the requested entities and filters. '
            'This describes the checked dataset and scope; it does not establish biological absence. '
            'Inspect the executed query and sources below before drawing a broader conclusion.')



QUERY_READINESS_VERSION = 'query-executed-plan-v1'


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


def query_readiness(plan, preview):
    required = required_query_steps(plan)
    outcomes = {step.get('step_id'): step for step in ((preview or {}).get('evidence') or {}).get('steps', [])}
    verified = {}
    for step in required:
        result = outcomes.get(step['id'])
        if checked_query_result(step, result, verified):
            verified[step['id']] = result
    required_ids = [step['id'] for step in required]
    ready = (bool(required_ids) and not plan.get('clarification')
             and (preview or {}).get('preparation_complete') is True
             and not (preview or {}).get('query_resource_limit_exceeded') and len(verified) == len(required))
    return {'version': QUERY_READINESS_VERSION, 'ready': ready,
            'required_step_ids': required_ids, 'verified_step_ids': list(verified),
            'blocked_step_ids': [step_id for step_id in required_ids if step_id not in verified],
            'no_match_step_ids': [step_id for step_id, result in verified.items() if result.get('status') == 'empty'],
            'derived_empty_step_ids': [step_id for step_id, result in verified.items() if not result.get('queries')]}


def confirmation_eligible(plan, preview):
    return query_readiness(plan, preview)['ready']
