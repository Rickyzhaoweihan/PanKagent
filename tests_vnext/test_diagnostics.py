from copy import deepcopy
from pankagent_vnext.diagnostics import annotate, diagnostic
from pankagent_vnext.app import public_payload


def failed():
    return {'run_id':'run-1','status':'failed','stage':'planning','plan':{
        'steps':[], 'proposal_issue':'entity_choice_requires_resolve_entities',
        'planning_route':{'claude_calls':3,'lookup_batches':1},
        'recovery':{'category':'planning_failure','message':'Generic','retryable':True}},
        'error':{'category':'planning_failure','message':'Generic'}}


def test_historical_cftr_cause_not_hidden_by_exhaustion():
    value=failed();before=deepcopy(value);out=public_payload(value,context=value)
    d=out['diagnostics'][0]
    assert d['code']=='E03.ENTITY_CHOICE_UNVERIFIED'
    assert d['reason']=='entity_choice_requires_resolve_entities'
    assert d['outcome']=='planning_repair_exhausted'
    assert d['attempts']=={'claude_calls':3,'lookup_batches':1}
    assert d['run_id']=='run-1' and value==before
    assert out['status']==value['status']
    assert {k:out['plan']['recovery'][k] for k in value['plan']['recovery']}==value['plan']['recovery']


def test_event_replay_and_snapshot_have_same_cause():
    value=failed()
    event={'type':'plan','payload':value['plan']}
    assert public_payload(event,context=value)['diagnostics']==public_payload(value,context=value)['diagnostics']


def test_raw_exception_and_serialized_compiler_details_not_exposed():
    value=failed();value['plan']['proposal_issue']='{"category":"preparation_failed","secret":"password"}'
    out=annotate(value,value)
    assert 'password' not in str(out)
    assert out['diagnostics'][0]['code']=='E04.PREPARATION_FAILED'
    assert diagnostic('secret password key=123')['reason']=='unclassified'


def test_nonblocking_grounding_and_partial_evidence():
    assert not annotate({'diagnostic':'E01','status':'unavailable'})['diagnostics'][0]['blocking']
    value={'status':'partial','evidence':{'steps':[{'step_id':'q1','status':'failed','error':{'category':'timeout'}}]}}
    out=annotate(value,{**value,'stage':'querying_graph'})
    assert out['status']=='partial' and out['diagnostics'][0]['code']=='E07.TIMEOUT'


def test_safe_history_and_unknown():
    value={'diagnostic_history':[{'attempt':1,'reason':'entity_choice_requires_resolve_entities','secret':'key'}]}
    assert 'secret' not in str(annotate(value))
    assert diagnostic('unrecognized failure with private path')['code']=='E00.UNCLASSIFIED'
