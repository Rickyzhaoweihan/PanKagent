import asyncio
from types import SimpleNamespace
import anthropic
import httpx
import pytest
from pankagent_vnext.config import Settings
from pankagent_vnext.llm import ClaudeGateway, PLAN_SCHEMA


def rejection():
    response = httpx.Response(400, request=httpx.Request('POST', 'https://api.anthropic.com/v1/messages'))
    return anthropic.BadRequestError('invalid_request', response=response, body={})


def test_provider_schema_and_local_step_cap(tmp_path):
    # The live strict schema rejects maxItems; the application enforces this cap.
    assert 'maxItems' not in str(PLAN_SCHEMA)
    async def check():
        gateway = ClaudeGateway(Settings(state_dir=tmp_path, anthropic_key='test-placeholder'))
        called = []
        async def create(**kwargs):
            called.append(kwargs)
            return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {'input_tokens': 10, 'output_tokens': 10}),
                content=[SimpleNamespace(type='tool_use', name='record_plan', input={
                    'steps': [{'id': str(i), 'depends_on': []} for i in range(4)]})])
        gateway.client.messages.create = create
        try:
            with pytest.raises(ValueError, match='plan_too_large'):
                await gateway.plan('Which cell types express INS?', [])
            assert len(called) == 1
            assert called[0]['thinking'] == {'type': 'disabled'}
            assert not {'temperature', 'top_p', 'top_k'} & called[0].keys()
            assert gateway.budget.snapshot()['pending_calls'] == 0
        finally:
            await gateway.close()
    asyncio.run(check())


def test_definitive_plan_rejection_releases_reservation(tmp_path):
    async def check():
        gateway = ClaudeGateway(Settings(state_dir=tmp_path, anthropic_key='test-placeholder'))
        async def create(**kwargs):
            raise rejection()
        gateway.client.messages.create = create
        try:
            with pytest.raises(anthropic.BadRequestError):
                await gateway.plan('Which cell types express INS?', [])
            assert gateway.budget.snapshot()['reserved_usd'] == 0
            assert gateway.budget.snapshot()['spent_usd'] == 0
        finally:
            await gateway.close()
    asyncio.run(check())


def test_definitive_stream_rejection_releases_reservation(tmp_path):
    class RejectedStream:
        async def __aenter__(self):
            raise rejection()
        async def __aexit__(self, *args):
            pass
    async def check():
        gateway = ClaudeGateway(Settings(state_dir=tmp_path, anthropic_key='test-placeholder'))
        gateway.client.messages.stream = lambda **kwargs: RejectedStream()
        try:
            with pytest.raises(anthropic.BadRequestError):
                async for _ in gateway.synthesize('Which cell types express INS?', {'s1': {'status': 'complete', 'nodes': [{'id': 'INS', 'labels': ['Gene'], 'properties': {'name': 'INS'}}]}}):
                    pass
            assert gateway.budget.snapshot()['reserved_usd'] == 0
        finally:
            await gateway.close()
    asyncio.run(check())
