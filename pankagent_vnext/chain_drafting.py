"""Compile a single ordered path proposal into ordinary bounded query tasks."""
from copy import deepcopy
from .composable_planning import object_schema, STRING
from .bounded_paths import PATH_SPEC_SCHEMA

GUIDANCE = '''For this explicitly connected path request, record one complete ordered path in chain_spec, including ALL requested node and edge roles. Do not split it or invent task IDs: the application compiles fragments and joins. chain_spec may have up to sixteen nodes. Every role must be lowercase snake_case. Each edge's from/to follows node order; direction=in represents reverse traversal. Use distinct_from node roles for different genes, never an invented property predicate. constraints contains only explicit user filters with owner_role. Return clarification=null when executable. Retain the entire requested topology, including repeated entity types and shared intermediate nodes. Use only actual schema types and actual grounded identity values.'''


def schema(query_schema):
    constraint = deepcopy(query_schema['properties']['constraints']['items'])
    constraint['required'] = list(dict.fromkeys(constraint['required'] + ['owner_role']))
    return object_schema({'interpreted_question': STRING, 'chain_spec': deepcopy(PATH_SPEC_SCHEMA),
        'constraints': {'type': 'array', 'items': constraint},
        'clarification': {'type': ['string', 'null']}})


def expand(proposal):
    if proposal.get('clarification'):
        return {'interpreted_question': proposal['interpreted_question'], 'steps': [], 'clarification': proposal['clarification']}
    spec = proposal['chain_spec']; nodes = spec['nodes']; edges = spec['edges']
    if not 2 <= len(nodes) <= 16 or len(edges) != len(nodes) - 1:
        raise ValueError('invalid_complete_chain_size')
    roles = [n['role'] for n in nodes]
    if len(set(roles)) != len(roles):
        raise ValueError('duplicate_chain_role')
    owner_roles = set(roles) | {e['role'] for e in edges}
    if any(c.get('owner_role') not in owner_roles for c in proposal['constraints']):
        raise ValueError('unknown_chain_filter_role')
    steps, operations = [], []
    start = 0
    previous = None
    final = None
    while start < len(nodes) - 1:
        end = min(start + 3, len(nodes) - 1)
        # Typed handoff uses one type; move a cut back to an unambiguous role.
        while end < len(nodes) - 1 and len(nodes[end]['entity_types']) != 1 and end > start + 1:
            end -= 1
        if end < len(nodes) - 1 and len(nodes[end]['entity_types']) != 1:
            raise ValueError('chain_handoff_requires_single_typed_role')
        key = 's' + str(len(steps) + 1)
        fragment_nodes = deepcopy(nodes[start:end + 1]); fragment_edges = deepcopy(edges[start:end])
        included = {n['role'] for n in fragment_nodes}
        for n in fragment_nodes:
            # Cross-fragment distinctness is enforced by the acyclic path join.
            if 'distinct_from' in n:
                n['distinct_from'] = [r for r in n['distinct_from'] if r in included]
        owners = included | {e['role'] for e in fragment_edges}
        bindings = [] if previous is None else [{'step_id': previous, 'entity_type': nodes[start]['entity_types'][0],
            'source_role': nodes[start]['role'], 'target_role': nodes[start]['role']}]
        steps.append({'id': key, 'question': proposal['interpreted_question'], 'constraints': [deepcopy(c) for c in proposal['constraints'] if c['owner_role'] in owners],
            'relation_types': sorted({t for e in fragment_edges for t in e['types_any']}),
            'depends_on': [] if previous is None else [previous], 'input_bindings': bindings,
            'complete': True, 'evidence_combination': 'cooccurrence',
            'path_spec': {'version': spec['version'], 'nodes': fragment_nodes, 'edges': fragment_edges}})
        if previous is not None:
            join_id = 'join' + str(len(operations) + 1)
            operations.append({'id': join_id, 'question': 'Preserve the complete connected path.', 'operator': 'join',
                'inputs': [{'step_id': k, 'entity_type': nodes[start]['entity_types'][0], 'role': nodes[start]['role']} for k in (final, key)]})
            final = join_id
        else:
            final = key
        previous = key; start = end
    return {'interpreted_question': proposal['interpreted_question'], 'steps': steps,
        'combine_operations': operations, 'answer_step_ids': [final], 'clarification': None}
