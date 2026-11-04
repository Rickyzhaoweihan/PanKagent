"""Independent final-output module. Reuse the deployed formatter without editing it."""
import asyncio
import json
import os
import subprocess
import sys
import time
from .transport import REMOTE_COMMAND

SOURCE = r'''
import hashlib,json,shlex,sys,urllib.request,urllib.error,time,os
from pathlib import Path
from types import SimpleNamespace
root=Path('/var/local/serviceuser/projects/pankagent-vnext')
sys.path.insert(0,str(root))
from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.answer_router import AnswerSkillRouter
from pankagent_vnext.scope_guard import ScopeTextFilter
# prepare_answer is a pure formatter method. No gateway constructor, planning,
# database access, service restart, or current service budget/storage mutation.
prepared=ClaudeGateway.prepare_answer(SimpleNamespace(answer_router=AnswerSkillRouter()), REQUEST['question'], REQUEST['evidence'])
env={}
for line in Path('/var/local/serviceuser/.config/pankagent-vnext/runtime.env').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.split('=',1);words=shlex.split(value);env[key]=words[0] if words else ''
body=json.loads(prepared.body)
body['approved_query_handoff']=REQUEST['handoff']
payload={'model':REQUEST['model'],'max_tokens':prepared.profile['answer_budget']['max_output_tokens'],
         'system':prepared.system,'messages':[{'role':'user','content':json.dumps(body,ensure_ascii=False)}], 'stream':True}
# Sonnet 5.5 cannot accept thinking=disabled. Without tools, between_tools
# removes up-front thinking so the formatter's output budget is for answer text.
if REQUEST['model']=='claude-sonnet-5-5':
    payload.update(thinking={'type':'between_tools'}, output_config={'effort':'low'})
req=urllib.request.Request('https://api.anthropic.com/v1/messages',data=json.dumps(payload).encode(),
    headers={'x-api-key':env['ANTHROPIC_API_KEY'],'anthropic-version':'2023-06-01','Content-Type':'application/json'})
reservation=None
if os.environ.get('SIMPLE_AGENT_SHARED_BUDGET')=='1':
    from simple_agent.shared_budget import reserve,settle
    reservation=reserve(payload,env,'simple_formatter')
provider_started=time.perf_counter()
scope=ScopeTextFilter(REQUEST['evidence'])
answer=[];pending='';last_emit=provider_started;first_text_s=None;usage={};stop_reason=None;stopped=False

def emit(text,final=False):
    global pending,last_emit,first_text_s
    pending+=text
    if pending and (final or first_text_s is None or time.perf_counter()-last_emit>=.15 or len(pending)>=512):
        if first_text_s is None:first_text_s=time.perf_counter()-provider_started
        answer.append(pending)
        print(json.dumps({'type':'delta','text':pending}),flush=True)
        pending='';last_emit=time.perf_counter()
try:
    with urllib.request.urlopen(req,timeout=90) as response:
        for line in response:
            if not line.startswith(b'data: '):continue
            event=json.loads(line[6:])
            kind=event.get('type')
            if kind=='error':raise RuntimeError(json.dumps(event.get('error',{})))
            if kind=='message_start':usage.update(event['message'].get('usage',{}))
            elif kind=='content_block_delta' and event.get('delta',{}).get('type')=='text_delta':
                emit(scope.feed(event['delta']['text']))
            elif kind=='message_delta':
                usage.update(event.get('usage',{}));stop_reason=event.get('delta',{}).get('stop_reason')
            elif kind=='message_stop':stopped=True
    if not stopped:raise RuntimeError('Provider stream ended before message_stop')
    emit(scope.feed('',final=True),final=True)
except Exception as exc:
    emit('',final=True)
    print(json.dumps({'type':'error','error':str(exc)[:600],'usage':usage,'stop_reason':stop_reason,'visible_characters':sum(map(len,answer))}),flush=True)
    raise SystemExit(0)
text=''.join(answer)
if reservation:settle(reservation,usage)
files=['llm.py','answer_router.py','evidence_context.py','prompts/answer_style.md','answer_skills/manifest.json']
print(json.dumps({'type':'final','answer':text,'profile':prepared.profile,'model':REQUEST['model'],'usage':usage,
  'provider_elapsed_s':time.perf_counter()-provider_started,'first_text_s':first_text_s,'streamed':True,'inference_options':{'thinking':payload.get('thinking'),'output_config':payload.get('output_config')},'stop_reason':stop_reason,'source_hashes':{f:hashlib.sha256((root/'pankagent_vnext'/f).read_bytes()).hexdigest() for f in files}}))
'''


