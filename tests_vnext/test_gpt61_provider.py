import asyncio
import pytest
from pankagent_vnext.config import Settings
from pankagent_vnext.budget import Budget
from pankagent_vnext.llm import ClaudeGateway


def test_new_default_and_gpt61_routing(monkeypatch, tmp_path):
    monkeypatch.delenv('PANK_VNEXT_MODEL', raising=False)
    assert Settings().model == 'claude-sonnet-5-5'
    async def run():
        settings=Settings(model='gpt-6.1-sol', openai_key='test',state_dir=tmp_path)
        assert settings.reasoning_effort == 'low'
        assert settings.provider_status_url == ''
        g=ClaudeGateway(settings)
        assert g.provider == 'openai' and g.api_key == 'test'
        await g.close()
    asyncio.run(run())


def test_gpt61_cache_and_long_context_prices(tmp_path):
    async def run():
        b=Budget(tmp_path/'budget.db',10)
        r=await b.areserve('gpt-6.1-sol','plan',1000,100)
        await b.asettle(r,{'input_tokens':100,'cache_read_input_tokens':200,'cache_creation_input_tokens':300,'output_tokens':10,'total_input_tokens':600})
        assert (await b.asnapshot())['spent_usd']==pytest.approx(.00107)
        r=await b.areserve('gpt-6.1-sol','plan',300000,100)
        await b.asettle(r,{'input_tokens':300000,'output_tokens':100,'total_input_tokens':300000})
        assert (await b.asnapshot())['spent_usd']==pytest.approx(1.20257)
        await b.aclose()
    asyncio.run(run())
