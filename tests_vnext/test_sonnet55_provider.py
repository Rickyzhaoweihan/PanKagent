import asyncio
from types import SimpleNamespace
from pankagent_vnext.config import Settings
from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.budget import Budget


def test_model_options_and_cached_token_cost(tmp_path):
    settings = Settings(model='claude-sonnet-5-5', state_dir=tmp_path)
    gateway = object.__new__(ClaudeGateway)
    gateway.settings = settings
    assert gateway._options() == {'thinking': {'type': 'between_tools'}, 'output_config': {'effort': 'high'}}
    b = Budget(tmp_path/'budget.sqlite3', 20)
    rid = b.reserve(settings.model, 'test', 10000, 1000)
    b.settle(rid, {'input_tokens': 1000, 'output_tokens': 100, 'cache_creation_input_tokens': 1000, 'cache_read_input_tokens': 1000})
    assert b.snapshot()['spent_usd'] == .0057
    asyncio.run(b.aclose())


def test_tool_compatibility_preserves_history_and_schema():
    async def scenario():
        seen = []
        async def create(**kwargs):
            seen.append(kwargs)
            return 'response'
        gateway = object.__new__(ClaudeGateway)
        gateway.client = SimpleNamespace(messages=SimpleNamespace(create=create))
        messages = [{'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': '', 'signature': 'test-signature'}]}, {'role': 'user', 'content': 'Tool result'}]
        tools = [{'name': 'record_plan', 'input_schema': {'type': 'object'}}]
        for model in ['claude-sonnet-5-5', 'claude-sonnet-5']:
            gateway.settings = SimpleNamespace(model=model)
            await gateway._create('reservation', messages=messages, tools=tools, tool_choice={'type': 'tool', 'name': 'record_plan'})
        assert seen[0]['tool_choice'] == {'type': 'auto', 'disable_parallel_tool_use': True}
        assert seen[0]['messages'] is messages and seen[0]['tools'] is tools
        assert seen[1]['tool_choice'] == {'type': 'tool', 'name': 'record_plan'}
    asyncio.run(scenario())
