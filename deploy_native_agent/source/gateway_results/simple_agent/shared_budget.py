"""Use the existing dev ledger; never reset its allowance or reservations."""
import json
import os
import sqlite3
import sys
from pathlib import Path
from contextlib import closing

def reserve(payload, env, purpose):
    if os.environ.get('SIMPLE_AGENT_SHARED_BUDGET') != '1':return None
    from .features import DEV_RELEASE
    sys.path.insert(0, DEV_RELEASE)
    from pankagent_vnext.budget import Budget, PRICES
    model=payload['model']
    if model == 'claude-haiku-5-5':PRICES[model]=(.5,2.5)
    elif model == 'claude-sonnet-5-5':PRICES[model]=(2,10)
    path=Path(env.get('PANK_VNEXT_BUDGET_DIR') or env['PANK_VNEXT_STATE_DIR'])/'budget.sqlite3'
    budget=Budget(path,float(env.get('PANK_VNEXT_BUDGET_USD','10')))
    bound=len(json.dumps(payload,ensure_ascii=False).encode())+12000
    rid=budget.reserve(model,purpose,bound,payload['max_tokens'])
    return {'id':rid,'path':str(path),'model':model}

def settle(record, usage):
    if record is None:return
    if not isinstance(usage,dict) or 'input_tokens' not in usage or 'output_tokens' not in usage:return
    total=sum(usage.get(k,0) for k in ('input_tokens','cache_creation_input_tokens','cache_read_input_tokens'))
    haiku=record['model']=='claude-haiku-5-5'
    ip,op,read=(.1,.5,.01) if haiku and total<=100000 else (.5,2.5,.05) if haiku else (2,10,.1)
    long=usage.get('cache_creation',{}).get('ephemeral_1h_input_tokens',0)
    short=usage.get('cache_creation_input_tokens',0)-long
    actual=(usage.get('input_tokens',0)*ip+usage.get('output_tokens',0)*op+short*ip*1.25+long*ip*2+usage.get('cache_read_input_tokens',0)*read)/1e6
    with closing(sqlite3.connect(record['path'],timeout=10)) as db:
        with db:db.execute('UPDATE usage SET actual=?,tokens=? WHERE id=?',(actual,json.dumps(usage),record['id']))
