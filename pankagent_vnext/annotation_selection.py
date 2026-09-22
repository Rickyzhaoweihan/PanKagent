"""Bound ordinary annotation overviews without changing explicit set requests."""
import re
from copy import deepcopy

RELATIONS = {'FUNCTION_ANNOTATION', 'ASSOCIATED_WITH_GO'}
_EXPLICIT = re.compile(r'\b(?:all|every|entire|complete|exhaustive|count|counts|number of|how many|total|percentage|proportion|rank|ranking|top|limit)\b|\b(?:show|list|return|give|retrieve)\s+(?:me\s+)?\d+\b|(?:列出|返回|展示)\s*\d+|全部|所有|完整|多少|数量|个数|总数|百分|比例|排名|前\s*\d+', re.I)


def apply_default(step, question):
    """Only a user-authored question can authorize the overview default."""
    if (step.get('path_spec') or not question or _EXPLICIT.search(question) or step.get('ranking')
            or step.get('depends_on')
            or len(step.get('relation_types', [])) != 1
            or step['relation_types'][0] not in RELATIONS):
        return step
    result = deepcopy(step)
    result['complete'] = False
    result['retrieval_selection'] = {'mode': 'annotation_overview', 'limit': 10,
        'exhaustive': False, 'ordering': 'stable_identifiers',
        'note': 'Up to 10 annotation relationships for this check; not ranked by importance and not a total count.'}
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
    # Fixed paths return one aggregate row, but a multi-edge path can contain
    # many more unique nodes and relationships than either of its one-edge
    # companion checks.  Weight by edge-count squared while retaining a hard
    # run-wide cap: integer shares never sum above the configured limit.
    weights = [max(1, len((step.get('path_spec') or {}).get('edges', [])) ** 2)
               for step in steps]
    total_weight = max(1, sum(weights))
    for step, weight in zip(steps, weights):
        step['retrieval_budget'] = {
            'max_bytes': getattr(settings, 'max_bytes', 2_000_000) * weight // total_weight,
            'max_nodes': getattr(settings, 'max_nodes', 2000) * weight // total_weight,
            'max_edges': getattr(settings, 'max_edges', 5000) * weight // total_weight,
            'max_rows': getattr(settings, 'max_rows', 1000) * weight // total_weight,
        }
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
