"""Typed planning and deterministic result operations, before answer compaction."""
from copy import deepcopy
import hashlib
import json

VERSION = 'composable-planning-v1'
OPERATORS = ('union', 'intersection', 'difference', 'filter', 'join')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def object_schema(properties, required=None):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties,
            'required': list(properties) if required is None else required}


STRING = {'type': 'string'}
BINDING_SCHEMA = object_schema({k: STRING for k in ('step_id', 'entity_type', 'source_role', 'target_role')})
SELECTOR_SCHEMA = object_schema({k: STRING for k in ('step_id', 'entity_type', 'role')})
OP_SCHEMA = object_schema({'id': STRING, 'question': STRING,
    'operator': {'type': 'string', 'enum': list(OPERATORS)},
    'inputs': {'type': 'array', 'items': SELECTOR_SCHEMA}})


def extend_schema(schema):
    step = schema['properties']['steps']['items']['properties']
    step['input_bindings'] = {'type': 'array', 'items': BINDING_SCHEMA}
    step['context_mode'] = {'type': 'string', 'enum': ['auto', 'full', 'identity_only']}
    schema['properties']['combine_operations'] = {'type': 'array', 'items': OP_SCHEMA}
    schema['properties']['answer_step_ids'] = {'type': 'array', 'items': STRING}


GUIDANCE = '''
Composable planning: decompose requested investigations into atomic query tasks. Independent tasks run in parallel; dependent tasks form chains; both can coexist. Use dependencies only when an earlier result supplies necessary identities. For every new dependency declare input_bindings with step_id, entity_type, source_role and target_role. For path fragments roles are exact path node roles; for ordinary directed queries use source/target. An empty source_role selects all returned nodes of the declared type, never all node types. Compose longer chains from fixed path fragments of at most four node roles. A dependent fragment may anchor its first node through input_bindings instead of inventing an identity constraint.
Declare combine_operations for final union/intersection/difference/filter or path join. Each has id, question, operator, and inputs (step_id, entity_type, role). Operations consume earlier query/operation results. filter is a semijoin retaining records from its first input; difference subtracts subsequent eligible IDs; join connects the last role of the first path to the first role of the next path. An empty role selects all nodes of entity_type. Use answer_step_ids to select final outputs; intermediate populations are not answers. Preserve independent categories as separate outputs unless combination is requested. A broader query is allowed only with explicit downstream filtering that retains all requested constraints. Never add a mandatory relationship that narrows the requested population. For a recorded-stage donor cohort with an independently excluded clinical diabetes category, prefer one query with a verified donor.diabetes_type != category predicate; a negative clinical condition is not a positive disease-node identity request. The runtime resolves the exact categorical spelling. All filters must be represented in a query and verified before combining; no implicit post-filter prose. Set context_mode=auto normally; identity_only is an answer-input view and never changes backend ID completeness. The total query plus combination task budget is twelve.
All node and edge role names must match [a-z][a-z0-9_]{0,31}; use lowercase names. For a five-role chain a-b-c-d-e, use s1 path a-b-c-d and s2 path d-e, then join c1 on the same entity_type and s1 role d / s2 role d; answer_step_ids=["c1"]. BOTH fragments require path_spec, even the two-node fragment. s2 input_bindings=[{step_id:"s1", entity_type:<d type>, source_role:"d", target_role:"d"}]. Each fragment's relation_types must equal ONLY its own edges; each constraint requires owner_role (for example the a identity belongs to role a). Always list path edge from/to in node traversal order; encode backwards traversal using direction="in". A join selector names the shared node, never the terminal output type. Prefer cutting at a single-type node such as Gene; declare exactly one input binding per parent. Each join has exactly two inputs; compose additional fragments through successive join operations. Do not use ordinary non-path queries for fragments of a requested connected chain. Repeated node types require separate role names, not additional identity constraints. Preserve this complete representation when repairing an invalid plan.

'''


