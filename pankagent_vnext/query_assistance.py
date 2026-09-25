"""Structured assistance uses existing verified values, never new literal filters."""
from copy import deepcopy
import json
from .composable_planning import digest, normalize, selected_nodes

SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'action': {'type': 'string', 'enum': ['no_change', 'patch', 'repair_upstream', 'clarify']},
    'step_json': {'type': 'string'}, 'reason': {'type': 'string'},
}, 'required': ['action', 'step_json', 'reason']}
SYSTEM = '''Assist a PanKgraph structural template or combination, or diagnose conflicting queries.
Use the original request, immutable approved task and verified runtime entities.
Return a complete step JSON only for a structural patch. Preserve all constraint values,
operators, entity types, relation types, dependencies, completeness and requested output scope.
You may correct owner_role, path structure or input binding roles using supplied schema and
verified parent paths. For combination operations preserve the operator, input step IDs and
entity types; correct only demonstrably mismatched roles. Do not generate Cypher here.
Never choose a candidate because it has more rows or invent evidence. If missing predicates
cannot be recovered from the existing approved task, return clarify. repair_upstream requires
a supplied earlier step ID and preserves its authorized constraints. A proposal is advice:
Python validates and executes it. Return no_change when the current structure is correct.'''


def approved_patch(original, proposal, parents):
    """No scope relaxation: changed literal constraints never pass this boundary."""
    if not isinstance(proposal, dict) or proposal.get('id') != original.get('id'):
        raise ValueError('assistance_step_identity_changed')
    allowed = {'path_spec', 'input_bindings', 'operation', 'constraints'}
    for key in set(original) | set(proposal):
        if key not in allowed and original.get(key) != proposal.get(key):
            raise ValueError('assistance_scope_changed:' + key)
    strip = lambda cs: [{k:v for k,v in c.items() if k != 'owner_role'} for c in cs]
    if strip(original.get('constraints', [])) != strip(proposal.get('constraints', [])):
        raise ValueError('assistance_predicate_changed')
    before, after = original.get('operation'), proposal.get('operation')
    if before:
        if not after or {k:v for k,v in before.items() if k != 'inputs'} != {k:v for k,v in after.items() if k != 'inputs'}:
            raise ValueError('assistance_operation_scope_changed')
        if [(s['step_id'],s['entity_type']) for s in before['inputs']] != [(s['step_id'],s['entity_type']) for s in after['inputs']]:
            raise ValueError('assistance_operation_inputs_changed')
        for selector in after['inputs']:
            parent = parents[selector['step_id']]
            selected = selected_nodes(parent, selector['entity_type'], selector['role'])
            if (selector['role'] and not selected and
                    any(selector['entity_type'] in node.get('labels', []) for node in parent.get('nodes', []))):
                raise ValueError('assistance_role_has_no_witness')
    elif after:
        raise ValueError('assistance_added_operation')
    # Resolutions bind to exact constraints; let prepare_plan rebuild proof after
    # changing owner roles rather than copying stale requested-constraint proofs.
    return deepcopy(proposal)


def diagnostic_view(result):
    """Samples explain roles; full sets remain exclusively in the executor."""
    nodes = result.get('nodes', [])
    roles = sorted({n['role'] for path in result.get('path_records', []) for n in path.get('nodes', [])})
    return {k:deepcopy(result[k]) for k in ('step_id','status','error','graph_version',
            'truncated','retrieval_execution','validation','evidence_coverage','queries') if k in result} | {
        'node_count':len(nodes), 'edge_count':len(result.get('edges', [])),
        'node_types':sorted({label for n in nodes for label in n.get('labels', [])}),
        'path_roles':roles, 'node_examples':deepcopy(nodes[:5]),
        'path_examples':deepcopy(result.get('path_records', [])[:2]),
        'sampled_for_diagnosis_only':True}


async def advise(runtime, run_id, kind, step, packet, parents):
    from .agent_schemas import module
    run = await runtime.io.call(runtime.store.get, run_id)
    if not run or run['status'] not in {'planning', 'awaiting_confirmation'}:
        return None
    if not await runtime.io.call(runtime.store.claim_execution_repair, run_id, step['id'], module('validation_repair')['limits'], {'planning', 'awaiting_confirmation'}):
        return None
    if kind == 'query_repair':
        return await runtime.gateway.repair_cypher(step, packet['question'], packet['failures'], packet['candidate'])
    if not hasattr(runtime.gateway, 'assist_query_structure'):
        return None
    from .agent_schemas import module as schema_module
    packet = {**packet, **({'result':diagnostic_view(packet['result'])} if 'result' in packet else {})}
    payload = {'kind':kind, 'question':run['question'], 'approved_step':step,
               'request_context':step.get('request_context'), 'diagnostics':packet,
               'parents':{key:diagnostic_view(value) for key,value in parents.items()},
               'schema':schema_module('query_patterns'),
               'database_schema':schema_module('graph_storage')}
    response = await runtime.gateway.assist_query_structure(payload)
    await runtime.io.call(runtime.store.audit_event, run_id, 'query_assistance', {
        'kind':kind, 'step_id':step['id'], 'response':response, 'input_sha256':digest(payload)})
    if response.get('action') not in {'patch','repair_upstream'}:
        return None
    proposal = json.loads(response['step_json'])
    target = step
    if response['action'] == 'repair_upstream':
        target = next((s for s in run['plan']['steps'] if s['id'] in step.get('depends_on', []) and s['id']==proposal.get('id')), None)
        if target is None:
            raise ValueError('assistance_unknown_parent')
    patch = approved_patch(target, proposal, parents)
    plan = deepcopy(run['plan'])
    plan['steps'] = [patch if s['id']==patch['id'] else s for s in plan['steps']]
    if patch.get('operation'):
        plan['combine_operations'] = [deepcopy(patch['operation']) if op['id']==patch['id'] else op
                                      for op in plan.get('combine_operations', [])]
    plan = normalize(plan)
    if not patch.get('operation'):
        plan = await runtime.graph.prepare_plan(plan, lambda k,p: runtime.emit(run_id,k,p))
        patch = next(s for s in plan['steps'] if s['id']==patch['id'])
        # Preparation may resolve existing values, but not replace the request.
        approved_patch(target, {**target, **{k:patch[k] for k in ('constraints','path_spec','input_bindings') if k in patch}}, parents)
    return patch
