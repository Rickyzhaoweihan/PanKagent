"""Deterministic presentation/policy; no scientific constraints are shortened."""
import re
from copy import deepcopy


def literature_only_revision(instruction):
    return bool(re.fullmatch(r'(?:please\s+)?(?:enable|disable|add|include|use|remove|skip|exclude)\s+(?:the\s+)?(?:literature|papers|publications|literature evidence|literature search)(?:\s+please)?[.!]?', instruction.strip(), re.I))


def enable_literature(plan):
    return {**plan, 'literature': True, 'literature_intent': {'included': True,
        'reason': 'always_enabled', 'policy_version': 'always-enabled-v1',
        'summary': 'Literature evidence will be searched after confirmation to help interpret the graph findings.'}}


def expand_compact_plan(plan):
    result=deepcopy(plan)
    for step in result.get('steps',[]):
        if not step.get('question'):
            if len(result['steps'])!=1 or not result.get('interpreted_question'):
                raise ValueError('missing_step_scope')
            step['question']=result['interpreted_question']
        title = result.get('interpreted_question') if len(result['steps']) == 1 else step['question']
        # Keep biological wording in the existing title, without schema tokens.
        title = re.sub(r'\s*\([A-Z][A-Z_]{3,}\)', '', title or step['question'])
        step.setdefault('title', title)
        step.setdefault('rationale','Check the recorded evidence, study context and supporting sources.')
    return enable_literature(result)