def normalize(plan):
    """Validate references and types; retain the old public steps envelope."""
    result = deepcopy(plan)
    steps = result.get('steps', [])
    # Canonicalize the equivalent reverse edge spelling, without changing topology.
    for step in steps:
        spec = step.get('path_spec') or {}
        nodes = spec.get('nodes', [])
        for index, edge in enumerate(spec.get('edges', [])):
            if index + 1 < len(nodes) and edge.get('from') == nodes[index + 1]['role'] and edge.get('to') == nodes[index]['role']:
                edge['from'], edge['to'] = edge['to'], edge['from']
                edge['direction'] = {'in': 'out', 'out': 'in', 'either': 'either'}.get(edge.get('direction'), edge.get('direction'))
    identifiers = {s['id'] for s in steps}
    for op in result.get('combine_operations', []):
        if op['id'] in identifiers:
            existing = next(s for s in steps if s['id'] == op['id'])
            if existing.get('operation') != op:
                raise ValueError('duplicate_operation_id')
            continue
        steps.append({'id': op['id'], 'question': op['question'], 'title': op['question'],
            'rationale': 'Combine verified query results by typed identity.', 'operation': deepcopy(op),
            'depends_on': list(dict.fromkeys(i['step_id'] for i in op['inputs'])),
            'constraints': [], 'relation_types': [], 'complete': True, 'purpose': 'primary'})
        identifiers.add(op['id'])
    if len(steps) > 12:
        raise ValueError('plan_too_large')
    if len(identifiers) != len(steps):
        raise ValueError('duplicate_step_id')
    ordered, seen, pending = [], set(), list(steps)
    while pending:
        ready = next((s for s in pending if set(s.get('depends_on', [])) <= seen), None)
        if ready is None:
            raise ValueError('invalid_plan_dependencies')
        ordered.append(ready); seen.add(ready['id']); pending.remove(ready)
    from .release_schema import REGISTRY
    for s in ordered:
        bindings = s.get('input_bindings', [])
        if bindings and (len(bindings) != len(s.get('depends_on', []))
                         or {b['step_id'] for b in bindings} != set(s['depends_on'])):
            raise ValueError('incomplete_typed_dependency_bindings')
        for b in bindings:
            if b['entity_type'] not in REGISTRY['nodes']:
                raise ValueError('invalid_dependency_type')
            if s.get('path_spec'):
                target = next((n for n in s['path_spec']['nodes'] if n['role'] == b['target_role']), None)
                if target is None or b['entity_type'] not in target['entity_types']:
                    raise ValueError('invalid_dependency_target_role')
            elif b['target_role'] not in ('source', 'target'):
                raise ValueError('invalid_dependency_target_role')
        op = s.get('operation')
        if op:
            if op.get('operator') not in OPERATORS or len(op.get('inputs', [])) < 2:
                raise ValueError('invalid_result_operation')
            if op['operator'] == 'join' and len(op['inputs']) != 2:
                raise ValueError('path_join_requires_two_inputs')
            if len({i['entity_type'] for i in op['inputs']}) != 1:
                raise ValueError('incompatible_result_types')
            if any(i['entity_type'] not in REGISTRY['nodes'] for i in op['inputs']):
                raise ValueError('invalid_result_type')
    result['steps'] = ordered
    if result.get('combine_operations') and 'answer_step_ids' not in result:
        inputs = {d for s in ordered for d in s.get('depends_on', [])}
        result['answer_step_ids'] = [s['id'] for s in ordered if s['id'] not in inputs]
    answers = result.get('answer_step_ids')
    if answers is not None and (not answers or len(answers) != len(set(answers)) or not set(answers) <= seen):
        raise ValueError('invalid_answer_step_ids')
    by_id = {s['id']: s for s in ordered}
    def path_sources(key, include_dependencies):
        task = by_id[key]
        if task.get('operation'):
            return set().union(*(path_sources(i['step_id'], include_dependencies) for i in task['operation']['inputs']))
        found = {key} if task.get('path_spec') else set()
        if include_dependencies:
            for parent in task.get('depends_on', []):
                found |= path_sources(parent, True)
        return found
    for key in answers or []:
        if by_id[key].get('operation', {}).get('operator') == 'join':
            if path_sources(key, False) != path_sources(key, True):
                raise ValueError('final_join_must_include_all_ancestor_path_fragments')
    chain_ids = {s['id'] for s in ordered if s.get('depends_on') or s.get('path_spec')}
    parents = {d for s in ordered for d in s.get('depends_on', [])}
    independent = {s['id'] for s in ordered} - chain_ids - parents
    result['execution_mode'] = 'mixed' if chain_ids and independent else 'chain' if chain_ids else 'parallel'
    result['planning_contract'] = VERSION
    return result


