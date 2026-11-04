"""Dataset-independent API transport. Storage contracts stay behind the API."""
import copy
import time
import httpx
from .core import Feedback, quoted
from .resolver import resolve


class IntegratedTransport:
    def __init__(self, config, token, model_client):
        self.config, self.token, self.model_client = dict(config), token, model_client
        self.base = config['query_url'].rstrip('/')
        self.release = config['release_id']
        self.indexes = None

    async def post(self, path, body):
        async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
            r = await client.post(self.base+path, json=body,
                                  headers={'Authorization':'Bearer '+self.token})
            if r.status_code >= 400:
                raise Feedback('Query service returned HTTP '+str(r.status_code)+': '+r.text[:500])
            return r.json()

    async def get(self, path):
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            r=await client.get(self.base+path,headers={'Authorization':'Bearer '+self.token})
            r.raise_for_status();return r.json()

    async def verify_release(self, pack):
        live = await self.get('/api/viewer/v1/release')
        if live['release_id'] != self.release or pack.data['release'] != self.release:
            raise Feedback('Schema/API release mismatch')
        if live['input_manifest_sha256'] != pack.data['identity']['input_manifest_sha256']:
            raise Feedback('Schema/API frozen-input identity mismatch')
        if live['status'] != {'graph':'available','evidence':'available'}:
            raise Feedback('Paired stores are not both available')
        return live

    async def initialize(self):
        if self.indexes is None:
            self.indexes=(await self.post('/api/kg/v1/agent',{'action':'indexes'}))['rows']
        return self.indexes

    async def _raw(self, cypher, parameters, cap):
        return await self.post('/api/kg/v1/agent',{'action':'query','cypher':cypher,
            'parameters':parameters,'row_cap':cap,'detail':'none'})

    async def _fulltext(self,index,text,cap=200):
        return await self.post('/api/kg/v1/agent',{'action':'resolver_fulltext',
            'index':index,'text':text,'cap':cap})

    async def call(self, action, args):
        if action == 'model':
            if self.model_client is None:
                raise RuntimeError('Native model authentication is not configured; no fallback')
            return await self.model_client.model(args)
        if action == 'resolve':
            return await resolve(self,args)
        if action == 'values':
            field='x.'+quoted(args['property'])
            match='MATCH (x:'+quoted(args['type'])+')' if args['owner']=='node' else 'MATCH ()-[x:'+quoted(args['type'])+']->()'
            raw=await self._raw(match+' WHERE '+field+' IS NOT NULL WITH '+field+' AS value LIMIT 500 RETURN DISTINCT value LIMIT 31',{},30)
            values=[r['value'] for r in raw['rows']]
            return {'values':values,'python_types':sorted({type(v).__name__ for v in values}),
                    'coverage':'Bounded property sample, not complete inventory','truncated':raw['truncated']}
        if action == 'query':
            start=time.monotonic();detail=args.get('detail','summary')
            shape=args.get('query_shape',{'nodes':1,'edges':0});limits=self.config.get('query_limits',{'nodes':100,'edges':400})
            if shape['nodes']>limits['nodes'] or shape['edges']>limits['edges']:
                raise Feedback('Pattern exceeds this API graph delivery budget; split requested branches explicitly')
            cap=min(args['row_cap'],limits['nodes']//max(1,shape['nodes']),limits['edges']//max(1,shape['edges']))
            raw=await self.post('/api/kg/v1/cypher',{'release_id':self.release,
                'cypher':args['cypher'],'parameters':args['parameters'],'row_limit':cap,
                'object_limit':10,'detail':detail})
            if raw['release_id'] != self.release:
                raise Feedback('Result release mismatch')
            flags=raw['truncated'];partial=flags['rows'] or flags['graph']
            graph=raw['graph']
            return {'graph':graph,'status':'partial' if partial else 'complete' if raw['records'] else 'empty',
                'row_count':len(raw['records']),'truncated':partial,'omitted_records':0,
                'execution_s':time.monotonic()-start,'notifications':raw['notifications'],
                'linked_evidence':raw['evidence'],
                'evidence_coverage':{'detail':detail,'descriptors_truncated':flags['evidence_objects'],
                    'note':'Only exact returned objects. Inspect a returned ref for registered tables; missing mappings do not imply zero.'},
                'provenance':{'database':self.release,'source_snapshots':raw['source_snapshots'],
                    'access':'Read-only; exact object/snapshot evidence'}}
        if action == 'evidence':
            ref=args['ref']
            card=await self.post('/api/viewer/v1/inspect',{'release_id':self.release,'ref':ref,'mode':'expanded'})
            if card.get('state') != 'available' or card.get('evidence_state') == 'temporarily_unavailable':
                raise Feedback('Exact-object evidence is temporarily unavailable; no absence conclusion')
            tables=card.get('tables',[])
            if not args.get('table_id'):
                return {'ref':ref,'state':'available','tables':tables,'source':'Exact selected-object mappings'}
            if args['table_id'] not in {t['id'] for t in tables}:
                raise Feedback('Table is not registered/owned by this exact object')
            body={'release_id':self.release,'ref':ref,'table_id':args['table_id'],
                  'limit':args.get('limit',20)}
            for key in ['fields','filters','cursor','record_id']:
                if key in args:body[key]=copy.deepcopy(args[key])
            result=await self.post('/api/viewer/v1/table',body)
            if result.get('state') == 'temporarily_unavailable':
                raise Feedback('Evidence service unavailable; retained graph is unchanged')
            return {'ref':ref,'table_id':args['table_id'],**result}
        raise Feedback('Unknown transport action')
