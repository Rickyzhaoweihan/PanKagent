"""Private API integration of the evaluated Qwen coverage-check candidate."""
import os,asyncio
from pathlib import Path
from contextlib import asynccontextmanager
os.umask(0o077)
os.environ['SIMPLE_AGENT_NATIVE']='1'
import native_client as native
from simple_agent import core
from simple_agent.transport import DevTransport
from simple_agent.workflow import Workflow,router
from simple_agent.features import presentation
from fastapi import FastAPI
ROOT=Path(__file__).resolve().parent
native.BASE='http://127.0.0.1:33924/v1'
native.MODEL='pank-candidate'
core.ENRICHMENT += '\nBefore finishing, cross-check the approved evidence against all entities and conditions requested in the question. Review whether retrieval coverage is sufficient and whether identity, scope or interpretation remains ambiguous. If the answer is already supported, use this phase to verify completeness rather than immediately finishing. Choose any further tool calls based on unresolved uncertainty; do not repeat successful work or require new evidence when the retained evidence suffices. Briefly state the verification outcome and any remaining uncertainty in finish.\n'
pack=core.SchemaPack(ROOT/'schema_pack.json')
class NativeTransport(DevTransport):
    async def call(self,action,args):
        if action=='model':return await self.client.model(args)
        return await super().call(action,args)
transport=NativeTransport()
transport.client=native.Client(ROOT/'calls',(ROOT.parent/'state/api-key').read_text().strip())
async def runner(question,history=None,on_progress=None,**kwargs):
    return await core.Agent(pack,transport,model=native.MODEL).run(question,history,on_progress,**kwargs)
async def render(question,result,on_delta=None):
    return {'answer':result.get('answer',''),'model':'deterministic-evidence-renderer','usage':{},'elapsed_s':0,'stop_reason':'end_turn'}
workflow=Workflow(runner,ROOT/'workflow-state',render,literature=None,presentation=presentation,graph_release=pack.data.get('release'))
@asynccontextmanager
async def lifespan(app):
    await transport.initialize()
    try:yield
    finally:
        tasks=list(workflow.tasks.values())+list(workflow.clocks.values())+list(workflow.presentations.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
app=FastAPI(title='Private Qwen API candidate',lifespan=lifespan)
app.include_router(router(workflow))
@app.get('/health')
def health():
    return {'status':'live','model':native.MODEL,'schema_pack_sha256':pack.sha256,'two_loops':True,'enrichment_deadline_s':workflow.enrichment_seconds,'formatter':'deterministic','candidate':'crosscheck-without-fast-prompt'}
