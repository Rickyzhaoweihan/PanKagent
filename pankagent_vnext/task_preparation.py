"""Preparation facts for the supervisor; never silently relax requested scope."""
from copy import deepcopy
from .planning_scope import _mentions, _compatible, _identity_present


class PreparationIssue(ValueError):
    def __init__(self, reason, partial_plan=None):
        super().__init__(reason)
        self.partial_plan = partial_plan


def bind_unique_requested_identities(question, grounding, plan):
    """Fill an omitted identity only when role and requested identity are unique.

    Existing identity predicates, negations, ambiguity, and repeated path roles
    are never overwritten. Normal authorization and graph checks still follow.
    """
    result = deepcopy(plan)
    if not grounding or grounding.get('status') != 'ready':
        return result
    words, mentions, _ = _mentions(question, grounding)
    for mention, candidate, forms, spans in mentions:
        # Repair only affirmative anchors. Unsupported negative scope remains a
        # diagnostic for the supervisor, never a new positive predicate.
        if any(set(words[max(0, start - 6):start]) &
               {'not', 'without', 'exclude', 'excluding', 'except', 'neither', 'nor'}
               or 'excluded' in words[end:end + 3] for start, end in spans):
            continue
        kind = candidate['entity_type']
        # Tissue/disease scopes can denote annotations or assay context. Only
        # explicitly requested, unique gene/variant anchors use this repair.
        if kind not in {'Gene', 'variants'} or len({c['id'] for _, c, _, _ in mentions if c['entity_type'] == kind}) != 1:
            continue
        for step in result.get('steps', []):
            if step.get('depends_on') or step.get('operation') or step.get('purpose') == 'context':
                continue
            if not any(_compatible(kind, relation) for relation in step.get('relation_types', [])):
                continue
            # A variant is not a node endpoint of a gene-to-disease coloc edge.
            if kind == 'variants' and 'SIGNAL_COLOC_WITH' in step.get('relation_types', []):
                continue
            if _identity_present(step, candidate, forms):
                continue
            if any(c.get('entity_type') == kind and str(c.get('property', '')).split('.')[-1] in {'id', 'name'}
                   for c in step.get('constraints', [])):
                continue
            constraint = {'entity_type': kind, 'property': 'id', 'operator': '=', 'value': candidate['id']}
            path = step.get('path_spec')
            if path:
                roles = [n.get('role') for n in path.get('nodes', []) if kind in n.get('entity_types', []) or n.get('entity_type') == kind or n.get('label') == kind]
                if len(roles) != 1 or not roles[0]:
                    continue
                constraint['owner_role'] = roles[0]
            step.setdefault('constraints', []).append(constraint)
            step.setdefault('preparation_trace', []).append({'rule': 'unique_requested_identity_binding',
                'mention': mention['requested'], 'entity_type': kind, 'id': candidate['id']})
    return result


def independent_subset(plan, reason):
    """Identify a failed task and descendants, retaining failures as coverage metadata.

    Global/ambiguous diagnostics deliberately do not guess which task is safe.
    The caller must recompile and prepare the returned candidate before reuse.
    """
    steps = plan.get('steps', [])
    parts = str(reason).split(':')
    affected = {s['id'] for s in steps if s.get('id') in parts[1:]}
    if len(affected) != 1:
        return None
    while True:
        descendants = {s['id'] for s in steps if set(s.get('depends_on', [])) & affected}
        if descendants <= affected:
            break
        affected |= descendants
    kept = [deepcopy(s) for s in steps if s['id'] not in affected]
    if not any(s.get('purpose') != 'context' and not s.get('operation') for s in kept):
        return None
    result = deepcopy(plan); result['steps'] = kept
    if 'answer_step_ids' in result:
        result['answer_step_ids'] = [i for i in result['answer_step_ids'] if i not in affected]
        if not result['answer_step_ids']:
            return None
    result.setdefault('unmet_checks', []).extend({'step_id': s['id'], 'question': s.get('question'),
        'reason': reason if s['id'] in parts else 'dependency_unavailable'} for s in steps if s['id'] in affected)
    result.setdefault('interpretation_warnings', []).append(
        'Partial investigation: some requested checks could not be prepared. Retained evidence does not answer those checks.')
    result['retrieval_policy'] = 'partial_independent_v1'
    return result
