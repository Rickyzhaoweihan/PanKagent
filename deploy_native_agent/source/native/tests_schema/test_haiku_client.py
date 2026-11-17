"""Check provider wire compatibility without changing the shared tool schema."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haiku_client import HaikuClient
from pankagent_query.core import TOOLS
from simple_agent.features import DEV_RELEASE


class HaikuClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_history_and_query_union_only_change_on_wire(self):
        client = HaikuClient('claude-haiku-5-5')
        block = dict(type='tool_use', id='previous', name='query',
                     input={'query_id': 'q1'}, _native_tool_call={'id': 'previous'})
        payload = dict(model=client.name, tools=TOOLS,
                       messages=[{'role': 'assistant', 'content': [block]}])
        before = deepcopy(payload)
        with patch('haiku_client.model_remote', new_callable=AsyncMock) as send:
            send.return_value = {'content': [], 'stop_reason': 'end_turn'}
            await client.model(payload)
            wire = send.await_args.args[0]
            self.assertEqual(send.await_args.kwargs, dict(budget_release=DEV_RELEASE))
        query = next(tool for tool in wire['tools'] if tool['name'] == 'query')
        self.assertNotIn('oneOf', query['input_schema'])
        self.assertEqual(wire['messages'][0]['content'][0]['input'], block['input'])
        self.assertNotIn('_native_tool_call', wire['messages'][0]['content'][0])
        self.assertEqual(payload, before)

    async def test_incomplete_model_output_is_an_error(self):
        client = HaikuClient('claude-haiku-5-5')
        with patch('haiku_client.model_remote', new_callable=AsyncMock) as send:
            send.return_value = {'stop_reason': 'max_tokens'}
            with self.assertRaisesRegex(RuntimeError, 'token limit'):
                await client.model(dict(model=client.name, messages=[], tools=[]))
