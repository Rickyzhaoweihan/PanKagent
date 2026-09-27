"""Task-local result checks; optional evidence never changes membership validity."""
from .agent_schemas import module


def assess(step, result):
    reasons=[]
    execution=result.get('retrieval_execution') or {}
    checks=module('validation').get('result_checks', [])
    if 'completed_execution' in checks and execution.get('completed') is not True:
        reasons.append('result_execution_unverified')
    if 'materialized_endpoints' in checks:
        ids={str(n.get('id')) for n in result.get('nodes') or [] if n.get('id') is not None}
        for edge in result.get('edges') or []:
            if any(str(edge.get(k)) not in ids for k in ('start_id','end_id')):
                reasons.append('result_missing_edge_endpoint');break
    partial=(result.get('truncated') is True or execution.get('cursor_exhausted') is not True
             or result.get('status')=='partial')
    return {'valid':not reasons,'reasons':reasons,'complete':not reasons and not partial,
            'empty':not reasons and not partial and result.get('status')=='empty',
            'scope':'approved_task','optional_annotations_affect_membership':False}
