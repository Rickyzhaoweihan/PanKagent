"""Reject detectable scope loss in explicit additive revisions before retrieval.

This guards recorded filters/types/completeness, not every natural-language meaning.
"""
from copy import deepcopy
import json
import re


def additive_instruction(instruction):
    text=instruction.strip()
    return bool(re.match(r'^(?:please\s+)?(?:add\b|also\s+(?:include|check|show)\b|(?:did|have)\s+you\s+(?:also\s+)?(?:add|include)\b)',text,re.I)) and not re.search(r'\b(?:instead|replace|remove|drop|only|exclude|switch)\b',text,re.I)


def preserve_additive_scope(plan, parent, instruction):
    if not additive_instruction(instruction) or plan.get('clarification'):
        return plan
    aliases={}
    for step in parent.get('steps',[]):
        for e in step.get('resolved_entities',[]):
            if e.get('state')=='resolved':
                for prop in ('id','name'):
                    if e.get(prop):aliases[(prop,e[prop])]=(e.get('entity_type'),e.get('id'))
    def signature(c):
        prop, value=c.get('property'),c.get('value')
        if c.get('operator','=')=='=' and isinstance(value,str) and (prop,value) in aliases:
            return ('verified_entity',*aliases[(prop,value)])
        if c.get('operator')=='IN' and isinstance(value,str):
            try:value=sorted(json.loads(value))
            except (ValueError,TypeError):pass
        return (prop,c.get('operator','='),json.dumps(value,sort_keys=True))
    lost=[]
    for old in parent.get('steps',[]):
        if old.get('purpose')=='context':continue
        required={signature(c) for c in old.get('constraints',[])}
        for kind in old.get('relation_types',[]) or [None]:
            matches=[s for s in plan.get('steps',[]) if (kind is None or kind in s.get('relation_types',[]))
                     and required <= {signature(c) for c in s.get('constraints',[])}
                     and (not old.get('complete',True) or s.get('complete',True))
                     and s.get('depends_on',[])==old.get('depends_on',[])]
            if not matches:lost.append({'step_id':old['id'],'relation_type':kind})
    if not lost:return plan
    result=deepcopy(parent)
    result.update(literature=plan.get('literature',parent.get('literature')), literature_intent=plan.get('literature_intent',parent.get('literature_intent')),
        clarification='The requested addition would replace or narrow earlier checks. The previous plan and preview are retained. Please specify which checks to keep or replace within the three-step limit.',
        proposal_issue='additive_revision_scope_loss', retained_previous_plan=True,
        revision_validation={'valid':False,'category':'additive_revision_scope_loss','affected_checks':lost})
    return result
