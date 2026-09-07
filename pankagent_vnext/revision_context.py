"""Keep revision intent and useful preview data without replaying internal logs."""
from copy import deepcopy
from collections import Counter

PLAN_FIELDS = ('interpreted_question', 'literature', 'literature_intent', 'clarification', 'include_context')
STEP_FIELDS = ('id', 'question', 'title', 'rationale', 'relation_types', 'depends_on', 'constraints',
               'complete', 'purpose', 'context_for', 'context_kind', 'evidence_combination', 'schema_bindings')

def parent_context(parent):
    plan = parent.get('plan') or {}
    clean = {key: deepcopy(plan[key]) for key in PLAN_FIELDS if key in plan}
    clean['steps'] = [{key: deepcopy(step[key]) for key in STEP_FIELDS if key in step} for step in plan.get('steps', [])]
    preview = parent.get('preview') or {}
    evidence = preview.get('evidence') or {}
    nodes = evidence.get('nodes') or []
    edges = evidence.get('edges') or []
    summary = {'status': preview.get('status'), 'node_count': len(nodes), 'edge_count': len(edges),
        'preview_summary_only': True, 'entity_list_sampled': len(nodes) > 40,
        'entities': [{'id': node.get('id'), 'labels': node.get('labels'), 'name': (node.get('properties') or {}).get('name')} for node in nodes[:40]],
        'relation_counts': dict(Counter(edge.get('type', 'unknown') for edge in edges)),
        'step_outcomes': [{'step_id': step.get('step_id'), 'status': step.get('status'), 'truncated': step.get('truncated', False)} for step in evidence.get('steps', [])]}
    return clean, summary
