"""Versioned, public-safe diagnostics. Presentation only; never changes eligibility."""
from copy import deepcopy
import json
import re

VERSION = 'diagnostics-v1'
# The family is also the inline flowchart annotation, never a control-flow gate.
REGISTRY = {
 'E01': ('M03/D01', 'Grounding lookup was unavailable. Planning can continue with available context.'),
 'E02': ('M04/D02', 'The planner could not produce a usable plan within its limits.'),
 'E03': ('M04', 'A selected identity could not be matched to retained resolver evidence.'),
 'E04': ('M04 tools/M07', 'An identity, filter, schema rule, or dependency could not be verified.'),
 'E05': ('M06b', 'The query generation service could not produce a candidate.'),
 'E06': ('M07/D8', 'The generated query did not pass executable validation.'),
 'E07': ('M08', 'Database retrieval failed or did not finish completely.'),
 'E08': ('M09', 'Results could not be combined or bound to the next task.'),
 'E09': ('M10/D4', 'The available evidence did not satisfy the readiness checks.'),
 'E10': ('M12', 'Answer generation could not finish.'),
 'E00': ('Unknown', 'The failure has not yet been classified. Use the run reference to investigate.'),
}
EXACT = {
 'entity_choice_requires_resolve_entities': ('E03', 'ENTITY_CHOICE_UNVERIFIED'),
 'entity_choice_not_in_user_request': ('E03', 'ENTITY_MENTION_UNVERIFIED'),
 'invalid_entity_choices': ('E03', 'INVALID_ENTITY_CHOICES'),
 'session_population_requires_connected_queries': ('E04', 'SESSION_POPULATION_BINDING_MISSING'),
 'session_population_role_ambiguous': ('E04', 'SESSION_POPULATION_ROLE_AMBIGUOUS'),
 'session_population_unavailable': ('E08', 'SESSION_POPULATION_UNAVAILABLE'),
 'session_population_stale_or_incomplete': ('E08', 'SESSION_POPULATION_STALE'),
 'reserved_session_population_id': ('E04', 'RESERVED_TASK_ID'),
 'malformed_plan': ('E02', 'MALFORMED_PLAN'), 'malformed_step': ('E02', 'MALFORMED_STEP'),
 'empty_executable_plan': ('E02', 'EMPTY_PLAN'), 'planning_repair_exhausted': ('E02', 'REPAIR_EXHAUSTED'),
 'plan_too_large': ('E02', 'PLAN_LIMIT'), 'invalid_plan_dependencies': ('E04', 'INVALID_DEPENDENCIES'),
 'answer_preparation': ('E10','ANSWER_PREPARATION'), 'answer_output_limit': ('E10','OUTPUT_LIMIT'),
 'invalid_bounded_path_evidence': ('E08','INVALID_PATH_EVIDENCE'),
 'query_generation_failed': ('E05','GENERATION_FAILED'), 'cypher_generation_failed': ('E05','GENERATION_FAILED'),
 'scope_unavailable': ('E04','SCOPE_UNAVAILABLE'),
 'preparation_failed': ('E04', 'PREPARATION_FAILED'), 'dependency_unavailable': ('E08', 'DEPENDENCY_UNAVAILABLE'),
 'query_validation': ('E06', 'QUERY_VALIDATION'), 'graph_release_mismatch': ('E07', 'RELEASE_MISMATCH'),
 'graph_identity': ('E07', 'GRAPH_IDENTITY'), 'planning_failure': ('E02', 'PLANNING_FAILED'),
 'stage_inventory_unavailable': ('E01', 'INVENTORY_UNAVAILABLE'),
}
STAGES = {'planning':'E02', 'resolving_entities':'E04', 'preparing_execution':'E04',
 'preparing_preview':'E09', 'generating_cypher':'E05', 'validating':'E06',
 'querying_graph':'E07', 'writing_answer':'E10', 'combining':'E08'}
SAFE_REASONS = {'timeout','rate_limited','budget_exhausted','authentication','authorization','billing',
 'unavailable','failed','partial','query_failed','query_generation_failed','cypher_generation_failed',
 'run_graph_materialization_limit','retrieval_incomplete','not_found','ambiguous','scope_unavailable'}


def _schema_reasons():
    from .agent_schemas import module
    return module('validation_repair')['diagnostic_reasons']


