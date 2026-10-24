"""Read-only view of the simple agent and the existing hosted dev route."""
import asyncio
import json
import time
from pathlib import Path
from .collector import observation,iso

CORE_IDS = ('agent.query_loop','qwen.model_access','agent.neo4j','results.live',
            'results.result_storage','results.ownership','frontend.hosted_api')

async def extend(collector,snapshot):
    # A missing historical UUID exercises HTTP routing and SQLite lookup only.
    # It never creates a question, queries the KG, or invokes a model.
    async def hosted():
        try:
            async with collector.http.stream('GET','https://dev.pankgraph.org/pankgraph-vnext/api/agent/v2/runs/00000000-0000-0000-0000-000000000000') as response:
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>16384:raise ValueError('size')
                data=json.loads(raw)
                ok=response.status_code in (200,401,403,404) and isinstance(data,dict)
                return observation('frontend.hosted_api','Hosted dev API routing','healthy' if ok else 'unavailable',required=True,
                    checked_at=iso(time.time()),age_seconds=0,error_category=None if ok else 'hosted_routing',
                    scope='HTTP/application routing only; no scientific query or inference canary.')
        except Exception:
            return observation('frontend.hosted_api','Hosted dev API routing','unavailable',required=True,
                checked_at=iso(time.time()),age_seconds=0,error_category='hosted_routing',
                scope='The public dev route is unavailable; loopback services may still be healthy.')
    async def model_access():
        try:
            key = Path('/db/pankagent-vnext-private/operations/lighthouse-direct/state/api-key').read_text().strip()
            raw = await collector.json_get('http://127.0.0.1:33924/v1/models', {'Authorization': 'Bearer '+key})
            rows = raw.get('data',{}).get('data',[])
            valid = not raw.get('error') and raw.get('status') == 200 and isinstance(rows,list) and any(isinstance(x,dict) and x.get('id')=='pank-candidate' for x in rows)
            error = raw.get('error') or (None if valid else 'model_identity_mismatch')
        except Exception:
            raw={}; valid=False; error='model_probe_unavailable'
        return observation('qwen.model_access','Lighthouse Qwen · direct relay access',
            'healthy' if valid else 'unavailable',required=True,checked_at=iso(time.time()),age_seconds=0,
            latency_ms=raw.get('latency_ms'),error_category=error,
            details={'model':'pank-candidate','authenticated_identity_verified':valid,'relay_port':33924,
                     'mac_transit_required':False},
            scope='Authenticated model discovery through the Lighthouse-to-jieliu3 relay. Tests the combined relay/model path, not inference, GPU health or allocation lifetime.')
    raw,route,model=await asyncio.gather(collector.json_get('http://127.0.0.1:18864/health'),hosted(),model_access())
    payload=raw.get('data',{})
    valid=not raw.get('error') and raw.get('status')==200 and payload.get('status')=='live' and payload.get('model')=='pank-candidate' and payload.get('two_loops') is True
    loop=observation('agent.query_loop','Query agent · Qwen 3.8-27B','healthy' if valid else 'unavailable',required=True,
        checked_at=iso(time.time()),age_seconds=0,latency_ms=raw.get('latency_ms'),error_category=None if valid else 'qwen_agent_unavailable',
        details={k:payload.get(k) for k in ('model','schema_pack_sha256','two_loops','candidate')},
        scope='Native seven-tool, two-loop candidate API. API liveness/configuration only; no model inference is run by health checks.')
    components=snapshot['components']
    for row in components:
        if row['id'] in ('agent.ready','agent.cypher','cypher.gateway','cypher.replica_a','cypher.replica_b','agent.claude','agent.claude_provider','results.budget'):
            row.update(required=False,scope='Legacy/optional capability; not required by the native Qwen query loop.')
        if row['id']=='agent.claude':row['label']='Legacy Claude model access'
        if row['id']=='agent.runtime':row.update(required=False,scope='Retained history workflow telemetry; not the Qwen candidate queue.')
    renderer_ok=valid and payload.get('formatter')=='deterministic'
    components.extend([loop,route,model,observation('agent.formatter','Answer renderer · deterministic',
        'healthy' if renderer_ok else 'unknown',checked_at=iso(time.time()),age_seconds=0,
        error_category=None if renderer_ok else 'renderer_not_verified',
        scope='Configured deterministic renderer uses finish.answer; no Sonnet/paid formatter call. Rendering correctness is not checked by this probe.')])
    snapshot.update(architecture='qwen-native-two-loop-direct-lighthouse',core_component_ids=list(CORE_IDS),
        alert_policy={'delivery':'Dashboard observations and incident history',
                      'availability':'Collector runs on jieliu3; no Mac runtime dependency. No email delivery is asserted.'})
    return snapshot
