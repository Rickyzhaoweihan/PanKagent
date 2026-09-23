"""Graph-only evidence selection. Never used by synthesis or computation."""
from copy import deepcopy

from .evidence_identity import evidence_id

VERSION = 'viewer-evidence-v1'
GOOD = {'complete', 'empty', 'partial'}


def select_evidence(run, phase):
    source = (run.get('preview') or {}).get('evidence') if phase == 'preview' else run.get('evidence')
    if not isinstance(source, dict):
        raise ValueError('evidence_not_ready')
    plan = run.get('plan') or {}
    targets = plan.get('answer_step_ids')
    specifications = {s.get('id'): s for s in plan.get('steps', [])}
    selected, omitted = [], []
    for index, step in enumerate(source.get('steps', [])):
        sid = step.get('step_id')
        if targets is not None and sid not in targets or step.get('status') not in GOOD:
            omitted.append({'step_id': sid, 'status': step.get('status')})
            continue
        # Only canonical backend records are eligible, never formatter samples.
        if step.get('context_compaction') or step.get('answer_evidence_scope'):
            raise ValueError('formatter_projection_is_not_viewer_evidence')
        if step.get('graph_version') != source.get('graph_version'):
            raise ValueError('graph_release_mismatch')
        keys = ('step_id', 'question', 'title', 'status', 'graph_version', 'nodes', 'edges',
                'path_records', 'rows', 'provenance', 'truncated', 'retrieval_execution',
                'requested_scope', 'donor_summary', 'aggregate_record_counts',
                'combination_summary', 'derivation', 'graph_identity_membership')
        item = {k: deepcopy(step[k]) for k in keys if k in step}
        item['evidence_id'] = evidence_id(step, index)
        item['depends_on'] = deepcopy(specifications.get(sid, {}).get('depends_on', []))
        # Query identity and provenance are retained without sending SQL/Cypher or
        # generation traces to the renderer. Those stay in private run history.
        item['query_provenance'] = {'step_id': sid, 'evidence_id': item['evidence_id'],
                                    'route': step.get('query_route'), 'query_count': len(step.get('queries', []))}
        if not item.get('nodes') and step.get('status') != 'empty' and not item.get('graph_identity_membership'):
            item['annotation_lookup'] = {'status': 'unavailable',
                'reason': 'membership_not_recorded',
                'recommended_action': 'Retrieve typed membership using the original query predicates.'}
        selected.append(item)
    missing = set(targets or []) - {s.get('step_id') for s in selected}
    partial = bool(missing or omitted and targets is None or any(
        s.get('status') == 'partial' or s.get('truncated') for s in selected))
    result = {'graph_version': source.get('graph_version'), 'steps': selected,
              'nodes': [], 'edges': [], 'truncated': partial,
              'completeness': 'partial' if partial else 'complete',
              'viewer': {'version': VERSION, 'execution_mode': plan.get('execution_mode'),
                         'answer_step_ids': targets, 'omitted_steps': omitted,
                         'missing_answer_step_ids': sorted(missing),
                         'membership_source': 'canonical_backend_results'}}
    return rebuild(result)


def rebuild(result):
    nodes, edges = {}, {}
    import json
    for step in result['steps']:
        for node in step.get('nodes', []):
            nodes.setdefault(str(node['id']), node)
        allowed = {str(n['id']) for n in step.get('nodes', [])}
        step['edges'] = [e for e in step.get('edges', [])
                         if str(e.get('start_id')) in allowed and str(e.get('end_id')) in allowed]
        for edge in step['edges']:
            edges.setdefault(json.dumps(edge, sort_keys=True), edge)
    result.update(nodes=list(nodes.values()), edges=list(edges.values()))
    unavailable = any(s.get('annotation_lookup', {}).get('status') in {'partial', 'unavailable'}
                      for s in result['steps'])
    if unavailable:
        result['completeness'] = 'partial'
    result['viewer']['status'] = 'available' if nodes else 'unavailable' if unavailable or result['completeness'] == 'partial' else 'empty'
    result['viewer']['retrieved_node_count'] = len(nodes)
    result['viewer']['retrieved_edge_count'] = len(edges)
    if nodes and result['completeness'] == 'empty':
        result['completeness'] = 'complete'
    if not nodes and result['completeness'] == 'complete':
        result['completeness'] = 'empty'
    return result


async def prepare_evidence(run, phase, graph):
    result = select_evidence(run, phase)
    for step in result['steps']:
        membership = step.pop('graph_identity_membership', None)
        if not membership:
            continue
        if (membership.get('graph_version') != result['graph_version']
                or membership.get('sampled') is not False
                or not isinstance(membership.get('typed_ids'), list)):
            raise ValueError('unverified_graph_identity_membership')
        requested = {(str(n['id']), n['entity_type']) for n in membership['typed_ids']}
        present = {(str(n['id']), label) for n in step.get('nodes', []) for label in n.get('labels', [])
                   if n.get('properties')}
        missing = requested - present
        if missing:
            try:
                hydrated = await graph.hydrate_viewer_ids(result['graph_version'], sorted(missing))
                received = {(str(n['id']), label) for n in hydrated['nodes'] for label in n.get('labels', [])}
                allowed = [n for n in hydrated['nodes'] if any((str(n['id']), label) in missing for label in n.get('labels', []))]
                replacements = {str(n['id']): n for n in allowed}
                for node in step.get('nodes', []):
                    replacements.setdefault(str(node['id']), node)
                step['nodes'] = list(replacements.values())
                step['annotation_lookup'] = {'status': 'partial' if missing - received or hydrated.get('truncated') else 'complete',
                    'requested_count': len(missing), 'missing_count': len(missing - received)}
            except Exception:
                step['annotation_lookup'] = {'status': 'unavailable', 'requested_count': len(missing)}
            if step['annotation_lookup']['status'] != 'complete':
                result['completeness'] = 'partial'; result['truncated'] = True
        if membership.get('complete') is not True:
            result['completeness'] = 'partial'; result['truncated'] = True
    return rebuild(result)
