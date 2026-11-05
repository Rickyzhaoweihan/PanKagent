"""Independent evidence-only formatter; query planning remains on Qwen."""
import inspect
import json
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import httpx
from .formatter import evidence_input, FormatterFailure

MODEL = 'gpt-6.1-sol'
RELEASE = Path('/var/local/serviceuser/projects/pankgraph-demo/releases/20261014-communication-v1-staged-v3/backend')

async def format_final(question, result, release, on_delta=None):
    # Reuse the versioned interpretation/style contract, without instantiating
    # the legacy model gateway or sending the query agent's generated answer.
    sys.path.insert(0, str(RELEASE)) if str(RELEASE) not in sys.path else None
    if result.get('context_identity', {}).get('prompt_version') == 'schema-agent-v1':
        from pankagent_query.formatting import prepare, PassThrough
        prepared = prepare(question, result, release)
        scope = PassThrough()
    else:
        # Read/retry historical results using their original formatter contract.
        from pankagent_vnext.llm import ClaudeGateway
        from pankagent_vnext.answer_router import AnswerSkillRouter
        from pankagent_vnext.scope_guard import ScopeTextFilter
        evidence = evidence_input(question, result, release)
        prepared = ClaudeGateway.prepare_answer(SimpleNamespace(answer_router=AnswerSkillRouter()), question, evidence)
        scope = ScopeTextFilter(evidence)
    env = {}
    for line in Path('/var/local/serviceuser/.config/pankagent-vnext/runtime.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            words = shlex.split(value)
            env[key] = words[0] if words else ''
    key = env.get('OPENAI_API_KEY')
    if not key:
        raise FormatterFailure('OpenAI formatter credential is not configured', {})
    body = json.loads(prepared.body)
    if result.get('context_identity', {}).get('prompt_version') != 'schema-agent-v1':
        body['approved_query_handoff'] = {k: result.get(k) for k in (
            'standardized_question', 'metapath', 'cypher', 'parameters',
            'retrieval_status', 'status', 'condition_audit', 'unfulfilled_conditions')}
        # Exact selected-object SQL evidence is part of the KG handoff, not a
        # second retrieval stage. Keep original owner refs, rows and availability.
        if result.get('linked_evidence') or result.get('evidence_reads'):
            body['selected_object_evidence'] = {
                'release_id': release,
                'linked_evidence': result.get('linked_evidence', []),
                'evidence_reads': result.get('evidence_reads', [])}
    payload = dict(model=MODEL, instructions='\n\n'.join(p['text'] for p in prepared.system),
                   input=json.dumps(body, ensure_ascii=False), reasoning={'effort': 'low'},
                   max_output_tokens=8192, stream=True, store=False)
    from .formatter_budget import reserve, settle
    reservation = reserve(payload)
    started = time.monotonic()
    parts = []
    usage = {}
    first_text_s = None
    final = None
    refusals = []
    diagnostics = {'model': MODEL, 'transport_phase': 'preparing_request'}
    async def transport_trace(name, info):
        diagnostics['transport_phase'] = name
    def failure(message):
        return FormatterFailure(message, {**diagnostics, 'usage': usage,
                                          'elapsed_s': round(time.monotonic() - started, 3)})
    async def emit(text):
        nonlocal first_text_s
        if not text:
            return
        if first_text_s is None:
            first_text_s = time.monotonic() - started
        parts.append(text)
        if on_delta:
            response = on_delta(text)
            if inspect.isawaitable(response):
                await response
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=300), transport=httpx.AsyncHTTPTransport(retries=2), trust_env=False) as client:
            async with client.stream('POST', 'https://api.openai.com/v1/responses',
                                     headers={'Authorization': 'Bearer ' + key}, json=payload, extensions={'trace': transport_trace}) as response:
                diagnostics['http_status'] = response.status_code
                diagnostics['request_id'] = response.headers.get('x-request-id')
                if response.status_code != 200:
                    raise failure('OpenAI formatter HTTP ' + str(response.status_code))
                async for line in response.aiter_lines():
                    if not line.startswith('data: '):
                        continue
                    event = json.loads(line[6:])
                    kind = event.get('type')
                    if kind == 'response.output_text.delta':
                        await emit(scope.feed(event['delta']))
                    elif kind == 'response.refusal.done':
                        refusals.append(event.get('refusal', ''))
                    elif kind in ('response.completed', 'response.incomplete'):
                        final = event['response']
                        usage = final.get('usage', {})
                    elif kind in ('error', 'response.failed'):
                        raise failure('OpenAI formatter failed')
    except httpx.TransportError as exc:
        settle(reservation, not_sent=isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)))
        diagnostics['exception_type'] = type(exc).__name__
        raise failure('OpenAI formatter ' + type(exc).__name__) from exc
    if final is None:
        raise failure('Formatter stream ended without completion')
    settle(reservation, final.get('usage'))
    for item in final.get('output', []):
        for part in item.get('content', []):
            if part.get('type') == 'refusal' and part.get('refusal'):
                refusals.append(part['refusal'])
    if refusals:
        diagnostics['category'] = 'refusal'
        diagnostics['refusal'] = '\n'.join(dict.fromkeys(refusals))
        raise failure('OpenAI formatter declined the request: ' + diagnostics['refusal'])
    await emit(scope.feed('', final=True))
    if final.get('status') != 'completed':
        raise FormatterFailure('Formatter response incomplete', {'model': MODEL, 'usage': usage,
                               'incomplete_details': final.get('incomplete_details')})
    text = ''.join(parts)
    if not text.strip():
        raise failure('Formatter produced no answer')
    return dict(answer=text, model=MODEL, usage=usage, profile=prepared.profile, diagnostics=diagnostics,
                elapsed_s=round(time.monotonic() - started, 3), first_text_s=first_text_s,
                streamed=True, stop_reason='end_turn', inference_options={'reasoning_effort': 'low', 'store': False})
