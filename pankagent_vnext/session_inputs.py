"""Typed, release-pinned prior populations. Never use answer text or display samples."""
from copy import deepcopy
import re
import json
from .agent_schemas import active_pack, module
from .composable_planning import complete, snapshot, selected_nodes


def population(question, prior, graph_release):
    if not prior or prior.get('status') not in {'completed', 'partial'}:
        return None
    rules = module('identity')['session_references']
    matching = [rule for rule in rules if re.search(rule['mention_pattern'], question, re.I)]
    if len(matching) != 1:
        return None
    rule = matching[0]
    if (prior.get('plan') or {}).get('run_context', {}).get('agent_schema', {}).get('sha256') != active_pack().digest:
        return None
    candidates = []
    answer_ids = set((prior.get('plan') or {}).get('answer_step_ids') or [])
    for result in (prior.get('evidence') or {}).get('steps', []):
        if answer_ids and result.get('step_id') not in answer_ids:
            continue
        if not complete(result) or result.get('graph_version') != graph_release:
            continue
        nodes = selected_nodes(result, rule['entity_type'])
        if nodes:
            candidates.append((frozenset(n['id'] for n in nodes), result))
    if not candidates or len({ids for ids, _ in candidates}) != 1:
        return None
    ids, result = candidates[0]
    return {'source_run_id': prior['run_id'], 'source_step_id': result['step_id'],
            'snapshot': snapshot(result), 'entity_type': rule['entity_type'],
            'graph_release': graph_release, 'schema_sha256': active_pack().digest,
            'count': len(ids), 'query_relations': rule['query_relations']}


def attach(plan, reference):
    """Bind new root queries to one authorized backend population."""
    result = deepcopy(plan)
    for step in result.get('steps', []):
        step.pop('session_input', None)  # never trust a model-authored reference
    if not reference or result.get('clarification'):
        return result
    anchor = 'session_population'
    if any(s['id'] == anchor for s in result['steps']):
        raise ValueError('reserved_session_population_id')
    # The model may name the prior task as an external dependency. Resolve
    # only the authenticated reference, never an arbitrary historical task.
    local_ids = {s['id'] for s in result['steps']}
    parent_names = {reference['source_step_id'], anchor} - local_ids
    roots = [s for s in result['steps'] if not s.get('operation')
             and set(s.get('depends_on', [])) <= parent_names]
    targets = [s for s in roots if set(s.get('relation_types', [])) & set(reference['query_relations'])]
    if not targets or len(targets) != len(roots):
        raise ValueError('session_population_requires_connected_queries:' + json.dumps({
            'required_relations': reference['query_relations'],
            'root_shapes': [{'relations': s.get('relation_types', []), 'has_path': bool(s.get('path_spec'))} for s in roots],
            'instruction': 'Each new root must connect the saved donor population through HAS_SAMPLE. Return the sample assay and tissue as annotations; do not add separate unanchored annotation queries.'}))
    for step in targets:
        if step.get('path_spec'):
            nodes = step['path_spec']['nodes']
            roles = [n['role'] for n in nodes if reference['entity_type'] in n['entity_types']]
            if len(roles) != 1:
                raise ValueError('session_population_role_ambiguous')
            target_role = roles[0]
        else:
            from .release_schema import REGISTRY
            paths = [p for relation in step['relation_types'] for p in REGISTRY['relations'][relation]['paths']]
            sides = {side for side in ('source', 'target') if any(reference['entity_type'] in p[side] for p in paths)}
            if len(sides) != 1:
                raise ValueError('session_population_role_ambiguous')
            target_role = next(iter(sides))
        step['depends_on'] = [anchor]
        step['input_bindings'] = [{'step_id': anchor, 'entity_type': reference['entity_type'],
                                  'source_role': '', 'target_role': target_role}]
    result['steps'].insert(0, {'id': anchor, 'question': 'Reuse the verified population from the previous answer.',
        'depends_on': [], 'constraints': [], 'relation_types': [], 'complete': True,
        'purpose': 'primary', 'session_input': deepcopy(reference)})
    result['answer_step_ids'] = result.get('answer_step_ids') or [s['id'] for s in result['steps'] if s['id'] != anchor]
    result['session_population'] = deepcopy(reference)
    return result


def materialize(step, source, current):
    reference = step['session_input']
    if (not source or source['run_id'] != reference['source_run_id']
            or source['session_id'] != current['session_id']
            or reference['schema_sha256'] != active_pack().digest):
        raise ValueError('session_population_unavailable')
    result = next((r for r in (source.get('evidence') or {}).get('steps', [])
                   if r.get('step_id') == reference['source_step_id']), None)
    if (not result or not complete(result) or snapshot(result) != reference['snapshot']
            or result.get('graph_version') != reference['graph_release']):
        raise ValueError('session_population_stale_or_incomplete')
    value = deepcopy(result)
    value.update(step_id=step['id'], question=step['question'],
                 session_input_provenance=deepcopy(reference))
    return value
