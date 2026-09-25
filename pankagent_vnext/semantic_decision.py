"""Retain Claude's scope choice separately from Python's lexical assessment."""
from copy import deepcopy
import hashlib


def record(step, question):
    return {'source': 'claude_record_plan', 'request_sha256': hashlib.sha256(question.encode()).hexdigest(),
            'step_id': step['id'], 'constraints': deepcopy(step.get('constraints', []))}


def covers(step, index, constraint, question):
    decision = step.get('model_scope_decision') or {}
    if (decision.get('source') != 'claude_record_plan'
            or decision.get('step_id') != step.get('id')
            or decision.get('request_sha256') != hashlib.sha256(question.encode()).hexdigest()):
        return False
    # A preparation canonicalization may add owner metadata. Its recorded input
    # must still be an actual model-selected predicate, not new helper scope.
    originals = [constraint] + [change.get('requested') for change in step.get('constraint_compilation', [])
        if change.get('constraint_index') == index and change.get('canonical_binding') == constraint]
    def same_predicate(selected, prepared):
        if not isinstance(selected, dict) or not isinstance(prepared, dict):
            return False
        defaults = {'operator': '=', 'entity_type': None}
        if any(selected.get(k, defaults.get(k)) != prepared.get(k, defaults.get(k))
               for k in ('property', 'operator', 'value', 'entity_type')):
            return False
        # A verified owner normalizer may fill absent metadata without changing
        # the selected predicate. It may not replace an owner Claude specified.
        return all(k not in selected or selected[k] == prepared.get(k)
                   for k in ('owner_role', 'owner_kind', 'relationship_type'))
    return any(same_predicate(selected, original)
               for selected in decision.get('constraints', []) for original in originals)
