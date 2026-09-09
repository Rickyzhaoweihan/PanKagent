import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx

from pankagent_vnext.app import create_app
from tests_vnext.test_answer_synthesis import (
    QUESTION, USAGE, MockStream, RuntimeGateway, RuntimeGraph, UnrequestedLiterature,
    await_state, detection_evidence, gateway_with_mock,
)


def test_grouped_answer_has_bounded_larger_budget_and_compact_contract(monkeypatch, tmp_path):
    async def run():
        gateway, fake, _ = gateway_with_mock(monkeypatch, tmp_path, ['Three checked categories. [G1]'])
        evidence = {f's{i}':deepcopy(detection_evidence()['s1']) for i in range(1, 4)}
        prepared = gateway.prepare_answer('Profile the recorded gene evidence.', evidence)
        assert prepared.profile['answer_budget']['max_output_tokens'] == 2400
        assert 'FOUR illustrative table rows' in prepared.system[-1]['text']
        original_profile = deepcopy(prepared.profile)
        answer = ''.join([part async for part in gateway.synthesize('Profile the recorded gene evidence.', evidence, prepared=prepared)])
        assert answer
        assert fake.stream_calls[0]['max_tokens'] == 2400
        assert prepared.profile == original_profile  # Previously emitted profile is immutable.
        assert prepared.generation['truncated'] is False
        assert gateway.budget.snapshot()['pending_calls'] == 0
    asyncio.run(run())


def test_output_limit_is_partial_and_never_authorizes_literature(monkeypatch, tmp_path):
    async def limit(self):
        return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: deepcopy(USAGE)), stop_reason='max_tokens')
    monkeypatch.setattr(MockStream, 'get_final_message', limit)
    async def run():
        gateway, fake, settings = gateway_with_mock(monkeypatch, tmp_path, ['INS was detected. [G1]\n\n| unfinished'], RuntimeGateway)
        class Literature(UnrequestedLiterature):
            calls = 0
            async def search(self, *_):
                self.calls += 1
                raise AssertionError('Incomplete written answer must not start literature')
        literature = Literature()
        graph = RuntimeGraph()
        app = create_app(settings, gateway, graph, literature)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
                created = (await client.post('/v2/plans', json={'question':QUESTION})).json()
                await await_state(client, created['run_id'], {'awaiting_confirmation'})
                await client.post(f"/v2/plans/{created['plan_id']}/confirm")
                result = await await_state(client, created['run_id'], {'partial'})
                assert result['evidence']['answer_generation']['truncated'] is True
                assert result['evidence']['synthesis_error']['category'] == 'answer_output_limit'
                assert result['evidence']['answer_incomplete'] is True
                assert result['evidence']['steps'][0]['status'] == 'complete'
                assert graph.calls == len(fake.stream_calls) == 1
                assert literature.calls == 0
                assert gateway.budget.snapshot()['pending_calls'] == 0
    asyncio.run(run())
