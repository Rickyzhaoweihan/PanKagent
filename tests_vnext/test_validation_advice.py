"""Python findings survive for model review without semantic task vetoes."""
import asyncio
from copy import deepcopy
import pytest
from pankagent_vnext.validation_advice import prepare, execution_errors


def test_preparation_keeps_constraints_and_unverified_state_honest():
    source = {'constraints': [{'property': 'assay', 'value': 'model-selected'}],
              'semantic_issues': ['unrecognized_wording'],
              'runtime_binding_issues': ['unverified_category'],
              'entity_resolution': {'state': 'needs_clarification'},
              'recovery': {'category': 'E03', 'message': 'Possible ambiguity'}}
    original = deepcopy(source)
    result = prepare(source)
    assert source == original
    assert result['constraints'] == source['constraints']
    assert result['entity_resolution']['state'] == 'diagnosed'
    assert not result.get('recovery')
    assert all(d['blocking'] is False for d in result['python_diagnostics'])
    assert {'unrecognized_wording', 'unverified_category'} <= {d['code'] for d in result['python_diagnostics']}


@pytest.mark.parametrize('query', ["MATCH (n) DELETE n RETURN n", "RETURN 1; RETURN 2",
    "CALL apoc.load.json('http://example.com') YIELD value RETURN value",
    "RETURN apoc.cypher.runFirstColumn('CREATE (n)',{},true)"])
def test_execution_envelope_is_independent_of_semantic_validation(query):
    assert execution_errors(query, {})


def test_execution_envelope_does_not_police_semantics():
    assert execution_errors("MATCH (n) WHERE n.unfamiliar = 'DELETE' RETURN n LIMIT 4", {}) == []
    assert execution_errors('RETURN $missing', {}) == ['unknown_parameters']


def test_semantic_diagnostics_do_not_discard_a_returned_result():
    from test_graph import FakeAdapter, VALID, step
    async def check():
        adapter = FakeAdapter([[VALID + ' LIMIT 10']])
        async def emit(*args): pass
        result = await adapter.execute(step(semantic_issues=['wording_mismatch']), {}, emit)
        assert len(adapter.retrieved) == 1
        assert result['status'] == 'complete'
        assert any(d['code'] == 'incomplete_limit_or_slice' for d in result['python_diagnostics'])
        assert any(d['code'] == 'wording_mismatch' for d in result['requested_scope']['python_diagnostics'])
    asyncio.run(check())


def test_returned_evidence_findings_are_retained_for_answer_model(monkeypatch):
    from test_graph import FakeAdapter, VALID, step
    import pankagent_vnext.result_assessment as assessment
    monkeypatch.setattr(assessment, 'assess', lambda *args: {
        'valid': False, 'reasons': ['missing_path_witness'], 'complete': False})
    async def check():
        adapter = FakeAdapter([[VALID]])
        async def emit(*args): pass
        result = await adapter.execute(step(), {}, emit)
        assert result['nodes']
        assert any(d['code'] == 'missing_path_witness' for d in result['python_diagnostics'])
        assert result['result_assessments'][0]['valid'] is False
    asyncio.run(check())


def test_persistent_advice_does_not_exhaust_planning_retries():
    from test_claude_led_planning import gateway, SCHEMA, PLAN
    from pankagent_vnext.planning_session import run
    async def check():
        g, calls = gateway([('record_plan', PLAN)])
        async def preparer(plan):
            plan['steps'][0]['semantic_issues'] = ['unusual_wording']
            return plan
        result = await run(g, 'Find TM4SF6', '{}', 'test', SCHEMA, 500,
                           lambda p, c: p, preparer=preparer)
        assert len(calls) == 2
        assert result['steps'] and not result.get('clarification')
        assert result['python_diagnostics'][0]['blocking'] is False
    asyncio.run(check())


def test_path_template_mismatch_falls_back_and_retains_raw_evidence(monkeypatch):
    from test_graph import FakeAdapter, VALID, step
    import pankagent_vnext.bounded_paths as paths
    import pankagent_vnext.graph as graph
    monkeypatch.setattr(paths, 'plan_issue', lambda s: 'unfamiliar_topology')
    def unavailable(s):
        raise paths.BoundedPathError('unfamiliar_topology')
    monkeypatch.setattr(paths, 'compile_query', unavailable)
    monkeypatch.setattr(graph, 'build_generation_question', lambda s: 'Use the selected scope')
    monkeypatch.setattr(graph, 'generation_request', lambda s, q: q)
    async def check():
        adapter = FakeAdapter([[VALID]])
        async def emit(*args): pass
        result = await adapter.execute(step(path_spec={'nodes': [], 'edges': []}), {}, emit)
        assert adapter.retrieved
        assert result['nodes'] and result['status'] != 'failed'
        codes = {d['code'] for d in result['python_diagnostics']}
        assert any(c.startswith('unsupported_bounded_path_spec:') for c in codes)
        assert any(c.startswith('invalid_bounded_path_evidence:') for c in codes)
    asyncio.run(check())