def selected_nodes(result, entity_type, role=''):
    if result.get('context_compaction') or result.get('answer_evidence_scope'):
        raise ValueError('formatter_projection_is_not_dependency_evidence')
    nodes = [n for n in result.get('nodes', []) if entity_type in n.get('labels', [])]
    if role:
        paths = result.get('path_records') or []
        if not paths and role in ('source', 'target'):
            key = 'start_id' if role == 'source' else 'end_id'
            ids = {e.get(key) for e in result.get('edges', [])}
            return [n for n in nodes if n['id'] in ids]
        if not paths and result.get('status') != 'empty':
            raise ValueError('dependency_role_unavailable')
        ids = {n['id'] for p in paths for n in p['nodes'] if n['role'] == role}
        if paths and not any(n['role'] == role for p in paths for n in p['nodes']):
            raise ValueError('dependency_role_unavailable')
        nodes = [n for n in nodes if n['id'] in ids]
    return nodes


def snapshot(result):
    return digest({k: result.get(k) for k in ('step_id', 'graph_version', 'status', 'truncated',
        'nodes', 'edges', 'rows', 'path_records', 'retrieval_execution', 'validation')})


def complete(result):
    execution = result.get('retrieval_execution') or {}
    return (result.get('status') in ('complete', 'empty') and result.get('truncated') is False
            and execution.get('completed') is True and execution.get('cursor_exhausted') is True)


