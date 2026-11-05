import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]/'deploy_native_agent/source/native'
spec = importlib.util.spec_from_file_location('release_dispatch', ROOT/'release_dispatch.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

def route(path, method='GET', payload=None):
    selected=[]
    async def old(scope, receive, send):
        selected.append(('old', (await receive())['body']))
    async def new(scope, receive, send):
        selected.append(('new', (await receive())['body']))
    legacy=SimpleNamespace(runs={'simple-qwen-old':{'plan_id':'plan-qwen-old'}},requests={'old-request':{}})
    app=module.ReleaseDispatch(old,new,legacy)
    body=json.dumps(payload).encode() if payload is not None else b''
    async def receive():return {'type':'http.request','body':body,'more_body':False}
    asyncio.run(app({'type':'http','path':path,'method':method},receive,None))
    assert selected[0][1]==body
    return selected[0][0]

def test_old_saved_result_and_pending_confirmation_remain_old():
    assert route('/results/simple-qwen-old:final')=='old'
    assert route('/results','POST',{'run_id':'simple-qwen-old'})=='old'

def test_old_request_replay_and_cancellation_remain_old():
    assert route('/agent/v2/requests/old-request/cancel','POST')=='old'
    assert route('/agent/v2/plans','POST',{'request_id':'old-request'})=='old'

def test_new_question_and_results_use_new_release():
    assert route('/agent/v2/plans','POST',{'question':'INS'})=='new'
    assert route('/results/simple-qwen-new:final')=='new'
    assert route('/agent/v2/requests/new-request')=='new'

def test_historical_confirmation_cannot_query_retired_release():
    sent=[]
    async def forbidden(*args): raise AssertionError('Retired workflow executed')
    legacy=SimpleNamespace(runs={'simple-qwen-old':{'plan_id':'plan-qwen-old'}},requests={})
    async def receive():return {'type':'http.request','body':b'{}','more_body':False}
    async def send(message):sent.append(message)
    asyncio.run(module.ReleaseDispatch(forbidden,forbidden,legacy)(
        {'type':'http','method':'POST','path':'/agent/v2/plans/plan-qwen-old/confirm'},receive,send))
    assert sent[0]['status']==409
