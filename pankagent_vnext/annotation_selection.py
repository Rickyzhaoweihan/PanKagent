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
    # companion checks.  Keep the general depth-squared policy so a long path
    # cannot starve a separate branch.  The reviewed HLA plan has measured,
    # release-specific fan-out: its interaction-only branch needs more bytes
    # than its direct annotation branch, while the joined branch must preserve
    # both relationships.  Recognize only that exact deterministic plan and
    # assign its audited 1:6:14 shares.  Integer shares retain one run-wide cap.
    route = plan.get('planning_route') or {}
    expected_hla_shapes = {
        'direct_annotations': (
            (('focus', ('Gene',)), ('process', ('kegg', 'reactome'))),
            (('annotation', 'focus', 'process', ('FUNCTION_ANNOTATION',), 'out'),)),
        'interaction_partners': (
            (('focus', ('Gene',)), ('partner', ('Gene',))),
            (('interaction', 'focus', 'partner',
              ('GENETIC_INTERACTION', 'PHYSICAL_INTERACTION'), 'either'),)),
        'partner_annotations': (
            (('focus', ('Gene',)), ('partner', ('Gene',)),
             ('process', ('kegg', 'reactome'))),
            (('interaction', 'focus', 'partner',
              ('GENETIC_INTERACTION', 'PHYSICAL_INTERACTION'), 'either'),
             ('annotation', 'partner', 'process', ('FUNCTION_ANNOTATION',), 'out'))),
    }

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

    hla_plan = (
        route.get('kind') == 'verified_bounded_path'
        and route.get('version') == 'bounded-path-v1'
        and len(steps) == len(expected_hla_shapes)
        and {step.get('id') for step in steps} == set(expected_hla_shapes)
        and all(path_shape(step) == expected_hla_shapes.get(step.get('id'))
                for step in steps)
    )
    hla_weights = {
        'direct_annotations': 1,
        'interaction_partners': 6,
        'partner_annotations': 14,
    }
    weights = ([hla_weights[step['id']] for step in steps] if hla_plan else [
        max(1, len(((step.get('path_spec') or {}).get('edges') or [])) ** 2)
        for step in steps
    ])
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
