"""Opt-in 1104 retrieval with the site's retained formatter and presentation."""
import asyncio
import json
from pathlib import Path
from fastapi import FastAPI
from pankagent_query import core, native_client
from pankagent_query.transport import IntegratedTransport
from simple_agent.workflow import Workflow, router
from simple_agent.features import presentation
from simple_agent.openai_formatter import format_final
from release_dispatch import ReleaseDispatch


def install(app, legacy, root, config_path):
    from contextlib import asynccontextmanager
    config = json.loads(Path(config_path).read_text())
    pack = core.SchemaPack(config['schema_path'])
    if pack.data.get('semantics_version') != 'schema-semantics-v1':
        raise RuntimeError('Current query core requires the versioned schema semantics pack')
    assert pack.data['release'] == config['release_id']
    native_client.BASE, native_client.MODEL = config['model_url'], config['model']
    client = native_client.Client(root/'calls-1104', Path(config['model_token_file']).read_text().strip())
    transport = IntegratedTransport(config, Path(config['query_token_file']).read_text().strip(), client)
    async def runner(question, history=None, on_progress=None, **kwargs):
        await transport.verify_release(pack)
        kwargs.setdefault('deadline_s', config.get('agent_deadline_s', 300))
        return await core.Agent(pack, transport, model=config['model'], max_turns=config.get('agent_max_model_calls', 48)).run(question, history, on_progress, **kwargs)
    async def render(question, result, on_delta=None):
        return await format_final(question, result, config['release_id'], on_delta)
    workflow = Workflow(runner, root/'workflow-state-1104', render,
                        literature=None, presentation=presentation, graph_release=config['release_id'])
    workflow.context_identity = core.context_identity(pack)
    current = FastAPI()
    current.include_router(router(workflow))
    @current.get('/health')
    async def health():
        await transport.verify_release(pack)
        return {'status':'live','release_id':config['release_id'],'model':config['model'],
                'formatter':'gpt-6.1-sol','two_loops':True,'schema_pack_sha256':pack.sha256,
                'prompt_version':core.PROMPT_VERSION,'semantics_version':pack.data.get('semantics_version')}
    previous = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(parent):
        async with previous(parent):
            await transport.verify_release(pack)
            await transport.initialize()
            try: yield
            finally:
                tasks = list(workflow.tasks.values()) + list(workflow.clocks.values()) + list(workflow.presentations.values())
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    app.router.lifespan_context = lifespan
    app.add_middleware(ReleaseDispatch, current=current, legacy_workflow=legacy)
    app.state.current_workflow = workflow
    return app
