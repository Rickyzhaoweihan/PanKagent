"""The legacy terminal popup must not turn a revision fragment into a new goal."""
import asyncio
import json
from copy import deepcopy

import pytest

from pankagent_vnext.store import Store
from pankagent_vnext.terminal_retry import RETRY_INSTRUCTION
from test_runtime import PLAN, service

ORIGINAL = 'Count HPAP stage 3 RNA samples in spleen.'
INSTRUCTION = 'Use PLN instead; retain stage 3 and RNA assays.'
RECOVERY = {'category': 'query_validation', 'message': 'The generated query could not be validated.'}


def failed_revision(store, status='failed', recovery=None):
    first = store.create(ORIGINAL)
    store.update(first['run_id'], status='awaiting_confirmation', plan=deepcopy(PLAN))
    _, revision = store.revise(first['plan_id'], INSTRUCTION,
        audit={'revision_instruction': INSTRUCTION, 'revision_mode': 'instruction'})
    plan = deepcopy(PLAN)
    plan['interpreted_question'] = 'Count HPAP stage 3 RNA samples in PLN.'
    plan['steps'][0]['constraints'] = [{'property': 'stage', 'value': '3'}]
    if recovery:
        plan['recovery'] = recovery
    return store.update(revision['run_id'], status=status, plan=plan)


def submit(store, previous, text=None, include_context=True):
    question = previous['question'] if text is None else text
    return store.create(question.strip(), previous['session_id'], include_context=include_context,
        audit={'original_question': question, 'source': 'user'}, legacy_retry_submission=question)


def test_exact_retry_keeps_durable_scope_across_restart(tmp_path):
    store = Store(tmp_path)
    prior = failed_revision(store)
    prior_audit = store.audit_metadata(prior['run_id'])
    store.close()
    store = Store(tmp_path)
    retry = submit(store, prior)
    audit = store.audit_metadata(retry['run_id'])
    assert retry['question'] == INSTRUCTION
    assert audit['original_question'] == ORIGINAL
    assert audit['parent_run_id'] == audit['retry_of_run_id'] == prior['run_id']
    assert audit['parent_plan_id'] == prior['plan_id']
    assert audit['revision_instruction'] == RETRY_INSTRUCTION
    assert audit['revision_mode'] == 'instruction'
    assert audit['retry_prior_revision_instruction'] == INSTRUCTION
    assert audit['retry_submitted_text'] == INSTRUCTION
    assert audit['prior_plan_sha256'] == Store.content_hash(prior['plan'])
    assert store.get(prior['run_id']) == prior
    assert store.audit_metadata(prior['run_id']) == prior_audit
    store.close()


def test_changed_retry_uses_only_exact_suffix(tmp_path):
    store = Store(tmp_path)
    prior = failed_revision(store)
    change = '  Switch to stage 1; keep PLN and RNA.  '
    exact = INSTRUCTION + '\nRequested change: ' + change
    retry = submit(store, prior, exact)
    audit = store.audit_metadata(retry['run_id'])
    assert retry['question'] == exact.strip()
    assert audit['revision_instruction'] == change
    assert audit['retry_submitted_text'] == exact
    assert audit['retry_kind'] == 'retry_with_change'
    assert audit['original_question'] == ORIGINAL
    store.close()


@pytest.mark.parametrize('status,recovery,expected', [
    ('failed', None, True), ('interrupted', None, True),
    ('partial', RECOVERY, True), ('partial', None, False),
    ('partial', {'category': 'query_validation'}, False),
    ('completed', RECOVERY, False), ('cancelled', RECOVERY, False),
    ('superseded', RECOVERY, False), ('planning', RECOVERY, False),
])
def test_only_failed_terminal_revision_eligible(tmp_path, status, recovery, expected):
    store = Store(tmp_path)
    prior = failed_revision(store, status, recovery)
    retry = submit(store, prior)
    audit = store.audit_metadata(retry['run_id'])
    assert bool(audit.get('retry_of_run_id')) == expected
    if not expected:
        assert audit['parent_run_id'] is None and audit['original_question'] == INSTRUCTION
    store.close()


@pytest.mark.parametrize('changed', ['Show CFTR enrichment.', 'Use PLN', INSTRUCTION + ' ',
    INSTRUCTION + '\nRequested change: ', INSTRUCTION + '\nRequested change:   '])
def test_different_or_nonexact_question_not_reinterpreted(tmp_path, changed):
    store = Store(tmp_path)
    prior = failed_revision(store)
    retry = submit(store, prior, changed)
    audit = store.audit_metadata(retry['run_id'])
    assert audit['parent_run_id'] is None
    assert audit['original_question'] == changed
    assert audit.get('retry_of_run_id') is None
    store.close()


def test_no_context_or_newer_run_does_not_search_older_failure(tmp_path):
    store = Store(tmp_path)
    prior = failed_revision(store)
    unrelated = submit(store, prior, include_context=False)
    assert store.audit_metadata(unrelated['run_id'])['parent_run_id'] is None
    assert store.latest_run(prior['session_id'])['run_id'] == unrelated['run_id']
    store.update(unrelated['run_id'], status='cancelled')
    retry = submit(store, prior)
    assert store.audit_metadata(retry['run_id'])['parent_run_id'] is None
    store.close()


@pytest.mark.parametrize('missing', ['plan', 'metadata', 'original_question', 'parent'])
def test_missing_history_not_invented(tmp_path, missing):
    store = Store(tmp_path)
    prior = failed_revision(store)
    if missing == 'plan':
        store.update(prior['run_id'], plan=None)
    elif missing == 'metadata':
        store.db.execute('DELETE FROM run_audit WHERE run_id=?', (prior['run_id'],)); store.db.commit()
    else:
        audit = store.audit_metadata(prior['run_id'])
        audit['original_question' if missing == 'original_question' else 'parent_run_id'] = None
        store.db.execute('UPDATE run_audit SET metadata=? WHERE run_id=?', (json.dumps(audit), prior['run_id']))
        store.db.commit()
    retry = submit(store, prior)
    assert store.audit_metadata(retry['run_id'])['parent_run_id'] is None
    store.close()


def test_root_failure_remains_plain_new_question(tmp_path):
    store = Store(tmp_path)
    prior = store.create(ORIGINAL)
    store.update(prior['run_id'], status='failed', plan=deepcopy(PLAN))
    retry = submit(store, prior)
    assert retry['question'] == ORIGINAL
    assert store.audit_metadata(retry['run_id'])['parent_run_id'] is None
    store.close()


def test_plan_api_wires_adapter_and_preserves_planning_context(tmp_path):
    async def scenario():
        async with service(tmp_path) as (client, runtime, *_):
            prior = failed_revision(runtime.store)
            # Inspect wiring without initiating planning, graph or model calls.
            runtime.launch = lambda run_id, coroutine: coroutine.close()
            response = await client.post('/v2/plans', json={'session_id': prior['session_id'], 'question': INSTRUCTION})
            assert response.status_code == 202
            retry = runtime.store.get(response.json()['run_id'])
            context = runtime.planning_history(retry)[-1]['revision_context']
            assert context['original_question'] == ORIGINAL
            assert context['parent_plan']['interpreted_question'] == prior['plan']['interpreted_question']
            assert context['instruction'] == RETRY_INSTRUCTION
            assert context['previous_revision_instruction'] == INSTRUCTION
            assert context['retry_of_run_id'] == prior['run_id']
            assert context['parent_plan']['steps'][0]['constraints'] == prior['plan']['steps'][0]['constraints']
    asyncio.run(scenario())
