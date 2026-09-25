"""Responses API transport normalized to the existing model message contract.

No provider SDK internals leak into graph/planning code. Secrets never enter
request bodies or audit events. Automatic retries are disabled: ambiguous
failures must retain their reservation rather than spend twice.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
import json
import time
from pathlib import Path
import httpx
from .audit import provider_event

PROMPT = (Path(__file__).parent / 'answer_skills/providers/gpt6_sol.md').read_text()


class ProviderStatusError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__('openai_request_rejected_' + str(status_code))


class ProviderProtocolError(RuntimeError):
    pass


class Record(SimpleNamespace):
    def model_dump(self, **kwargs):
        def dump(v):
            if isinstance(v, Record): return {k: dump(x) for k, x in vars(v).items()}
            if isinstance(v, list): return [dump(x) for x in v]
            return v
        return dump(self)


def normalize_response(data):
    usage = data.get('usage')
    if not isinstance(usage, dict) or 'input_tokens' not in usage or 'output_tokens' not in usage:
        raise ProviderProtocolError('missing_openai_usage')
    details = usage.get('input_tokens_details') or {}
    cached, writes = details.get('cached_tokens', 0), details.get('cache_write_tokens', 0)
    total = usage['input_tokens']
    if min(total, cached, writes, usage['output_tokens']) < 0 or cached + writes > total:
        raise ProviderProtocolError('invalid_openai_usage')
    normalized = dict(input_tokens=total-cached-writes, output_tokens=usage['output_tokens'],
                      cache_read_input_tokens=cached, cache_creation_input_tokens=writes,
                      total_input_tokens=total,
                      reasoning_tokens=(usage.get('output_tokens_details') or {}).get('reasoning_tokens', 0))
    content = []
    for item in data.get('output', []):
        if item['type'] == 'function_call':
            # Malformed model arguments are repairable application output; usage
            # must still settle before the planner decides whether to repair.
            try: args = json.loads(item.get('arguments', '{}'))
            except (ValueError, TypeError): args = {'_invalid_arguments': True}
            content.append(Record(type='tool_use', id=item['call_id'], name=item['name'], input=args))
        elif item['type'] == 'message':
            for block in item.get('content', []):
                if block['type'] == 'output_text': content.append(Record(type='text', text=block['text']))
    status = data.get('status')
    if status not in ('completed', 'incomplete'):
        raise ProviderProtocolError('openai_response_not_completed')
    reason = (data.get('incomplete_details') or {}).get('reason')
    stop = 'max_tokens' if reason == 'max_output_tokens' else 'tool_use' if any(b.type == 'tool_use' for b in content) else 'end_turn'
    if status == 'incomplete' and reason != 'max_output_tokens':
        stop = 'refusal'
    return Record(id=data['id'], model=data.get('model'), content=content,
                  usage=Record(**normalized), stop_reason=stop)


def request_body(kwargs, effort):
    system = kwargs.get('system', [])
    instructions = system if isinstance(system, str) else '\n\n'.join(b['text'] for b in system)
    items = []
    for message in kwargs['messages']:
        role, content = message['role'], message['content']
        if isinstance(content, str):
            items.append({'role': role, 'content': content}); continue
        for block in content:
            if block['type'] == 'text': items.append({'role': role, 'content': block['text']})
            elif block['type'] == 'tool_use':
                items.append({'type': 'function_call', 'call_id': block['id'],
                              'name': block['name'], 'arguments': json.dumps(block['input'])})
            elif block['type'] == 'tool_result':
                value = block.get('content', '')
                items.append({'type': 'function_call_output', 'call_id': block['tool_use_id'],
                              'output': value if isinstance(value, str) else json.dumps(value)})
            else: raise ProviderProtocolError('unsupported_message_block')
    body = {'model': kwargs['model'], 'instructions': instructions + '\n\n' + PROMPT,
            'input': items, 'max_output_tokens': kwargs['max_tokens'],
            'reasoning': {'effort': effort}, 'store': False, 'service_tier': 'default'}
    tools = kwargs.get('tools')
    if tools:
        # Preserve optional fields: strict=True would require changing the common
        # plan schema. Existing application/schema validation remains authoritative.
        body['tools'] = [{'type': 'function', 'name': t['name'],
                          'description': t.get('description', ''),
                          'parameters': t['input_schema'], 'strict': False} for t in tools]
        choice = kwargs.get('tool_choice', {})
        body['tool_choice'] = ({'type': 'function', 'name': choice['name']} if choice.get('type') == 'tool'
                               else 'required' if choice.get('type') == 'any' else 'auto')
        body['parallel_tool_calls'] = False
    return body


class ResponseStream:
    def __init__(self, response, started):
        self.response, self.started = response, started
        self.final = None
        self.text_stream = self._text()

    async def _text(self):
        first = True
        async for line in self.response.aiter_lines():
            if not line.startswith('data: '): continue
            if line[6:] == '[DONE]': break
            event = json.loads(line[6:])
            kind = event.get('type')
            if kind == 'response.output_text.delta':
                if first:
                    provider_event('model_first_token', {'provider': 'openai', 'seconds': time.monotonic()-self.started})
                    first = False
                yield event['delta']
            elif kind in ('response.completed', 'response.incomplete'):
                self.final = normalize_response(event['response'])
            elif kind in ('error', 'response.failed'):
                raise ProviderProtocolError('openai_stream_failed')
        if self.final is None: raise ProviderProtocolError('openai_stream_missing_final_usage')

    async def get_final_message(self):
        if self.final is None: raise ProviderProtocolError('openai_stream_not_finished')
        return self.final


class OpenAIClient:
    def __init__(self, api_key, timeout, effort='none', transport=None):
        self.http = httpx.AsyncClient(base_url='https://api.openai.com/v1/',
            headers={'Authorization': 'Bearer '+api_key}, timeout=timeout, transport=transport)
        self.effort = effort
        self.messages = self
        self.models = SimpleNamespace(retrieve=self.retrieve)

    async def retrieve(self, model):
        r = await self.http.get('models/'+model)
        if r.is_error: raise ProviderStatusError(r.status_code)
        return Record(**r.json())

    async def create(self, **kwargs):
        started = time.monotonic()
        r = await self.http.post('responses', json=request_body(kwargs, self.effort))
        if r.is_error: raise ProviderStatusError(r.status_code)
        result = normalize_response(r.json())
        provider_event('provider_response', {'provider': 'openai', 'model': result.model,
                       'request_id': r.headers.get('x-request-id'), 'seconds': time.monotonic()-started})
        return result

    @asynccontextmanager
    async def stream(self, **kwargs):
        started = time.monotonic()
        async with self.http.stream('POST', 'responses', json={**request_body(kwargs, self.effort), 'stream': True}) as r:
            if r.is_error: raise ProviderStatusError(r.status_code)
            yield ResponseStream(r, started)

    async def close(self):
        await self.http.aclose()
