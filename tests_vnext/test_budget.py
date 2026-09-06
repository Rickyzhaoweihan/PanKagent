from concurrent.futures import ThreadPoolExecutor
from pankagent_vnext.budget import Budget,BudgetExceeded

def test_reservations_are_atomic_and_persist(tmp_path):
    path=tmp_path/'budget.sqlite3'
    def reserve(_):
        try: return Budget(path,.10).reserve('claude-sonnet-5','test',16000,1000)
        except BudgetExceeded: return None
    Budget(path,.10)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids=list(pool.map(reserve,range(8)))
    assert len([x for x in ids if x])==2
    snap=Budget(path,.10).snapshot()
    assert snap['remaining_usd']==0 and snap['pending_calls']==2

def test_actual_usage_includes_cache_and_releases_difference(tmp_path):
    b=Budget(tmp_path/'budget.sqlite3',1)
    rid=b.reserve('claude-sonnet-5','test',10000,1000)
    b.settle(rid,{'input_tokens':1000,'output_tokens':100,'cache_creation_input_tokens':1000,'cache_read_input_tokens':1000})
    s=b.snapshot()
    assert s['spent_usd']==.0057
    assert s['pending_calls']==0
