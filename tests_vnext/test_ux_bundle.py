import json

from ux_audit.build_bundle import assemble, measures


def test_failure_text_is_not_a_validated_preview_or_graph_answer():
    result = measures({
        'created': {'run_id': 'one'},
        'confirmed_at': '2026-09-06T12:00:10+00:00',
        'events': [
            {'run_id': 'one', 'type': 'plan_ready', 'elapsed_ms': 5000},
            {'run_id': 'one', 'type': 'graph_answer', 'elapsed_ms': 15000,
             'timestamp': '2026-09-06T12:00:15+00:00'},
        ],
        'initial': {'preview': {'evidence': {'steps': [{'status': 'failed'}]}}},
        'final': {'status': 'partial', 'evidence': {'nodes': []}},
    })
    assert result['plan_ready_s'] == 5
    assert result['validated_preview_s'] is None
    assert result['graph_nodes_present'] is False
    assert result['answer_text_after_confirmation_s'] == 5


def test_missing_capture_and_holdout_have_explicit_blockers(tmp_path):
    manifest = {'tasks': [{'id': 'E02', 'parent_task': 'N10', 'goal': 'Inspect evidence',
                           'question': 'A protected question', 'source': {}, 'holdout': False}]}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    summary = assemble(tmp_path)
    outcome = summary['outcomes'][0]
    assert outcome['classification'] == 'not comparable'
    assert len(outcome['sides']['official']['blockers']) == 3
    card = json.loads((tmp_path / 'task-cards/E02.json').read_text())
    assert 'holdout_policy' in card
    assert 'A protected question' not in (tmp_path / 'sanitized-summary.json').read_text()


def test_task_specific_capture_takes_precedence_over_parent(tmp_path):
    (tmp_path / 'manifest.json').write_text(json.dumps({'tasks': [
        {'id': 'X01', 'parent_task': 'N01', 'goal': 'Reload', 'question': 'Q',
         'source': {}, 'holdout': False}]}))
    (tmp_path / 'cases').mkdir()
    (tmp_path / 'cases/N01-vnext.json').write_text(json.dumps({'status': 'captured'}))
    (tmp_path / 'cases/X01-vnext.json').write_text(json.dumps({'status': 'blocked', 'error_category': 'timeout'}))
    summary = assemble(tmp_path)
    assert summary['outcomes'][0]['sides']['vnext']['capture_status'] == 'blocked'
