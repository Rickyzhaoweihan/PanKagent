"""Literature enriches a usable new graph answer; it never replaces one."""
VERSION = 'grounded-literature-v2'


def literature_gate(plan, evidence, answer):
    if not plan.get('literature'):
        return False, 'not_requested'
    from .definition_intent import definition_only_plan
    if not plan.get('steps') or plan.get('answer_mode') in {'skills', 'skill_only', 'explanation'} or definition_only_plan(plan):
        return False, 'no_new_graph_requested'
    if not isinstance(answer, str) or not answer.strip():
        return False, 'no_graph_answer'
    steps = evidence.get('steps', []) if isinstance(evidence, dict) else []
    if isinstance(steps, dict): steps = list(steps.values())
    for step in steps:
        if step.get('purpose') == 'context' or step.get('status') not in {'complete', 'partial'}:
            continue
        if step.get('nodes') or step.get('edges'):
            return True, 'grounded_graph_answer'
        # Zero counts remain useful answers, but do not license literature.
        for row in step.get('rows', []):
            if not isinstance(row, dict): continue
            numbers = [v for v in row.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if numbers and any(v != 0 for v in numbers):
                return True, 'grounded_scalar_answer'
    return False, 'no_usable_graph_evidence'
