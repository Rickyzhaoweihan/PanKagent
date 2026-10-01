"""Budget refusal stops an acceptance arm rather than failing every later case."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import make_dataclass
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
spec = importlib.util.spec_from_file_location('original_workflow_budget_test',
    ROOT / 'scripts/acceptance/original_workflow.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_reservations_reduce_admission_even_if_remaining_is_stale():
    assert runner.available_budget({'limit_usd': 20, 'spent_usd': 10,
        'reserved_usd': 9.9, 'remaining_usd': 10}) == pytest.approx(.1)
    assert runner.available_budget({'limit_usd': 20, 'spent_usd': 10,
        'reserved_usd': 0, 'remaining_usd': 9}) == 9


def test_only_financial_budget_diagnostics_stop_the_arm():
    assert runner.budget_failure({'category': 'budget_exhausted'})
    assert runner.budget_failure({'error': {'code': 'insufficient_quota'}})
    assert runner.budget_failure('BudgetExceeded')
    assert not runner.budget_failure({'status': 'lookup_budget_exhausted'})
    assert not runner.budget_failure({'category': 'timeout'})


@pytest.mark.parametrize('failure', ['admission', 'provider_quota', 'planning', 'synthesis', 'confirmation', 'reserved'])
def test_financial_refusal_leaves_remaining_cases_unattempted(tmp_path, monkeypatch, failure):
    # Inject isolated adapters: no API, graph, service, or real ledger is touched.
    def module(name, **attrs):
        value = ModuleType(name)
        value.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, value)

    async def noop(*args, **kwargs):pass
    budget = {'limit_usd': 20, 'spent_usd': 10, 'reserved_usd': 0, 'remaining_usd': 10}
    if failure == 'reserved':budget.update(reserved_usd=9.9)
    async def snapshot():return dict(budget)
    gateway = SimpleNamespace(budget=SimpleNamespace(asnapshot=snapshot))
    settings = make_dataclass('Settings', [(key, object, None) for key in (
        'model', 'budget_dir', 'budget_usd', 'state_dir', 'provider_status_url',
        'plan_cache_enabled', 'reasoning_effort', 'cypher_url')])
    module('deploy_results.manage', read_protected_env=lambda _: {})
    module('pankagent_vnext.config', Settings=settings)
    module('pankagent_vnext.llm', ClaudeGateway=lambda _: gateway)
    module('pankagent_vnext.graph', GraphAdapter=lambda _: SimpleNamespace(
        _retrieve=noop, _generate=noop, _explain=noop))
    module('pankagent_vnext.planning_contract', VerifiedCache=lambda: None)
    module('pankagent_vnext.preplanning_grounding', warm_grounding=noop)
    module('pankagent_vnext.agent_schemas', run_context=lambda _: {})
    raw = {'status': 'failed', 'error': {'category': 'budget_exhausted'}}
    if failure in {'synthesis', 'confirmation'}:
        raw = {'status': 'awaiting_confirmation', 'plan_id': 'p1',
               'preview': {'confirmation_eligible': True}}
    async def io_call(fn, *args):return fn(*args)
    store = SimpleNamespace(get=lambda _: dict(raw), audit_snapshot=lambda _: {'events': []},
                            events_after=lambda *a: [])
    @asynccontextmanager
    async def lifespan(_):yield
    app = SimpleNamespace(router=SimpleNamespace(lifespan_context=lifespan),
                          state=SimpleNamespace(runtime=SimpleNamespace(
                              io=SimpleNamespace(call=io_call), store=store)))
    module('pankagent_vnext.app', create_app=lambda *a: app)
    requests = []
    class Response:
        def __init__(self, status, payload):self.status_code, self.payload = status, payload
        def json(self):return self.payload
        def raise_for_status(self):
            if self.status_code >= 400:raise RuntimeError('HTTP status error')
    class Client:
        def __init__(self, **kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self, *args):pass
        async def post(self, url, **kw):
            requests.append(url)
            if url == '/v2/plans':
                if failure == 'admission':return Response(503, {'detail': 'The development evaluation budget is unavailable or exhausted.'})
                if failure == 'provider_quota':return Response(429, {'error': {'code': 'insufficient_quota'}})
                return Response(202, {'run_id': 'r1'})
            if url.endswith('/confirm'):
                if failure == 'confirmation':return Response(503, {'detail': 'The development evaluation budget is unavailable or exhausted.'})
                raw.update(status='partial', evidence={'synthesis_error': {'category': 'budget_exhausted'}})
                return Response(202, {})
            return Response(200, {})
    module('httpx', AsyncClient=Client, ASGITransport=lambda **kw: None)
    monkeypatch.setattr(runner, 'evaluate', lambda *args: {'verified_core_covered': False})
    fixture = tmp_path / 'fixture.json'
    fixture.write_text(json.dumps({'cases': [{'id': f'Q{i:02}', 'question': 'example'} for i in (1, 2, 3)]}))
    (tmp_path / 'budget.sqlite3').touch()
    args = SimpleNamespace(ledger=tmp_path, fixture=fixture, case_keys=None, model='claude-sonnet-5-5',
        ceiling=20, root=tmp_path, arm='candidate', env=tmp_path/'unused.env', resume=False)
    asyncio.run(runner.run(args))
    summary = json.loads((tmp_path/'summary.json').read_text())
    assert summary['complete'] is False
    assert summary['incomplete'] == 'budget'
    assert requests.count('/v2/plans') == (0 if failure == 'reserved' else 1)
    assert summary['unattempted_cases'] == (['Q01', 'Q02', 'Q03'] if failure == 'reserved' else ['Q02', 'Q03'])
    assert not (tmp_path/'inflight.json').exists()
    if failure != 'reserved':
        rows = [json.loads(line) for line in (tmp_path/'report.jsonl').read_text().splitlines()]
        assert len(rows) == 1
        assert rows[0]['evaluation_status'] == 'incomplete_budget'