def evidence_input(question, result, release):
    """Lossless record adapter; do not feed the query agent's answer into synthesis."""
    graph = result['graph']
    nodes = [dict(n) for n in graph['nodes']]
    edges = [{**e, 'start_id': e['source'], 'end_id': e['target']} for e in graph['edges']]
    pattern = result['metapath']
    constraints = [{'owner': 'node', 'key': n['key'], 'entity_type': n['label'], **f}
                   for n in pattern['nodes'] for f in n.get('filters', [])]
    constraints += [{'owner': 'edge', 'key': e['key'], 'relationship_type': e['type'], **f}
                    for e in pattern['edges'] for f in e.get('filters', [])]
    # An executed pattern is structurally checked. Human confirmation is not an
    # independent scientific validator, so do not invent verified query coverage.
    step = dict(question=question, purpose='primary', nodes=nodes, edges=edges, rows=[],
                status=result['retrieval_status'], truncated=result['retrieval_status']=='partial', graph_version=release,
                queries=[{'cypher': result['cypher'], 'parameters': result['parameters']}],
                requested_scope={'constraints': constraints, 'pattern': pattern},
                validation={'status': 'accepted', 'scope': 'schema-checked agent pattern; user-confirmed, semantic fidelity not independently verified'},
                condition_audit=result.get('condition_audit', []), unfulfilled_conditions=result.get('unfulfilled_conditions', []))
    return {'graph': step}


def request_payload(question, result, release):
    if os.environ.get('SIMPLE_AGENT_ALLOW_REMOTE_MODEL') != '1':
        raise RuntimeError('Remote formatting requires the approved model-transfer gate')
    payload = {'question': question, 'evidence': evidence_input(question, result, release),
               'model': os.environ.get('SIMPLE_FORMATTER_MODEL', 'claude-sonnet-5-5'),
               'handoff': {k: result.get(k) for k in ('standardized_question', 'metapath', 'cypher', 'parameters', 'retrieval_status', 'status', 'condition_audit', 'unfulfilled_conditions')}}
    wire = json.dumps(payload, ensure_ascii=False)
    if len(wire.encode()) > 2_100_000:
        raise ValueError('Formatting input exceeds the bounded graph handoff')
    return wire


class FormatterFailure(RuntimeError):
    def __init__(self, message, metadata):
        super().__init__(message)
        self.metadata = metadata


async def consume_stream(reader, on_delta=None):
    import inspect
    output = None
    while line := await reader.readline():
        event = json.loads(line)
        if event.get('type') == 'delta':
            if on_delta:
                value = on_delta(event['text'])
                if inspect.isawaitable(value): await value
        elif event.get('type') == 'error':
            raise FormatterFailure('Formatting stream failed: '+str(event['error']), event)
        elif event.get('type') == 'final':
            output = event
    if output is None:
        raise FormatterFailure('Formatting stream ended without a final result', {})
    if not output.get('answer', '').strip():
        raise FormatterFailure('Formatter produced no visible answer; stop_reason='+str(output.get('stop_reason')), output)
    return output


async def format_final(question, result, release, on_delta=None):
    started = time.monotonic()
    wire = request_payload(question, result, release)
    argv=[sys.executable,'-'] if os.environ.get('SIMPLE_AGENT_NATIVE') == '1' else ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','jieliulab3-codex',REMOTE_COMMAND]
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        process.stdin.write(('import json\nREQUEST=json.loads('+repr(wire)+')\n'+SOURCE).encode())
        await process.stdin.drain()
        process.stdin.close()
        output = await consume_stream(process.stdout, on_delta)
        await process.wait()
        stderr = await stderr_task
        if process.returncode:
            raise FormatterFailure('Formatter transport failed: '+stderr.decode()[-600:], output)
        output['elapsed_s'] = round(time.monotonic() - started, 3)
        return output
    finally:
        if process.returncode is None:
            process.terminate()
            try: await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill();await process.wait()
        await stderr_task
