"""Prepare definite organ-scoped cell/marker checks from cached ontology scope.

Only canonical membership is supplied. The GPU still generates every lookup;
ordinary typed-predicate and endpoint validators still approve each query.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .anatomy_membership import DIGEST as MEMBERSHIP_DIGEST, REGISTRY, resolve_cell_membership

VERSION = 'anatomy-scope-1'
DIGEST = hashlib.sha256((VERSION + MEMBERSHIP_DIGEST).encode()).hexdigest()
HIERARCHY = {'HAS_CELL_TYPE', 'PART_OF', 'SUBCLASS_OF'}
ANATOMY_TYPES = {'anatomical_structure', 'tissue', 'cell_type', 'cell'}


def _recovery(plan, reason, release):
    message = {
        'step_cap': 'This cell-and-marker investigation needs separate checks to retain cell groups with no marker annotation. Narrow the investigation to one tissue and its marker annotations so it fits the three-check limit.',
        'multiple_roots': 'More than one tissue scope or an additional cell restriction needs to be preserved. Ask for one tissue and its marker annotations, or name the exact cell groups to compare.',
        'unrepresented_scope': 'The recorded hierarchy cannot safely represent all of these extra cell or tissue restrictions. Specify the cell groups for this marker lookup; your other filters have been kept.',
        'empty_membership': 'This release has no registered cell-group hierarchy for the selected tissue. This is an indexing limitation, not evidence that those cells are absent. Name a cell type to inspect its marker annotations.',
        'invalid_registry': 'The service could not verify the tissue-to-cell membership registry for this graph release. Your question is saved; changing its biological scope will not repair the service registry.',
    }[reason]
    value = {'category': 'anatomy_scope_needs_clarification', 'title': 'The cell and marker scope needs clarification',
             'message': message, 'retryable': False,
             'suggestions': [] if reason == 'invalid_registry' else [{'label': 'Focus on one tissue', 'instruction': 'Focus this question on one tissue and its recorded cell types and marker annotations; keep any gene and evidence filters.'}],
             'evidence': {'graph_release': release, 'reason': reason, 'registry_digest': MEMBERSHIP_DIGEST}}
    plan['anatomy_scope_issue'] = reason
    plan['recovery'] = value
    plan['clarification'] = message
    plan['entity_resolution'] = {'state': 'needs_clarification', 'graph_version': release, 'issues': [{'category': value['category'], 'reason': reason}]}
    plan['review_ready'] = False
    return plan


def _scope_constraint(constraint):
    return (constraint.get('entity_type') in ANATOMY_TYPES
            or str(constraint.get('value')) in REGISTRY['categories'])


async def normalize_plan(plan: dict, graph_release: str, resolve_constraint, max_steps: int = 3) -> dict:
    """Expand/reuse cell inventory + marker checks without silently dropping scope.

    resolve_constraint is the adapter's existing cached canonical resolver.
    Prior generated scope is rebuilt from the immutable original step snapshot,
    so a stale digest or edited membership list is never trusted on replay.
    """
    result = copy.deepcopy(plan)
    previous = result.pop('anatomy_scope_normalization', None)
    if previous:
        result['steps'] = copy.deepcopy(previous['original_steps'])
        if previous.get('original_display_groups') is not None:
            result['display_groups'] = copy.deepcopy(previous['original_display_groups'])
    if result.pop('anatomy_scope_issue', None):
        recovery = result.get('recovery') or {}
        if recovery.get('category') == 'anatomy_scope_needs_clarification':
            result.pop('recovery', None)
            if result.get('clarification') == recovery.get('message'):
                result['clarification'] = None
    original = copy.deepcopy(result.get('steps') or [])
    scopes = {}
    for step in original:
        relations = set(step.get('relation_types') or [])
        family = relations and relations <= HIERARCHY and re.search(r'\bcell(?:s| types?| populations?| groups?)?\b', step.get('question', ''), re.I)
        marker = 'MARKER_GENE_OF' in relations and relations <= HIERARCHY | {'MARKER_GENE_OF'}
        if not (family or marker) or step.get('purpose') == 'context':
            continue
        roots = []
        other_anatomy = []
        for index, constraint in enumerate(step.get('constraints') or []):
            if not _scope_constraint(constraint):
                continue
            prop = str(constraint.get('property', '')).split('.')[-1]
            if prop == 'category' and constraint.get('value') == 'cell_type':
                continue
            if prop in {'id', 'name'} and constraint.get('operator', '=') == '=' and isinstance(constraint.get('value'), str):
                resolved = await resolve_constraint(constraint, index, step)
                identifier = resolved.get('id') if resolved.get('state') == 'resolved' else None
                if REGISTRY['categories'].get(identifier) in {'tissue', 'region'}:
                    roots.append((index, identifier, resolved))
                    continue
            other_anatomy.append(constraint)
        if len(roots) > 1 or roots and other_anatomy:
            return _recovery(result, 'multiple_roots', graph_release)
        if not roots:
            continue
        index, root, resolved = roots[0]
        scope_text = re.sub(r'\bwithout\s+(?:any\s+)?(?:limits?|list slices?|rank cutoffs?)\b', '', step.get('question', ''), flags=re.I)
        if re.search(r'\b(?:only|excluding|except|without)\b', scope_text, re.I):
            return _recovery(result, 'unrepresented_scope', graph_release)
        try:
            membership = resolve_cell_membership(root, graph_release)
        except ValueError:
            return _recovery(result, 'invalid_registry', graph_release)
        if not membership['cell_ids']:
            return _recovery(result, 'empty_membership', graph_release)
        scopes[step['id']] = {'root': root, 'root_index': index, 'resolved': resolved,
                              'membership': membership, 'kind': 'markers' if marker else 'cells'}
    # A marker check may already depend on a separately planned organ/cell check.
    for step in original:
        if step['id'] in scopes or set(step.get('relation_types') or []) != {'MARKER_GENE_OF'}:
            continue
        parents = [scopes[d] for d in step.get('depends_on', []) if d in scopes and scopes[d]['kind'] == 'cells']
        if len(parents) == 1:
            if any(_scope_constraint(c) for c in step.get('constraints', [])):
                return _recovery(result, 'unrepresented_scope', graph_release)
            scopes[step['id']] = {**parents[0], 'root_index': None, 'kind': 'markers'}
        elif len(parents) > 1:
            return _recovery(result, 'multiple_roots', graph_release)
    if not scopes:
        return result

    by_id = {s['id']: s for s in original}
    cell_checks = {}
    added = []
    for identifier, scope in scopes.items():
        if scope['kind'] != 'cells':
            continue
        step = by_id[identifier]
        if step.get('depends_on') or any(i != scope['root_index'] and not (c.get('property') == 'category' and c.get('value') == 'cell_type')
                                       for i, c in enumerate(step.get('constraints', []))):
            return _recovery(result, 'unrepresented_scope', graph_release)
        if scope['root'] in cell_checks:
            return _recovery(result, 'unrepresented_scope', graph_release)
        cell_checks[scope['root']] = identifier
    used = set(by_id)
    for identifier, scope in scopes.items():
        if scope['root'] in cell_checks:
            continue
        cell_id = identifier + '_cell_groups'
        if cell_id in used:
            return _recovery(result, 'unrepresented_scope', graph_release)
        used.add(cell_id)
        cell_checks[scope['root']] = cell_id
        added.append((cell_id, identifier, scope))
    if len(original) + len(added) > max_steps:
        return _recovery(result, 'step_cap', graph_release)

    def binding(scope):
        return {'entity_type': 'anatomical_structure', 'property': 'id', 'operator': 'IN',
                'value': list(scope['membership']['cell_ids'])}

    def metadata(scope, source):
        return {'version': VERSION, 'digest': DIGEST, 'role': scope['kind'],
                'original_root_constraint': copy.deepcopy(source['constraints'][scope['root_index']]) if scope['root_index'] is not None else copy.deepcopy(scope['resolved'].get('requested')),
                'canonical_cell_binding': binding(scope), 'membership': copy.deepcopy(scope['membership'])}

    def cell_step(identifier, source, scope):
        title = scope['resolved'].get('name') or scope['root']
        return {'id': identifier, 'title': 'Identify recorded cell groups in ' + title,
                'question': 'Find every anatomical_structure cell-group node in the canonical ID set below. Return each cell node and its recorded properties, including groups without marker annotations. Do not require any gene, marker or hierarchy relationship. The set is the complete registered cell hierarchy for ' + title + '.',
                'rationale': 'Keep all recorded cell groups visible, including groups without a marker annotation.',
                'constraints': [binding(scope)], 'relation_types': [], 'depends_on': [], 'complete': True,
                'purpose': 'primary', 'anatomy_scope': {**metadata(scope, source), 'role': 'cells'}}

    replacement = {}
    for identifier, scope in scopes.items():
        source = by_id[identifier]
        if scope['kind'] == 'cells':
            replacement[identifier] = cell_step(identifier, source, scope)
            continue
        marker = copy.deepcopy(source)
        for key in ('resolution_key', 'resolved_entities', 'entity_resolution', 'resolved_constraints'):
            marker.pop(key, None)
        marker['constraints'] = [copy.deepcopy(c) for index, c in enumerate(source.get('constraints', [])) if index != scope['root_index']]
        marker['constraints'].append(binding(scope))
        marker['relation_types'] = ['MARKER_GENE_OF']
        marker['depends_on'] = list(dict.fromkeys([*source.get('depends_on', []), cell_checks[scope['root']]]))
        marker['anatomy_scope'] = metadata(scope, source)
        marker['question'] += '\nThe organ scope is resolved to the canonical cell-group IDs below through the verified anatomical hierarchy. Retrieve Gene to cell MARKER_GENE_OF annotations for that exact set; preserve all other gene and evidence filters. Do not add a direct cell-to-organ hierarchy join, which would remove valid subgroups. No marker-strength or abundance ranking is implied.'
        replacement[identifier] = marker
    insertions = {source_id: cell_step(cell_id, by_id[source_id], scope) for cell_id, source_id, scope in added}
    result['steps'] = []
    for source in original:
        if source['id'] in insertions:
            result['steps'].append(insertions[source['id']])
        result['steps'].append(replacement.get(source['id'], source))
    for group in result.get('display_groups') or []:
        key = 'step_ids' if 'step_ids' in group else 'steps' if 'steps' in group else None
        if key and isinstance(group[key], list) and all(isinstance(item, str) for item in group[key]):
            group[key] = [part for step_id in group[key] for part in ([insertions[step_id]['id'], step_id] if step_id in insertions else [step_id])]
    result['anatomy_scope_normalization'] = {'version': VERSION, 'digest': DIGEST,
        'registry_digest': MEMBERSHIP_DIGEST, 'original_steps': original,
        'original_display_groups': copy.deepcopy(plan.get('display_groups')),
        'cell_check_ids': list(cell_checks.values())}
    return result
