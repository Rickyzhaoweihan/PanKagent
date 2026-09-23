"""One standalone-question interpretation, followed by ordinary planning."""
from copy import deepcopy
from .composable_planning import object_schema, STRING, digest

SCHEMA = object_schema({'new_question': STRING,
    'execution': {'type': 'string', 'enum': ['chain_restart', 'parallel_extend', 'clarify']},
    'reason': STRING, 'recommended_question': STRING})
SYSTEM = '''Rewrite the current standalone question using the user's revision instruction. Return a complete new_question preserving every unchanged entity, filter, negation, requested output, and explicit preference. Do not answer, invent values, or silently relax filters. Use chain_restart if a chain/path changes or is introduced; parallel_extend for an independent addition or a parallel population revision. Removing/replacing a filter may admit IDs absent from the old result: the new question must cover that population, not just old IDs. A purely independent addition to a mixed plan may reuse its chain. If intent is ambiguous use clarify, explain the exact ambiguity in reason, and recommend a complete question in recommended_question without applying it. Write only the revised scope in new_question; do not restate removed conditions or narrate the revision. For example removing a stage restriction from a donor count produces "Count all donors from the retained source", not "without T1D stage filtering". This is one interpretation call, not a patch or repair workflow.'''


def retrieval_signature(step):
    keys = ('relation_types', 'constraints', 'path_spec', 'input_bindings', 'depends_on', 'complete',
            'operation', 'retrieval_selection', 'sample_requirements', 'purpose')
    return digest({key: step.get(key) for key in keys})


def reuse_step_ids(plan, parent):
    """Stable IDs for structurally identical independent checks; never narrow new queries."""
    result = deepcopy(plan)
    old = parent.get('steps', [])
    mapping, used = {}, set()
    for step in result['steps']:
        matches = [s for s in old if not s.get('depends_on') and not step.get('depends_on')
                   and s['id'] not in used and retrieval_signature(s) == retrieval_signature(step)]
        if len(matches) == 1:
            mapping[step['id']] = matches[0]['id']; used.add(matches[0]['id'])
    occupied = set(mapping.values())
    for step in result['steps']:
        key = step['id']
        if key not in mapping:
            new = key
            while new in occupied:
                new += '_new'
            mapping[key] = new; occupied.add(new)
    for step in result['steps']:
        step['id'] = mapping[step['id']]
        step['depends_on'] = [mapping.get(k, k) for k in step.get('depends_on', [])]
        for binding in step.get('input_bindings', []):
            binding['step_id'] = mapping.get(binding['step_id'], binding['step_id'])
    for op in result.get('combine_operations', []):
        for selector in op['inputs']:
            selector['step_id'] = mapping.get(selector['step_id'], selector['step_id'])
    if 'answer_step_ids' in result:
        result['answer_step_ids'] = [mapping.get(k, k) for k in result['answer_step_ids']]
    return result
