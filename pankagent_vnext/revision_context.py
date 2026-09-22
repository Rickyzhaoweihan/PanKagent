"""Keep revision intent and useful preview data without replaying internal logs."""
from copy import deepcopy
from collections import Counter

PLAN_FIELDS = ('interpreted_question', 'literature', 'literature_intent', 'clarification', 'include_context')
STEP_FIELDS = ('id', 'question', 'title', 'rationale', 'relation_types', 'depends_on', 'constraints',
               'complete', 'purpose', 'context_for', 'context_kind', 'evidence_combination', 'schema_bindings',
               'path_spec')

def parent_context(parent):
    plan = parent.get('plan') or {}
    clean = {key: deepcopy(plan[key]) for key in PLAN_FIELDS if key in plan}
    clean['steps'] = [{key: deepcopy(step[key]) for key in STEP_FIELDS if key in step} for step in plan.get('steps', [])]
    # Retain only verified public identity fields needed to compare an ID with
    # a recorded name during a replacement. Never replay resolution signatures,
    # candidate diagnostics, or full graph records into the planning context.
    identity_fields = ('constraint_index', 'state', 'id', 'name', 'entity_type', 'labels')
    for source, target in zip(plan.get('steps', []), clean['steps']):
        identities = [{key: deepcopy(value[key]) for key in identity_fields if key in value}
                      for value in source.get('resolved_entities', [])
                      if value.get('state') == 'resolved' and value.get('id') and value.get('entity_type')]
        if identities:
            target['resolved_entities'] = identities
        if source.get('purpose') == 'context' and source.get('context_for'):
            from .plan_constraints import related_context_step
            origin = next((step for step in plan.get('steps', []) if step.get('id') == source['context_for']), None)
            expected = related_context_step({**plan, 'steps': [origin]}) if origin else None
            if expected and all(source.get(key) == value for key, value in expected.items()):
                target['application_generated_context'] = {
                    'kind': 'related_context_step', 'version': 1, 'source_step_id': origin['id']}
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