def combine(step, previous):
    """Pure result algebra. Never read sampled formatting payloads or invent edges."""
    op = step['operation']
    base = {'step_id': step['id'], 'question': step['question'], 'title': step.get('title', step['question']),
        'status': 'blocked', 'nodes': [], 'edges': [], 'rows': [], 'queries': [], 'provenance': [],
        'truncated': False, 'validation': [], 'requested_scope': {'complete': True, 'operation': op}}
    parents = [previous.get(i['step_id']) for i in op['inputs']]
    if any(not p or p.get('status') not in ('complete', 'empty', 'partial') for p in parents):
        base['error'] = {'category': 'dependency_unavailable', 'message': 'A combination input is unavailable.'}
        return base
    releases = {p.get('graph_version') for p in parents}
    if len(releases) != 1 or None in releases:
        raise ValueError('combination_graph_release_mismatch')
    identity_types = {}
    for parent in parents:
        for node in parent.get('nodes', []):
            labels = set(node.get('labels', []))
            previous_labels = identity_types.get(node['id'])
            if previous_labels is not None and not previous_labels.intersection(labels):
                raise ValueError('ambiguous_cross_type_identity')
            identity_types[node['id']] = labels
    sets = [{n['id'] for n in selected_nodes(p, i['entity_type'], i['role'])}
            for p, i in zip(parents, op['inputs'])]
    all_complete = all(complete(p) for p in parents)
    kind = op['operator']
    if kind == 'difference' and not all_complete:
        base['error'] = {'category': 'incomplete_exclusion_input', 'message': 'Exclusion requires complete input ID sets.'}
        return base
    wanted = set.union(*sets) if kind == 'union' else (sets[0] - set.union(*sets[1:]) if kind == 'difference'
              else set.intersection(*sets))
    entity_type = op['inputs'][0]['entity_type']
    paths = []
    if kind == 'join':
        if any(p.get('nodes') and not p.get('path_records') for p in parents):
            base['error'] = {'category': 'path_witnesses_unavailable', 'message': 'Joining requires verified path records for both fragments.'}
            return base
        paths = deepcopy(parents[0].get('path_records', []))
        for parent, left_selector, right_selector in zip(parents[1:], op['inputs'], op['inputs'][1:]):
            joined = []
            for left in paths:
                for right in parent.get('path_records', []):
                    a, b = left['nodes'][-1], right['nodes'][0]
                    if (a['id'] != b['id'] or a['role'] != left_selector['role'] or b['role'] != right_selector['role']
                            or entity_type not in a['labels'] or entity_type not in b['labels']):
                        continue
                    nodes = left['nodes'] + right['nodes'][1:]
                    if len({n['id'] for n in nodes}) != len(nodes):
                        continue
                    joined.append({'nodes': nodes, 'edges': left['edges'] + right['edges']})
                    if len(joined) > 2000:
                        base['error'] = {'category': 'combined_path_limit', 'message': 'The combined path result exceeds the evidence budget.'}
                        return base
            paths = joined
        wanted_nodes = {n['id'] for p in paths for n in p['nodes']}
        wanted_edges = {e['fingerprint'] for p in paths for e in p['edges']}
    else:
        for p, selector in zip(parents[:1] if kind in ('filter', 'difference') else parents,
                               op['inputs'][:1] if kind in ('filter', 'difference') else op['inputs']):
            for path in p.get('path_records', []):
                members = [n['id'] for n in path['nodes'] if entity_type in n['labels']
                           and (not selector['role'] or n['role'] == selector['role'])]
                if members and all(identifier in wanted for identifier in members):
                    paths.append(deepcopy(path))
        wanted_nodes = wanted | {n['id'] for p in paths for n in p['nodes']}
        wanted_edges = {e['fingerprint'] for p in paths for e in p['edges']}
    sources = parents[:1] if kind in ('filter', 'difference') else parents
    nodes, edges = {}, {}
    for p in sources:
        for e in p.get('edges', []):
            from .graph import _public_edge_fingerprint
            key = _public_edge_fingerprint(e)
            endpoints = {e.get('start_id'), e.get('end_id')}
            # Keep only witnesses incident to selected entities, or verified path edges.
            rejected = {n['id'] for n in p.get('nodes', []) if entity_type in n.get('labels', [])} - wanted
            keep = key in wanted_edges or (kind != 'join' and bool(endpoints & wanted) and not endpoints & rejected)
            if keep:
                edges[digest(e)] = deepcopy(e); wanted_nodes.update(endpoints)
        for n in p.get('nodes', []):
            if n['id'] in wanted_nodes:
                nodes[(tuple(sorted(n.get('labels', []))), n['id'])] = deepcopy(n)
    base.update(graph_version=next(iter(releases)), nodes=list(nodes.values()), edges=list(edges.values()),
        path_records=list({digest(p): p for p in paths}.values()), status='complete' if all_complete else 'partial',
        truncated=not all_complete, retrieval_execution={'completed': True, 'cursor_exhausted': all_complete, 'mode': 'verified_derivation'},
        validation=[{'valid': True, 'reasons': []}],
        derivation={'version': VERSION, 'operation': deepcopy(op), 'operation_sha256': digest(op),
                    'parents': {p['step_id']: snapshot(p) for p in parents}},
        provenance=[{'step_id': p['step_id'], 'graph_version': p['graph_version'], 'sources': deepcopy(p.get('provenance', []))} for p in parents],
        combination_summary={'operator': kind, 'entity_type': entity_type, 'selected_entity_count': len(wanted) if kind != 'join' else None,
                             'complete': all_complete})
    if not base['nodes'] and all_complete:
        base['status'] = 'empty'
    from .semantic_registry import donor_summary
    summary = donor_summary(base)
    if summary:
        base['donor_summary'] = summary
    return base


def verify_derivation(step, result, previous):
    proof = result.get('derivation') or {}
    return (snapshot(result) == snapshot(combine(step, previous))
            and proof.get('version') == VERSION and proof.get('operation') == step.get('operation')
            and proof.get('operation_sha256') == digest(step.get('operation'))
            and proof.get('parents') == {key: snapshot(previous[key]) for key in step.get('depends_on', []) if key in previous}
            and set(step.get('depends_on', [])) <= set(previous))


def answer_results(plan, previous):
    ids = plan.get('answer_step_ids')
    return previous if ids is None else {key: previous[key] for key in ids if key in previous}
