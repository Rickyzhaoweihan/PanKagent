"""Retired experimental GPT ingress; dev has one canonical agent."""
from fastapi.responses import JSONResponse


def install_gpt6_proxy(app, client):
    @app.api_route('/api/gpt6/', methods=['GET', 'POST'])
    @app.api_route('/api/gpt6/{path:path}', methods=['GET', 'POST'])
    async def retired_gpt_agent(path: str = ''):
        return JSONResponse(
            {'detail': 'The experimental GPT agent has been retired. Use the main dev agent.',
             'agent_url': '/agent-vnext'},
            status_code=410,
            headers={'Cache-Control': 'no-store'},
        )
