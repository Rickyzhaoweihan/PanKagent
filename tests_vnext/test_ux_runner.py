from ux_audit.run_pairs import Replay


def test_pending_revision_uses_plan_session_and_preserves_sequence():
    runner = object.__new__(Replay)
    runner.current = {'initial': {'session_id': 'chat', 'pending_plan_session_id': 'plan'}}
    runner.persist = lambda: None
    calls = []

    def call(side, method, path, body=None):
        calls.append((method, path, body))
        return {}

    runner.call = call
    runner.official({'family': 'revision', 'revisions': [
        {'instruction': 'Only ND'}, {'instruction': 'Also include immune cells'}]})
    assert calls[:2] == [
        ('POST', '/plan/revise', {'session_id': 'plan', 'prompt': 'Only ND'}),
        ('POST', '/plan/revise', {'session_id': 'plan', 'prompt': 'Also include immune cells'})]
    assert calls[2] == ('POST', '/chat/plan/confirm', {'chat_session_id': 'chat', 'plan_session_id': 'plan'})
    assert not any(path == '/chat/start' for _, path, _ in calls)
