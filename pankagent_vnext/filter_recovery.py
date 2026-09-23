"""Let independently verified checks survive a sibling's filter rejection.

No predicate is removed or authorized here. Rejected checks remain non-executable,
including downstream consumers; the ordinary evidence gate decides whether any
verified primary result is sufficient for a partial answer.
"""
from copy import deepcopy

from .evidence_status import PARTIAL_INDEPENDENT_POLICY


def recover_filter_failures(plan):
    result = deepcopy(plan)
    steps = result.get('steps') or []
    rejected = [s for s in steps if (s.get('entity_resolution') or {}).get('state') == 'needs_clarification']
    if not rejected:
        return result
    for step in rejected:
        resolution = step.get('entity_resolution') or {}
        if (resolution.get('unknown_relations') or not step.get('runtime_binding_issues')
                or any(e.get('state') not in {'resolved', 'literal_predicate'}
                       for e in step.get('resolved_entities', []))):
            return result
    # A usable branch must not transitively depend on any rejected branch.
    usable = set()
    rejected_ids = {s['id'] for s in rejected}
    for step in steps:
        if (step['id'] not in rejected_ids and not step.get('semantic_issues')
                and not step.get('recovery') and set(step.get('depends_on', [])) <= usable):
            usable.add(step['id'])
    if not any(s['id'] in usable and s.get('purpose') != 'context' for s in steps):
        return result
    warnings = []
    for step in rejected:
        errors = step['runtime_binding_issues']
        constraints = step.get('constraints') or []
        indices = set()
        for error in errors:
            parts = error.split(':')
            if len(parts) > 1 and parts[1].isdigit():
                indices.add(int(parts[1]))
        filters = [deepcopy(c) for i, c in enumerate(constraints) if i in indices]
        descriptions = [f"{c.get('relationship_type') or c.get('entity_type') or 'record'}.{c.get('property')} {c.get('operator', '=')} {c.get('value')!r}" for c in filters]
        detail = '; '.join(descriptions) or 'the proposed query filter'
        message = ('Warning: this check could not establish ' + detail + '. '
                   'The proposed filter could not be verified against the request and current graph. '
                   'Other checks provide partial evidence only; they do not resolve this condition '
                   'or establish absence, an exact count, or the requested relationship.')
        if any(c.get('property') in {'gwas_lead_vars', 'qtl_lead_vars'} for c in filters):
            message += (' Naming a variant does not establish that it is the lead variant of the requested signal.')
        warning = {'step_id': step['id'], 'category': 'filter_scope_unavailable',
                   'message': message, 'filters': filters}
        step['filter_warning'] = warning
        warnings.append(warning)
    result['scope_warnings'] = warnings
    result['retrieval_policy'] = PARTIAL_INDEPENDENT_POLICY
    result['clarification'] = None
    result.pop('recovery', None)
    return result