def diagnostic(reason, stage=None, *, blocking=True, run_id=None, step_id=None):
    raw = reason if isinstance(reason, str) else ''
    if raw.startswith('{'):
        try: raw = json.loads(raw).get('category', '')
        except (ValueError, AttributeError): raw = ''
    if raw.startswith('E01 preparation service unavailable'):
        family, name, safe = 'E04', 'PREPARATION_UNAVAILABLE', 'preparation_unavailable'
    elif any(raw.startswith(prefix + ':') for prefix in _schema_reasons()):
        prefix = next(p for p in _schema_reasons() if raw.startswith(p + ':'))
        family, name, safe = 'E04', _schema_reasons()[prefix], prefix
    elif raw.split(':', 1)[0] in EXACT:
        safe = raw.split(':', 1)[0]; family, name = EXACT[safe]
    elif raw in SAFE_REASONS:
        family, name, safe = STAGES.get(stage, 'E00'), raw.upper(), raw
    elif raw.startswith(('missing_', 'unrequested_', 'unsupported_', 'scope_', 'dependency_', 'path_', 'chain_')):
        family = 'E04' if stage == 'planning' else STAGES.get(stage, 'E04')
        name, safe = 'VALIDATION_FAILED', 'validation_failed'
    else:
        family, name, safe = 'E00', 'UNCLASSIFIED', 'unclassified'
    if family == 'E01': blocking = False
    module, message = REGISTRY[family]
    out = {'version':VERSION, 'code':family+'.'+name, 'module':module, 'message':message,
           'reason':safe, 'severity':'error' if blocking else 'warning', 'blocking':blocking}
    for key, value in [('run_id',run_id), ('step_id',step_id)]:
        if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value): out[key]=value
    return out


def annotate(value, context=None):
    """Project legacy and new snapshots/events without rewriting stored history."""
    if not isinstance(value, (dict,list)): return value
    context = context if isinstance(context,dict) else {}
    run_id = context.get('run_id')
    default_stage = context.get('stage')
    def visit(item, stage=default_stage):
        if isinstance(item,list): return [visit(x,stage) for x in item]
        if not isinstance(item,dict): return item
        out={k:visit(v,stage) for k,v in item.items() if k not in {'diagnostics','diagnostic_history'}}
        here=item.get('stage') or ('querying_graph' if item.get('step_id') else stage)
        found=[]
        for advice in item.get('identity_selection_diagnostics', []):
            if isinstance(advice, dict) and advice.get('reason') == 'entity_choice_requires_resolve_entities':
                found.append(diagnostic(advice['reason'], 'planning', blocking=False, run_id=run_id))
        if item.get('proposal_issue'):
            found.append(diagnostic(item['proposal_issue'],'planning',run_id=run_id))
            # Do not publish serialized compiler records or arbitrary exceptions.
            out['proposal_issue']=found[-1]['reason']
        if item.get('category') in EXACT or item.get('category') in SAFE_REASONS:
            found.append(diagnostic(item['category'],here,run_id=run_id))
        error=item.get('error')
        if isinstance(error,dict) and error.get('category') and not found:
            found.append(diagnostic(error['category'],error.get('stage') or here,run_id=run_id,step_id=item.get('step_id')))
        if item.get('diagnostic')=='E01':
            found.append(diagnostic('stage_inventory_unavailable',here,blocking=False,run_id=run_id))
        if item.get('status') in {'failed','blocked','partial'} and item.get('step_id') and not found:
            checks=item.get('validation') or []
            reasons=[r for c in checks if isinstance(c,dict) for r in c.get('reasons',[]) if isinstance(r,str)]
            reason=reasons[-1] if reasons else 'retrieval_incomplete' if item.get('status')=='partial' else 'failed'
            stage_hint='validating' if reasons else 'querying_graph'
            found.append(diagnostic(reason,stage_hint,run_id=run_id,step_id=item['step_id']))
        for key in ('plan','preview','evidence','error','synthesis_error','recovery','payload'):
            child=out.get(key)
            if isinstance(child,dict): found.extend(child.get('diagnostics',[]))
        for child in out.get('steps',[]) if isinstance(out.get('steps'),list) else []:
            if isinstance(child,dict): found.extend(child.get('diagnostics',[]))
        route=item.get('planning_route') or {}
        attempts={k:route[k] for k in ('claude_calls','lookup_batches') if isinstance(route.get(k),int)}
        # Prefer originating plan/step cause over a generic outer recovery category.
        specific=[d for d in found if d['code'] not in {'E02.PLANNING_FAILED','E00.UNCLASSIFIED'}]
        if specific: found=specific
        unique={}
        for d in found:
            d=deepcopy(d)
            if attempts:d['attempts']=attempts
            if item.get('proposal_issue') and route.get('claude_calls')==3:
                d['outcome']='planning_repair_exhausted'
            unique[(d['code'],d.get('step_id'))]=d
        if unique: out['diagnostics']=list(unique.values())
        if item.get('diagnostic_history'):
            out['diagnostic_history']=[{'attempt':h.get('attempt'), **diagnostic(h.get('reason'),'planning',run_id=run_id)}
                for h in item['diagnostic_history'] if isinstance(h,dict)][:3]
        return out
    result = visit(value)
    if isinstance(result, dict) and value.get('category') == 'planning_failure' and (context.get('plan') or {}).get('proposal_issue'):
        result['diagnostics'] = visit(context['plan']).get('diagnostics', [])
    return result
