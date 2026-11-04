"""Isolated agent backend with the existing frontend's plan/confirm API."""
import os
from pathlib import Path
from fastapi import FastAPI
from .core import Agent, SchemaPack
from .transport import DevTransport
from .workflow import Workflow, router
from .formatter import format_final
from .features import literature, presentation
ROOT = Path(__file__).resolve().parents[1]
pack = SchemaPack(ROOT / 'schema_pack.json')
transport = DevTransport()
app = FastAPI(title='PanKagent simple-loop isolated demo')
@app.on_event('startup')
async def initialize_indexes():
    try:
        await transport.initialize()
    except Exception as exc:
        # Keep the agent available: resolver returns useful inventory feedback.
        app.state.index_inventory_error = type(exc).__name__

@app.get('/health')
def health():
    return {'status': 'live', 'demo': 'isolated', 'schema_pack_sha256': pack.sha256,
            'model': os.environ.get('SIMPLE_AGENT_MODEL', 'claude-haiku-5-5'), 'database_write_tools': False}
async def prepare_agent(question, history, on_progress=None):
    return await Agent(pack, transport, model=os.environ.get('SIMPLE_AGENT_MODEL', 'claude-haiku-5-5')).run(question, history, on_progress)
async def format_answer(question, result, on_delta=None):
    return await format_final(question, result, pack.data.get('release'), on_delta)
workflow = Workflow(prepare_agent, os.environ.get('SIMPLE_AGENT_STATE_DIR', str(ROOT / 'state' / 'confirmed-workflow')), format_answer,
                    literature=literature, presentation=presentation, graph_release=pack.data.get('release'))
app.include_router(router(workflow))
