"""Retrieve full annotation evidence by default; preserve explicit user limits."""
import re
from copy import deepcopy

RELATIONS = {'FUNCTION_ANNOTATION', 'ASSOCIATED_WITH_GO'}
_EXPLICIT = re.compile(r'\b(?:all|every|entire|complete|exhaustive|count|counts|number of|how many|total|percentage|proportion|rank|ranking|top|limit)\b|\b(?:show|list|return|give|retrieve)\s+(?:me\s+)?\d+\b|(?:列出|返回|展示)\s*\d+|全部|所有|完整|多少|数量|个数|总数|百分|比例|排名|前\s*\d+', re.I)


def apply_default(step, question):
    """Ordinary annotation questions require complete retrieval within hard caps."""
    if (step.get('path_spec') or not question or _EXPLICIT.search(question) or step.get('ranking')
            or step.get('depends_on')
            or len(step.get('relation_types', [])) != 1
            or step['relation_types'][0] not in RELATIONS):
        return step
    result = deepcopy(step)
    result['complete'] = True
    result.pop('retrieval_selection', None)
    return result


def overview(step):
    selection = step.get('retrieval_selection') or {}
    return (step.get('complete') is False and selection.get('mode') == 'annotation_overview'
            and selection.get('limit') == 10 and selection.get('exhaustive') is False
            and len(step.get('relation_types', [])) == 1 and step['relation_types'][0] in RELATIONS)


def validation_errors(query, step, parameters):
    if not overview(step):
        return []
    from .query_templates import compile_query
    expected = compile_query(step)
    # This deliberately rejects a LIMIT after collect(), which limits only the
    # single aggregate row and does not bound annotation materialization.
    if (expected is None or query.strip() != expected['cypher'].strip()
            or parameters != expected['parameters']):
        return ['annotation_overview_requires_bounded_template']
    return []


def allocate_independent_budgets(plan, settings):
    """Partition hard materialization caps so concurrent checks cannot starve peers."""
    steps = plan.get('steps') or []
    from .agent_schemas import module
    import json
    route = plan.get('planning_route') or {}
    def path_shape(step):
        spec = step.get('path_spec') or {}
        nodes, edges = spec.get('nodes'), spec.get('edges')
        if (spec.get('version') != 'bounded-path-v1'
                or step.get('evidence_combination') != 'cooccurrence'
                or not isinstance(nodes, list) or not isinstance(edges, list)
                or not all(isinstance(item, dict) for item in nodes + edges)):
            return None
        return (
            tuple((node.get('role'), tuple(node.get('entity_types') or []))
                  for node in nodes),
            tuple((edge.get('role'), edge.get('from'), edge.get('to'),
                   tuple(sorted(edge.get('types_any') or [])), edge.get('direction'))
                  for edge in edges),
        )

    weights = [max(1, len(((step.get('path_spec') or {}).get('edges') or [])) ** 2)
               for step in steps]
    release = module('database_schema')['release']
    for hint in module('validation').get('structural_budget_hints', []):
        if (hint['graph_release'] == release and route.get('kind') == hint['route_kind']
                and route.get('version') == hint['version']
                and {step.get('id') for step in steps} == set(hint['shapes'])
                and len(steps) == len(hint['shapes'])
                and all(json.loads(json.dumps(path_shape(step))) == hint['shapes'][step['id']]
                        for step in steps)):
            weights = [hint['weights'][step['id']] for step in steps]
            break
    total_weight = max(1, sum(weights))
    for step, weight in zip(steps, weights):
        step['retrieval_budget'] = {
            'max_bytes': getattr(settings, 'max_bytes', 2_000_000) * weight // total_weight,
            'max_nodes': getattr(settings, 'max_nodes', 2000) * weight // total_weight,
            'max_edges': getattr(settings, 'max_edges', 5000) * weight // total_weight,
            'max_rows': getattr(settings, 'max_rows', 1000) * weight // total_weight,
        }
    # A completed parent's unused reservation can be shared by its direct
    # children. Split before execution so concurrent siblings cannot each
    # spend the same reservation; unrelated branches keep their own share.
    children = {step['id']: set() for step in steps}
    for step in steps:
        for parent in step.get('depends_on', []):
            if parent in children:
                children[parent].add(step['id'])
    for step in steps:
        step['budget_parent_shares'] = {
            parent: len(children[parent]) for parent in step.get('depends_on', [])
            if parent in children and children[parent]}
    # A gene mentioned in a GWAS question is not a variant/locus binding. Keep
    # the unavailable branch explicit instead of silently scanning a disease.
    genes = [entity for step in steps for entity in step.get('resolved_entities', [])
             if entity.get('state') == 'resolved' and entity.get('entity_type') == 'Gene']
    for step in steps:
        step.pop('gwas_scope_unavailable', None)
        if step.get('relation_types') != ['PART_OF_GWAS_SIGNAL'] or step.get('depends_on'):
            continue
        if any(c.get('entity_type') in {'variants', 'Gene'} for c in step.get('constraints', [])):
            continue
        if any(re.search(r'(?<!\w)' + re.escape(str(entity[field])) + r'(?!\w)', step.get('question', ''), re.I)
               for entity in genes for field in ('id', 'name') if entity.get(field)):
            step['gwas_scope_unavailable'] = True
    return plan


def inherited_budget(step, previous):
    """Transfer only measured unused reservations, never formatter evidence."""
    budget = dict(step.get('retrieval_budget') or {})
    for parent, divisor in (step.get('budget_parent_shares') or {}).items():
        result = previous.get(parent) or {}
        if (type(divisor) is not int or divisor < 1
                or result.get('status') not in {'complete', 'empty'}):
            continue
        accounting = result.get('resource_budget') or {}
        allocated, consumed = accounting.get('allocated', {}), accounting.get('consumed', {})
        for key in budget:
            if key in allocated and key in consumed:
                budget[key] += max(0, allocated[key] - consumed[key]) // divisor
    return budget
