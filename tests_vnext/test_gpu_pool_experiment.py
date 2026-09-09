"""Offline admission and persistent request ceilings for the GPU-only audit."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest

HARNESS=Path(__file__).resolve().parents[2]/'docs/pankagent-vnext/grounded-planning-2026-09-09/run_gpu_pool.py'
if not HARNESS.exists():
    pytest.skip('Evaluation harness is tracked by project home',allow_module_level=True)
spec=importlib.util.spec_from_file_location('gpu_pool_audit',HARNESS);audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)


def test_frozen_crossover_respects_seventy_two_request_cap():
    m=audit.load_manifest(HARNESS.with_name('gpu-pool.manifest.frozen.json'))
    assert len(m['cases'])*sum(sum(r['order']) for r in m['rounds'])==72
    assert m['rounds']==[{'id':'A','order':[2,4]},{'id':'B','order':[4,2]}]
    assert m['claude_calls']==0
    assert all(c['references'][0]['frozen_rows_sha256'] for c in m['cases'])
    assert all(not any(x.get('entity_type') in {'donor','Sample_node'} for x in c['step']['constraints']) for c in m['cases'])


def test_requests_are_not_refunded_by_reopening_or_failure(tmp_path):
    db=tmp_path/'request_budget.sqlite3'
    for i in range(72):audit.reserve_request(db,str(i),'public question')
    with pytest.raises(RuntimeError,match='exhausted'):
        audit.reserve_request(db,'again','public question')
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM requests').fetchone()[0]==72
        assert conn.execute('SELECT prompt_sha FROM requests LIMIT 1').fetchone()[0]!='public question'


def test_duplicate_run_identity_cannot_consume_untracked_request(tmp_path):
    db=tmp_path/'requests.sqlite3';audit.reserve_request(db,'A:2:P01:0','question')
    with pytest.raises(sqlite3.IntegrityError):audit.reserve_request(db,'A:2:P01:0','question')
    with sqlite3.connect(db) as conn:assert conn.execute('SELECT count(*) FROM requests').fetchone()[0]==1
