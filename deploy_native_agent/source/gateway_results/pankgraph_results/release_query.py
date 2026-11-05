"""Release-pinned read-only HTTP adapter for conventional dev queries."""
import json
from pathlib import Path
import httpx
from pankagent_vnext.graph import GraphValidationError

class ReleaseQueryAdapter:
    def __init__(self, settings, url, release, token_file):
        self.settings, self.url, self.release = settings, url.rstrip('/'), release
        self.token = Path(token_file).read_text().strip()
        self.http = httpx.AsyncClient(timeout=40, trust_env=False)

    async def close(self): await self.http.aclose()

    async def _ensure_identity(self):
        response = await self.http.get(self.url+'/api/viewer/v1/release', headers={'Authorization':'Bearer '+self.token})
        response.raise_for_status(); value=response.json()
        if value.get('release_id') != self.release or value.get('status') != {'graph':'available','evidence':'available'}:
            raise GraphValidationError('Configured paired graph/evidence release is unavailable')
        return value

    async def probe(self):
        await self._ensure_identity()
        return {'ready':True,'identity_verified':True,'graph_version':self.release,'read_access':True}

    async def _execute(self, query, parameters, row_limit=1000):
        await self._ensure_identity()
        response=await self.http.post(self.url+'/api/kg/v1/cypher',headers={'Authorization':'Bearer '+self.token},json={
            'release_id':self.release,'cypher':query,'parameters':parameters or {},'detail':'none',
            'row_limit':max(1,min(1000,row_limit)),'object_limit':0})
        if response.status_code >= 400: raise GraphValidationError('Current-release query rejected; inspect the 1104 schema')
        value=response.json()
        if value.get('release_id')!=self.release: raise GraphValidationError('Query release mismatch')
        return value

    async def _explain(self, query, parameters):
        # The paired reader performs EXPLAIN immediately before execution and
        # rejects unknown schema fields. No legacy driver is constructed here.
        return []

    async def _small_query(self, query, params=None):
        value=await self._execute(query,params)
        if any(value['truncated'].values()): raise GraphValidationError('Lookup exceeds the current-release bound')
        return value['records']

    async def _retrieve(self, query, parameters, limits=None):
        limits=limits or {}
        cap=min(getattr(self.settings,'max_rows',1000)-limits.get('used_rows',0),limits.get('max_step_rows',1000))
        if cap<1: return {'nodes':[],'edges':[],'rows':[],'truncated':True,'materialized_bytes':0}
        value=await self._execute(query,parameters,cap)
        nodes=value['graph']['nodes']; edges=[{**e,'start_id':e['source'],'end_id':e['target']} for e in value['graph']['edges']]
        result={'nodes':nodes,'edges':edges,'rows':value['records'],'truncated':any(value['truncated'].values())}
        size=len(json.dumps(result).encode())
        allowance=min(self.settings.max_bytes-limits.get('used_bytes',0),limits.get('max_step_bytes',self.settings.max_bytes))
        if size>allowance or len(nodes)>min(self.settings.max_nodes,limits.get('max_step_nodes',self.settings.max_nodes)) or len(edges)>min(self.settings.max_edges,limits.get('max_step_edges',self.settings.max_edges)):
            raise GraphValidationError('Current-release result exceeds the delivery budget; narrow the request')
        result['materialized_bytes']=size
        return result
