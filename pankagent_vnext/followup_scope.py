"""Carry a uniquely requested gene into referential follow-ups for re-resolution."""
import re

VERSION = 'followup-gene-scope-v1'


def grounding_question(question, prior):
    if not prior or not re.search(r'\b(?:those|that|these|it|now|earlier|previous|focus|restrict)\b', question, re.I):
        return question
    # An explicit new gene/variant owns its own scope. Do not pull a former
    # subject into 'What about gene CFTR?' or a request for a new named locus.
    if re.search(r'\bgene\s+[A-Z][A-Z0-9-]+\b|\brs\d+\b|\b[A-Z][A-Z0-9]{2,}\b', re.sub(r'\b(?:GO|T1D|RNA|ATAC|GWAS|QTL|HIRN)\b','',question)):
        return question
    anchors=set()
    for step in (prior.get('plan') or {}).get('steps',[]):
        for binding in step.get('constraints',[]):
            if binding.get('entity_type')=='Gene' and binding.get('property') in {'id','name'} and binding.get('operator','=')=='=' and isinstance(binding.get('value'),str):
                anchors.add(binding['value'])
    if len(anchors)!=1: return question
    return question + '\nRetain the previously requested gene: ' + next(iter(anchors)) + '.'


def preserve_original_sources(plan, question, prior):
    """Honor explicit source-preserving follow-ups before any graph execution.

    A planner-added evidence category is not part of a request to reinterpret
    the previously retrieved sources. Exact source/version bindings come from
    those records, not from model-invented aliases.
    """
    from copy import deepcopy
    if not prior or not re.search(r'\bkeep (?:the )?original sources\b', question, re.I):
        return plan
    result = deepcopy(plan)
    previous = (prior.get('plan') or {}).get('steps', [])
    allowed = {r for s in previous for r in s.get('relation_types', [])}
    kept, rejected = [], []
    for step in result.get('steps', []):
        relations = step.get('relation_types', [])
        if not relations or not set(relations) <= allowed:
            rejected.append(step['id'])
            continue
        if len(relations) != 1:
            raise ValueError('original_source_scope_requires_single_relation')
        relation = relations[0]
        records = [e for s in (prior.get('evidence') or {}).get('steps', [])
                   for e in s.get('edges', []) if e.get('type') == relation]
        for prop in ('data_source', 'data_version'):
            values = {str((e.get('properties') or {})[prop]) for e in records
                      if (e.get('properties') or {}).get(prop) is not None}
            if len(values) > 1:
                raise ValueError('original_source_scope_ambiguous')
            if not values:
                continue
            value = next(iter(values))
            existing = [c for c in step.get('constraints', []) if c.get('property') == prop]
            if any(c.get('operator', '=') != '=' or str(c.get('value')) != value for c in existing):
                raise ValueError('changed_original_source_scope')
            if not existing:
                step.setdefault('constraints', []).append({'entity_type':None,
                    'owner_kind':'relationship', 'relationship_type':relation,
                    'property':prop, 'operator':'=', 'value':value})
        kept.append(step)
    if not kept or any(set(s.get('depends_on', [])) & set(rejected) for s in kept):
        raise ValueError('original_source_scope_unavailable')
    result['steps'] = kept
    result['followup_source_scope'] = {'version':'original-sources-v1',
        'source_run_id':prior['run_id'], 'rejected_unrequested_step_ids':rejected}
    return result
