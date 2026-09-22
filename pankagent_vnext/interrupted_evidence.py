"""Preserve retrieved evidence and label unfinished approved checks explicitly."""
from copy import deepcopy


def complete_interrupted_evidence(plan, evidence, error, started_step_ids=()):
    result = deepcopy(evidence or {'steps': [], 'nodes': [], 'edges': [], 'queries': [], 'provenance': []})
    recorded = {step['step_id']: step for step in result.get('steps', []) if isinstance(step, dict) and step.get('step_id')}
    approved = (plan or {}).get('steps') or []
    missing = [step for step in approved if step['id'] not in recorded]
    if not missing:
        return result  # A synthesis failure does not invalidate completed retrieval.
    unknown_started = started_step_ids is None
    started = set(started_step_ids or ())
    added = []
    for index, step in enumerate(approved, 1):
        if step['id'] in recorded:
            continue
        blocked_by = [dep for dep in step.get('depends_on', [])
                      if dep not in recorded or recorded[dep].get('status') not in {'complete', 'empty', 'partial'}]
        if unknown_started:
            state, category = 'interrupted', 'service_restarted'
            message = 'No outcome was recorded before service interruption; whether this check started is unknown.'
        elif step['id'] in started:
            state = 'timed_out' if error.get('category') in {'timeout', 'retrieval_timeout'} else 'interrupted'
            category = error.get('category', 'execution_interrupted')
            message = 'This check exceeded the execution deadline.' if state == 'timed_out' else 'Execution ended before this check finished.'
        elif blocked_by:
            state, category = 'blocked', 'dependency_unavailable'
            message = 'This check could not start because a required earlier check did not finish successfully.'
        else:
            state, category = 'not_attempted', 'execution_stopped'
            message = 'Execution ended before this check was attempted.'
        item = {key: deepcopy(step[key]) for key in ('question', 'purpose', 'category', 'depends_on') if key in step}
        item.update(step_id=step['id'], evidence_id=f'G{index}', status='failed', execution_status=state,
                    attempted=None if unknown_started else step['id'] in started, nodes=[], edges=[], rows=[], queries=[],
                    validation=[{'valid': False, 'reasons': [category]}],
                    error={'category': category, 'message': message})
        if blocked_by and not unknown_started:
            item['blocked_by'] = blocked_by
        recorded[step['id']] = item
        added.append(step['id'])
    # Existing evidence IDs and content stay intact, including legitimate zero results.
    planned_ids = {step['id'] for step in approved}
    result['steps'] = [recorded[step['id']] for step in approved]
    result['steps'].extend(step for step in (evidence or {}).get('steps', []) if step.get('step_id') not in planned_ids)
    result['completeness'] = 'partial'
    retrieval = result.setdefault('retrieval', {})
    retrieval.update(completeness='partial', checks=len(result['steps']),
                     failed_checks=sum(step.get('status') == 'failed' for step in result['steps']),
                     incomplete_step_ids=added)
    return result
