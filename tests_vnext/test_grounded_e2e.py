"""Network-free checks for fresh lifecycle evaluation and truthful accounting."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

HARNESS = Path(__file__).resolve().parents[2] / 'docs/pankagent-vnext/grounded-planning-2026-09-09/run_fresh_e2e.py'
if not HARNESS.is_file():
    pytest.skip('Project-home evaluation harness is separately tracked', allow_module_level=True)
spec = importlib.util.spec_from_file_location('fresh_e2e', HARNESS)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_frozen_manifest_enters_gateway_with_questions_not_injected_plans():
    manifest = audit.load_manifest(HARNESS.with_name('manifest.frozen.json'))
    assert manifest['sampling']['unique_tasks'] == 16
    assert manifest['sampling']['fresh_initial_questions'] == 18
    assert manifest['sampling']['revision_instructions'] == 2
    assert all('plan' not in case for case in manifest['cases'])
    assert any(case['id'] == 'R01' and case['repeat'] == 3 for case in manifest['cases'])
    assert {c['expected_outcome'] for c in manifest['cases']} == {'supported', 'zero_match', 'unresolved'}


def test_manifest_tampering_fails(tmp_path):
    path = tmp_path / 'manifest.frozen.json'
    path.write_text('{"graph_release":"test"}')
    path.with_suffix('.sha256').write_text('0' * 64)
    with pytest.raises(ValueError, match='hash differs'):
        audit.load_manifest(path)


def test_reserved_guard_reads_only_questions_and_explicit_instructions(tmp_path):
    db_path = tmp_path / 'sessions.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE runs (run_id TEXT,question TEXT)')
        db.execute('CREATE TABLE run_audit (run_id TEXT,metadata TEXT)')
        db.execute('INSERT INTO runs VALUES (?,?)', ('held', 'Do not use this question'))
        db.execute('INSERT INTO run_audit VALUES (?,?)', ('held', json.dumps({'revision_instruction': 'Keep only this cell'})))
    excluded = tmp_path / 'excluded.json'
    excluded.write_text(json.dumps({'reserved_guard': {'excluded_run_ids': ['held']}}))
    manifest = {'cases': [{'id': 'blocked', 'question': ' do NOT use   this question '},
                          {'id': 'revision', 'question': 'new', 'revisions': [{'instruction': 'Keep only this cell'}]},
                          {'id': 'allowed', 'question': 'different'}]}
    result = audit.reserved_guard(manifest, db_path, excluded)
    assert result['blocked_case_ids'] == ['blocked', 'revision']
    assert result['answers_read'] is False
    assert 'question' not in json.dumps(result).replace('excluded_question_hashes_sha256', '')


def test_shared_and_batch_reservations_are_both_enforced(tmp_path):
    from pankagent_vnext.budget import Budget, BudgetExceeded
    path = tmp_path / 'budget.sqlite3'
    old = Budget(path, 30)
    rid = old.reserve('claude-sonnet-5', 'old', 400000, 0)  # $1 reservation
    batch = audit.make_audit_budget(path, global_limit=1.5, batch_cap=.4, prefix='audit:test:')
    batch.current_case = 'case1'
    one = batch.reserve('claude-sonnet-5', 'plan', 80000, 0)  # $.20
    batch.reserve('claude-sonnet-5', 'answer', 80000, 0)
    with pytest.raises(BudgetExceeded):
        batch.reserve('claude-sonnet-5', 'extra', 1, 0)
    batch.settle(one, {})
    assert old.snapshot()['reserved_usd'] == 1.2
    assert old.snapshot()['pending_calls'] == 2
    assert len(audit.budget_rows(path, 'audit:test:')) == 2
    assert audit.budget_rows(path, 'audit:test:')[0]['purpose'].startswith('audit:test:case1:')
    assert rid != one


def test_record_equality_includes_direction_and_measurement_properties():
    expected = {'start_id': 'Gene', 'end_id': 'disease', 'type': 'SIGNAL_COLOC_WITH', 'properties': {'PP.H4': .97}}
    swapped = {**expected, 'start_id': 'disease', 'end_id': 'Gene'}
    changed = {**expected, 'properties': {'PP.H4': .79}}
    assert audit.edge_key(expected) != audit.edge_key(swapped)
    assert audit.edge_key(expected) != audit.edge_key(changed)


def test_zero_scalar_is_evidence_and_multiple_candidate_counts_require_review():
    ref = [{'id': 'x', 'kind': 'count', 'subject': 'donor', 'rows': [{'donor_count': 0}]}]
    run = {'evidence': {'steps': [{'step_id': 's1', 'status': 'complete', 'truncated': False, 'rows': [{'donor_count': 0}]}]}}
    result = audit.summarize_evidence(run, ref)
    assert result['count_checks'][0]['matched']
    run['evidence']['steps'][0]['rows'] = [{'donor_count': 0}, {'donor_count': 3}]
    result = audit.summarize_evidence(run, ref)
    assert result['count_checks'][0]['matched'] is False
    assert result['count_checks'][0]['review_needed'] is True


def test_cost_includes_initial_plan_of_revision_journey_and_failed_cost():
    results = [{'attempt': 'J01-r1-v1', 'status': 'awaiting_confirmation', 'answer_nonempty': False, 'actual_usd': .02},
               {'attempt': 'J01-r1-v2', 'status': 'completed', 'answer_nonempty': True, 'actual_usd': .04},
               {'attempt': 'R01-r1-v1', 'status': 'failed', 'answer_nonempty': False, 'actual_usd': .03}]
    rows = [{'actual_usd': .02, 'reserved_usd': .1}, {'actual_usd': .04, 'reserved_usd': .1},
            {'actual_usd': .03, 'reserved_usd': .1}, {'actual_usd': None, 'reserved_usd': .12}]
    result = audit.costs(results, rows)
    assert result['mean_answer_journey_usd'] == pytest.approx(.06)
    assert result['mean_failed_journey_usd'] == pytest.approx(.03)
    assert result['mean_all_recorded_journey_usd'] == pytest.approx(.045)
    assert result['pending_reserved_usd'] == .12
    assert result['attributed_actual_usd'] == .09


def test_unknown_timing_remains_unknown():
    result = audit.timing([{'type': 'progress', 'elapsed_ms': 0}])
    assert result['first_progress_s'] == 0
    assert result['checked_plan_s'] is None
    assert result['graph_answer_s'] is None


def test_frozen_reference_hash_checks_properties_multiplicity_and_ignores_order():
    rows = [{'source': 'public', 'n_snp': 22}, {'source': 'public', 'n_snp': 4}]
    ref = {'id': 'qtl', 'kind': 'edges', 'frozen_count': 2,
           'frozen_rows_sha256': audit.reference_rows_digest(rows)}
    audit.validate_reference(ref, list(reversed(rows)), 'S02')
    with pytest.raises(RuntimeError, match='property drift'):
        audit.validate_reference(ref, [{'source': 'public', 'n_snp': 1}, rows[1]], 'S02')
    with pytest.raises(RuntimeError, match='snapshot drift'):
        audit.validate_reference(ref, rows + [rows[0]], 'S02')
    assert audit.reference_rows_digest(rows) != audit.reference_rows_digest([rows[0], rows[0]])


def test_supplement_is_separately_frozen_and_has_full_reference_signatures():
    value = audit.load_manifest(HARNESS.with_name('manifest.supplement.frozen.json'))
    assert value['sampling']['unique_tasks'] == 3
    assert {case['id'] for case in value['cases']} == {'S01', 'S02', 'S03'}
    assert all(ref['frozen_rows_sha256'] for case in value['cases'] for ref in case['references'])
