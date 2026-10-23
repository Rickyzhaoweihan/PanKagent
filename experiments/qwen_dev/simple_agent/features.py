"""Reuse retained dev feature modules without restoring its agent orchestration."""
import asyncio
import inspect
import json
import os
import sys

DEV_RELEASE = '/var/local/serviceuser/projects/pankgraph-demo/releases/20261014-communication-v1-staged-v3/backend'
REMOTE_COMMAND = 'sudo -n -u serviceuser ' + DEV_RELEASE + '/.venv/bin/python -'
SOURCE = r'''
import asyncio,json,os,shlex,sys
from pathlib import Path
sys.path.insert(0, REQUEST['release'])
for line in Path('/var/local/serviceuser/.config/pankagent-vnext/runtime.env').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.split('=',1);words=shlex.split(value);os.environ[key]=words[0] if words else ''

async def main():
    if REQUEST['operation']=='literature':
        from pankagent_vnext.config import Settings
        from pankagent_vnext.literature_sources import LiteratureSources,normalize_sources
        service=LiteratureSources(Settings())
        registry=[]
        async def emit(kind,payload):
            if kind=='literature_sources':payload=normalize_sources(payload,registry)
            print(json.dumps({'type':'event','kind':kind,'payload':payload}),flush=True)
        try:
            result=normalize_sources(await service.search(REQUEST['question'],REQUEST['history'],emit,REQUEST['answer']),registry)
        finally:await service.close()
    else:
        # Pure presentation/resource modules; no query or model gateway constructed.
        from pankgraph_results.layout import LayoutService
        from pankgraph_results.resources import ResourceManager
        from pankgraph_results.config import ResultsSettings
        from pankgraph_results.coordinates import CoordinateLookup
        # Match the retained results asset store, so its download API can serve
        # resolved objects. No graph/state/budget restoration occurs here.
        os.environ['PANK_RESULTS_STATE_DIR']='/var/local/serviceuser/.local/state/pankgraph-results'
        os.environ['PANK_RESULTS_DBSNP_COMMAND']='/var/local/serviceuser/.local/bin/dbsnp-query'
        settings=ResultsSettings()
        layout=LayoutService(max_nodes=settings.display_nodes,timeout_seconds=settings.layout_timeout)
        resources=ResourceManager(settings.state_dir/'resources',CoordinateLookup(settings.dbsnp_command),public_base=settings.public_path+'/api/resources',settings=settings)
        try:
            presentation=await layout.layout(REQUEST['evidence'],REQUEST['focus_ids'])
            print(json.dumps({'type':'event','kind':'presentation','payload':presentation}),flush=True)
            result=await asyncio.wait_for(resources.resolve(REQUEST['evidence']),30)
        finally:await asyncio.gather(layout.close(),resources.close())
    print(json.dumps({'type':'final','result':result}),flush=True)
try:asyncio.run(main())
except Exception as exc:
    import traceback
    frames=[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(exc.__traceback__)[-5:]]
    print(json.dumps({'type':'error','category':type(exc).__name__,'frames':frames}),flush=True)
'''

async def call(operation, on_event=None, **payload):
    payload.update(operation=operation, release=DEV_RELEASE)
    wire='REQUEST='+repr(payload)+'\n'+SOURCE
    if len(wire.encode()) > 2_500_000:
        raise ValueError('Feature evidence exceeds the bounded transport limit')
    argv=[sys.executable,'-'] if os.environ.get('SIMPLE_AGENT_NATIVE') == '1' else ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','jieliulab3-codex',REMOTE_COMMAND]
    process=await asyncio.create_subprocess_exec(*argv,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,limit=8_000_000)
    try:
        process.stdin.write(wire.encode());await process.stdin.drain();process.stdin.close()
        final=None
        async for line in process.stdout:
            value=json.loads(line)
            if value['type']=='error':raise RuntimeError('Retained dev feature failed: '+json.dumps(value))
            if value['type']=='event' and on_event:
                emitted=on_event(value['kind'],value['payload'])
                if inspect.isawaitable(emitted):await emitted
            if value['type']=='final':final=value['result']
        await process.wait()
        if process.returncode or final is None:raise RuntimeError('Retained dev feature stream interrupted')
        return final
    finally:
        if process.returncode is None:
            process.terminate()
            try:await asyncio.wait_for(process.wait(),3)
            except asyncio.TimeoutError:process.kill();await process.wait()

async def literature(question, history, answer, on_event):
    return await call('literature',on_event,question=question,history=history,answer=answer)

async def presentation(evidence, focus_ids, on_event):
    return await call('presentation',on_event,evidence=evidence,focus_ids=focus_ids)
