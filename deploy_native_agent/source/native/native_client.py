"""Native OpenAI transport only. No planner, parameter repair or history projection."""
import copy
import json
import os
import shlex
import time
import uuid
from pathlib import Path

BASE = 'http://127.0.0.1:33920/v1'
MODEL = 'Qwen3.8-27B'

class ProviderFailure(RuntimeError):
    pass

class ProtocolFailure(RuntimeError):
    pass

def credential():
    for line in Path('/var/local/serviceuser/.config/pankagent-vnext/runtime.env').read_text().splitlines():
        if line.startswith('CYPHER_API_TOKEN='):
            return shlex.split(line.split('=', 1)[1])[0]
    raise RuntimeError('Existing CYPHER_API_TOKEN is missing')

def definitions(tools, strict=False):
    out = []
    for t in tools:
        f = {'name': t['name'], 'description': t['description'], 'parameters': copy.deepcopy(t['input_schema'])}
        if strict:
            f['strict'] = True
        out.append({'type': 'function', 'function': f})
    return out

def messages(payload):
    out = [{'role': 'system', 'content': payload['system']}]
    for m in payload['messages']:
        c = m['content']
        if isinstance(c, str):
            out.append(copy.deepcopy(m)); continue
        if m['role'] == 'assistant':
            calls, texts = [], []
            for b in c:
                if b['type'] == 'tool_use':
                    calls.append(copy.deepcopy(b.get('_native_tool_call')) or {
                        'id': b['id'], 'type': 'function', 'function': {
                            'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)}})
                elif b['type'] == 'text':
                    texts.append(b['text'])
                else:
                    raise ProtocolFailure('Unsupported assistant block: ' + b['type'])
            msg = {'role': 'assistant', 'content': ''.join(texts) or None}
            if calls: msg['tool_calls'] = calls
            out.append(msg)
        else:
            for b in c:
                if b['type'] == 'tool_result':
                    out.append({'role': 'tool', 'tool_call_id': b['tool_use_id'], 'content': b['content']})
                elif b['type'] == 'text':
                    out.append({'role': 'user', 'content': b['text']})
                else:
                    raise ProtocolFailure('Unsupported user block: ' + b['type'])
    return out

class Stream:
    def __init__(self):
        self.calls = {}; self.text = ''; self.reasoning = ''; self.usage = {}; self.finish = None; self.done = False
    def feed(self, obj):
        if obj.get('usage'): self.usage = obj['usage']
        for choice in obj.get('choices', []):
            if choice.get('index', 0) != 0: raise ProtocolFailure('Unexpected multiple choices')
            self.finish = choice.get('finish_reason') or self.finish
            d = choice.get('delta', {})
            self.text += d.get('content') or ''
            self.reasoning += d.get('reasoning_content') or d.get('reasoning') or ''
            for item in d.get('tool_calls', []):
                i = item['index']; c = self.calls.setdefault(i, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                for k in ('id',):
                    if item.get(k): c[k] += item[k]
                for k in ('name', 'arguments'):
                    if item.get('function', {}).get(k): c['function'][k] += item['function'][k]
    def result(self):
        if not self.done or self.finish is None: raise ProtocolFailure('Stream ended without DONE/finish_reason')
        m = {'role': 'assistant', 'content': self.text or None}
        if self.calls: m['tool_calls'] = [self.calls[i] for i in sorted(self.calls)]
        if self.reasoning: m['reasoning_content'] = self.reasoning
        return {'message': m, 'usage': self.usage, 'finish_reason': self.finish}

class Client:
    def __init__(self, directory, token):
        self.directory = Path(directory); self.directory.mkdir(parents=True, exist_ok=True)
        self.token = token
    def save(self, name, value):
        raw = json.dumps(value, ensure_ascii=False, default=str, indent=2).replace(self.token, '[REDACTED]') if self.token else json.dumps(value, ensure_ascii=False, default=str, indent=2)
        (self.directory / name).write_text(raw)
    async def completion(self, msgs, tools, *, strict=False, stream=True):
        import httpx
        ident = uuid.uuid4().hex; start = time.monotonic()
        body = {'model': MODEL, 'messages': msgs, 'tools': definitions(tools, strict), 'tool_choice': 'auto',
                'chat_template_kwargs': {'enable_thinking': False}, 'stream': stream}
        if stream: body['stream_options'] = {'include_usage': True}
        self.save(ident + '-request.json', body)
        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as c:
                async with c.stream('POST', BASE + '/chat/completions', json=body, headers={'Authorization': 'Bearer ' + self.token}) as r:
                    if r.status_code != 200:
                        raw = (await r.aread()).decode(errors='replace')
                        self.save(ident + '-http-error.json', {'status': r.status_code, 'body': raw})
                        raise ProviderFailure(f'HTTP {r.status_code}: {raw}')
                    if stream:
                        state = Stream()
                        with (self.directory / (ident + '-sse.jsonl')).open('w') as log:
                            async for line in r.aiter_lines():
                                if not line.startswith('data:'): continue
                                data = line[5:].strip()
                                log.write(json.dumps({'elapsed_s': time.monotonic()-start, 'data': data.replace(self.token, '[REDACTED]')}) + '\n'); log.flush()
                                if data == '[DONE]': state.done = True; continue
                                state.feed(json.loads(data))
                        result = state.result()
                    else:
                        raw = json.loads(await r.aread()); self.save(ident + '-raw.json', raw)
                        result = {'message': raw['choices'][0]['message'], 'usage': raw.get('usage', {}), 'finish_reason': raw['choices'][0]['finish_reason']}
            result.update(record_id=ident, elapsed_s=time.monotonic()-start)
            self.save(ident + '-response.json', result)
            return result
        except BaseException as e:
            self.save(ident + '-error.json', {'type': type(e).__name__, 'message': str(e), 'elapsed_s': time.monotonic()-start, 'model_decision': False})
            raise
    async def model(self, payload):
        raw = await self.completion(messages(payload), payload['tools'])
        m = raw['message']; blocks = []
        try:
            if raw['finish_reason'] == 'length': raise ProtocolFailure('Provider output limit')
            ids = set()
            if m.get('content'): blocks.append({'type': 'text', 'text': m['content']})
            if m.get('reasoning_content'): raise ProtocolFailure('Unexpected reasoning with thinking disabled; raw response retained')
            for c in m.get('tool_calls', []):
                if not c.get('id') or c['id'] in ids: raise ProtocolFailure('Missing or duplicate tool_call_id')
                ids.add(c['id'])
                args = json.loads(c['function']['arguments'])
                if not isinstance(args, dict): raise ProtocolFailure('Arguments must be an object')
                blocks.append({'type': 'tool_use', 'id': c['id'], 'name': c['function']['name'], 'input': args, '_native_tool_call': copy.deepcopy(c)})
        except (ValueError, KeyError, ProtocolFailure) as e:
            return {'content': [], '_recoverable_error': {'kind': 'protocol', 'message': str(e), 'raw_text': json.dumps(m, ensure_ascii=False), 'record_id': raw['record_id']}}
        return {'content': blocks, 'usage': {'input_tokens': raw['usage'].get('prompt_tokens', 0), 'output_tokens': raw['usage'].get('completion_tokens', 0)}, '_record_id': raw['record_id'], '_provider_elapsed_s': raw['elapsed_s']}
