"""Anthropic Messages adapter for the release-bound dev query workflow."""
from copy import deepcopy

from simple_agent.features import DEV_RELEASE
from simple_agent.transport import model_remote


class HaikuClient:
    def __init__(self, model):
        if model != 'claude-haiku-5-5':
            raise ValueError('The shared dev ledger requires a priced Haiku model')
        self.name = model

    async def model(self, payload):
        if payload['model'] != self.name:
            raise ValueError('Query model identity mismatch')
        wire = deepcopy(payload)
        # Anthropic requires an object at the tool root. The runtime still
        # validates new-query vs pagination arguments using the original schema.
        for tool in wire['tools']:
            if tool['name'] == 'query':
                tool['input_schema'].pop('oneOf', None)
        # A confirmed plan can contain a tool call saved by the local provider.
        for message in wire['messages']:
            if isinstance(message['content'], list):
                for block in message['content']:
                    if block['type'] == 'tool_use':
                        block.pop('_native_tool_call', None)
        wire.update(max_tokens=8192, thinking={'type': 'disabled'})
        response = await model_remote(wire, budget_release=DEV_RELEASE)
        if response.get('stop_reason') == 'max_tokens':
            raise RuntimeError('Haiku reached its response token limit')
        return response
