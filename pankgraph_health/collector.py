"""Bounded, read-only probes and explicit normalization of existing contracts."""
import asyncio
import copy
import hashlib
import html.parser
import json
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

def obj(value):
    return value if isinstance(value, dict) else {}


STATES = {'healthy', 'degraded', 'unavailable', 'unknown'}
LABELS = {'cypher': 'Cypher model access', 'neo4j': 'Knowledge graph', 'claude': 'Claude model access',
          'hirn': 'HIRN literature API', 'claude_provider': 'Claude API service status', 'runtime': 'Agent queue, storage and budget',
          'agent': 'Agent connectivity', 'result_storage': 'Results storage', 'budget': 'Shared Claude budget',
          'layout_worker': 'Graph layout', 'query_adapter': 'Template query execution', 'resources': 'Supplemental resources',
          'synthesis': 'Result synthesis', 'functional_api': 'Functional proxy operation'}
DETAIL_KEYS = {'owner_state', 'owner_epoch', 'persistence_queue_depth', 'audit_dropped', 'model', 'prompt_version', 'backends_up', 'authenticated', 'auth_ok', 'identity_verified',
              'graph_version', 'database_role_enforced', 'read_only_enforcement', 'database_auth_enabled',
              'storage', 'durable', 'active_queries', 'queue_depth', 'capacity', 'remaining_usd', 'spent_usd',
              'reserved_usd', 'limit_usd', 'provider_indicator', 'api_component_status', 'provider_component_id', 'corpus_version', 'source_policy',
              'calls', 'cache_hits', 'fallbacks', 'timeouts', 'worker_active', 'cache_entries', 'active_fetches'}
OPERATIONS = {'layout_worker', 'query_adapter', 'resources', 'synthesis', 'functional_api'}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch is not None else None


def text(value):
    return re.sub(r'[^a-zA-Z0-9 _./:+-]', '', str(value or ''))[:160]


def category(value):
    allowed={'authentication','authorization','billing','budget_exhausted','not_configured','graph_identity','stale_observation',
             'timeout','rate_limited','connection','invalid_response','query_validation','dependency_unavailable','internal_error',
             'queue_full','cancelled','service_restarted','upstream_unavailable','access_denied','not_found','upstream_http_error',
             'upstream_timeout','upstream_unreachable','schema_mismatch','cache_capacity_exceeded','coordinate_provider_unavailable',
             'plot_generation_failed','plot_dependency_unavailable','unknown_observation','query_failed','validation_rejected','service_ownership'}
    return value if isinstance(value,str) and value in allowed else 'upstream_error' if value else None


def details(values):
    return {k: (v if isinstance(v, (bool, int, float)) and (not isinstance(v, float) or math.isfinite(v)) else text(v))
            for k, v in obj(values).items() if k in DETAIL_KEYS and isinstance(v, (str, int, float, bool))}


def observation(key, label, state='unknown', **kwargs):
    return {'id': key, 'label': label, 'state': state if isinstance(state,str) and state in STATES else 'unknown',
            'kind': 'access', 'required': False, 'checked_at': None, 'age_seconds': None,
            'latency_ms': None, 'error_category': None, **kwargs}


def component(prefix, key, value, required=False):
    value = obj(value)
    state = value.get('state', 'unknown')
    if value.get('stale'):
        state = 'unknown'
    c = observation(prefix+'.'+key, LABELS.get(key, key.replace('_', ' ').title()), state,
                    required=required, checked_at=value.get('checked_at'), age_seconds=value.get('age_seconds'),
                    latency_ms=value.get('latency_ms'), last_success=value.get('last_success'),
                    error_category=category(value.get('error_category')), details=details(value.get('details')),
                    kind='operation' if prefix == 'results' and key in OPERATIONS else 'access')
    if prefix == 'agent' and key == 'claude_provider':
        c['scope'] = "Anthropic's published Claude API status. Authenticated model access is checked separately."
    # Preserve upstream hard inference failures even when the operation is old.
    if value.get('state') == 'unavailable' and isinstance(value.get('error_category'),str) and value.get('error_category') in {'authentication','authorization','billing','budget_exhausted','not_configured','graph_identity'}:
        c['state'] = 'unavailable'
    inference = value.get('recent_inference')
    if isinstance(inference, dict) and key != 'claude_provider':
        c['operation'] = {k: inference.get(k) for k in ('state','checked_at','last_success','age_seconds','stale')}
        c['operation']['error_category'] = category(inference.get('error_category'))
    return c


class Scripts(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.sources = []; self.has_root = False
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'script' and values.get('src'): self.sources.append(values['src'])
        if values.get('id') == 'root': self.has_root = True


class Collector:
    def __init__(self, settings, history, transport=None):
        self.settings, self.history = settings, history
        self.http = httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False, transport=transport)
        # Public dev delivery has a separate cookie/auth context from operator
        # and demo probes. Its origin is fixed in code, never supplied by a user.
        self.dev_http = httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False, transport=transport)
        self.current = None
        self.last_cycle = None
        self.last_error = None
        self.task = None
        self.asset = None
        self.asset_checked = 0
        self.dev_asset = None
        self.dev_asset_checked = 0
        self.dev_asset_sha256 = None
        self.started = time.time()

    async def get(self, url, headers=None, max_bytes=1024*1024):
        start = time.monotonic()
        try:
            async with self.http.stream('GET', url, headers=headers or {}) as response:
                if response.status_code not in (200, 503):
                    error = 'authentication' if response.status_code in (401,403) else 'http_'+str(response.status_code)
                    return {'error': error, 'status': response.status_code}
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes: return {'error': 'response_too_large'}
                return {'body': bytes(body), 'status': response.status_code,
                        'latency_ms': round((time.monotonic()-start)*1000, 2)}
        except httpx.TimeoutException: return {'error': 'timeout'}
        except httpx.HTTPError: return {'error': 'connection'}

    async def json_get(self, url, headers=None):
        response = await self.get(url, headers)
        if 'body' in response:
            try:
                response['data'] = json.loads(response.pop('body'))
                if not isinstance(response['data'], dict): response['error'] = 'invalid_response'; response['data'] = {}
            except (ValueError, UnicodeError): response['error'] = 'invalid_response'
        return response

    async def service(self, name, base, version, headers):
        results = await asyncio.gather(*(self.json_get(base+'/health/'+route, headers) for route in ('live','ready','components')),
                                       self.get(base+'/metrics', headers))
        live, ready, data, metric = results
        out = []
        for route, response in zip(('live','ready','components'), results):
            value = response.get('data', {})
            error = response.get('error')
            if not error and value.get('version') != version: error = 'unsupported_contract'
            if not error and route == 'ready' and (not isinstance(value.get('ready'), bool) or not isinstance(value.get('state'),str) or value.get('state') not in STATES): error = 'invalid_response'
            if not error and route == 'components' and not isinstance(value.get('components'), dict): error = 'invalid_response'
            state = 'unavailable' if error else ('healthy' if route in {'live','components'} else value.get('state', 'unknown'))
            if route == 'ready' and value.get('ready') is False: state = 'unavailable'
            if response.get('status') == 503: state = 'unavailable'
            out.append(observation(name+'.'+route, name.title()+' '+{'live':'API liveness','ready':'readiness','components':'telemetry access'}[route],state,
                       required=True, checked_at=iso(time.time()),age_seconds=0,error_category=error,latency_ms=response.get('latency_ms')))
        valid = out[2]['state'] == 'healthy'
        payload = data.get('data', {}) if valid else {}
        names = list(('cypher','neo4j','claude','runtime','hirn','claude_provider') if name == 'agent' else ('agent','neo4j','result_storage','budget','layout_worker','query_adapter','resources','synthesis','functional_api'))
        for key in names:
            required = key in ({'cypher','neo4j','claude','runtime'} if name=='agent' else {'neo4j','result_storage'})
            out.append(component(name,key,payload.get('components',{}).get(key,{}),required))
        if name == 'results':
            owner = obj(payload.get('ownership'))
            active = owner.get('state') == 'active' and isinstance(owner.get('expires_at'), (int, float)) and owner['expires_at'] > time.time()
            out.append(observation('results.ownership', 'Results job ownership',
                'healthy' if active else 'unavailable' if owner else 'unknown', required=True,
                checked_at=iso(time.time()), age_seconds=0,
                error_category=None if active else 'service_ownership' if owner else 'unknown_observation',
                details=details({'owner_state': owner.get('state'), 'owner_epoch': owner.get('epoch')})))
        # Parse only recognized numeric metric families, never return raw metric text/labels.
        metrics = {}
        if not metric.get('error') and metric.get('status') == 200:
            for line in metric.get('body', b'').decode('utf-8',errors='replace').splitlines():
                match = re.fullmatch(r'(pankagent_[a-z_]+|pank_results_[a-z_]+)(\{[^\n]{0,160}\})? ([0-9.eE+-]+)',line)
                if match:
                    try:
                        value = float(match[3])
                        if math.isfinite(value):
                            labels = match[2] or ''
                            if labels and not re.fullmatch(r'\{stage="(?:plan_ready|graph_answer|run_complete)"(?:,quantile="(?:0.5|0.95)")?\}', labels):
                                continue
                            metrics[match[1]+labels] = value
                    except ValueError: pass
        out.append(observation(name+'.metrics', name.title()+' metrics', 'unavailable' if metric.get('error') or metric.get('status') != 200 else 'healthy' if metrics else 'unknown',
                   error_category=metric.get('error') or ('metrics_http_error' if metric.get('status') != 200 else None if metrics else 'no_metrics'), checked_at=iso(time.time()),age_seconds=0))
        runtime = obj(payload.get('components',{}).get('runtime')).get('details',{})
        return out, metrics, payload, runtime

    async def simple(self, key, label, url, replica=False):
        result = await self.json_get(url)
        value = result.get('data',{})
        raw = value.get('state',value.get('status'))
        raw = raw if isinstance(raw,str) else ''
        mapped = {'ok':'healthy','up':'healthy','down':'unavailable','error':'unavailable'}.get(raw,raw)
        state = mapped if mapped in STATES else 'unknown'
        if result.get('error') or result.get('status') == 503: state = 'unavailable'
        if value.get('ok') is False or value.get('ready') is False: state = 'unavailable'
        if not result.get('error') and result.get('status') == 200 and value.get('ok') is not False and value.get('ready') is not False and state != 'unavailable' and replica and isinstance(value.get('backends_up'), int):
            state = 'healthy' if value['backends_up'] >= 2 else 'degraded' if value['backends_up'] == 1 else 'unavailable'
        return observation(key,label,state,checked_at=iso(time.time()),age_seconds=0,latency_ms=result.get('latency_ms'),
                           error_category=result.get('error') or (None if state!='unknown' else 'unrecognized_health_contract'),details=details(value))

    async def cypher(self, key, label, base, gateway=False):
        if not base:
            return observation(key, label, 'unknown', error_category='not_configured',
                               scope='No endpoint is configured; no probe was sent.')
        return await self.simple(key, label, base + '/health', gateway)

    async def frontend(self):
        # Public app requires Basic auth; checking on-disk deployed bytes would miss HTTP failure.
        # Its login boundary is itself checked here. Delivery check uses a server-side optional credential.
        headers = getattr(self.settings,'frontend_headers',{})
        url = self.settings.results_url+'/pankgraph-vnext/'
        result = await self.get(url,headers)
        if result.get('error') or result.get('status') != 200:
            return observation('frontend.delivery','Frontend delivery','unavailable',required=True,checked_at=iso(time.time()),age_seconds=0,error_category=result.get('error') or 'frontend_http_error')
        parser=Scripts()
        try: parser.feed(result.get('body',b'').decode('utf-8'))
        except (UnicodeError,ValueError): pass
        asset = next((s for s in parser.sources if '/static/js/main.' in s),None)
        absolute = urljoin(url,asset or '')
        valid = parser.has_root and asset and urlparse(absolute).netloc == urlparse(url).netloc and urlparse(absolute).path.startswith('/pankgraph-vnext/static/js/')
        error = None
        if valid and (asset != self.asset or time.time()-self.asset_checked > 300):
            fetched = await self.get(absolute,headers,max_bytes=8*1024*1024)
            if fetched.get('error') or fetched.get('status')!=200 or not fetched.get('body'):
                error = fetched.get('error') or 'bundle_unavailable'
            else: self.asset=asset; self.asset_checked=time.time()
        if not valid: error='invalid_frontend_document'
        return observation('frontend.delivery','Frontend HTML and main bundle','unavailable' if error else 'healthy',required=True,
            checked_at=iso(time.time()),age_seconds=0,latency_ms=result.get('latency_ms'),error_category=error,
            details={'bundle':asset if valid else None,'bundle_checked_at':iso(self.asset_checked) if self.asset_checked else None},
            scope='Delivery only; browser interaction and answer correctness require separate acceptance.')

    async def dev_get(self, path, max_bytes=1024*1024):
        # Accept only the fixed dev index and its single hashed main JS asset.
        if path != '/' and not re.fullmatch(r'/static/js/main\.[A-Za-z0-9_-]{1,128}\.js', path):
            return {'error': 'invalid_dev_asset'}
        start = time.monotonic()
        response = None
        try:
            request = self.dev_http.build_request('GET', 'https://dev.pankgraph.org' + path)
            for name in ('authorization', 'cookie', 'x-api-key', 'x-operator-token'):
                request.headers.pop(name, None)
            response = await self.dev_http.send(request, stream=True)
            if response.status_code != 200:
                return {'error': 'http_' + str(response.status_code)}
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    return {'error': 'response_too_large'}
            return {'body': bytes(body), 'content_type': response.headers.get('content-type', '').split(';')[0].lower(),
                    'latency_ms': round((time.monotonic()-start)*1000, 2)}
        except httpx.TimeoutException:
            return {'error': 'timeout'}
        except httpx.HTTPError:
            return {'error': 'connection'}
        finally:
            if response is not None:
                await response.aclose()

    async def dev_frontend(self):
        result = await self.dev_get('/')
        error = result.get('error')
        parser = Scripts()
        asset = None
        if not error:
            try:
                parser.feed(result.get('body', b'').decode('utf-8'))
            except (UnicodeError, ValueError):
                error = 'invalid_frontend_document'
            source = next((src for src in parser.sources if '/static/js/main.' in src), None)
            parsed = urlparse(urljoin('https://dev.pankgraph.org/', source or ''))
            if (result.get('content_type') != 'text/html' or not parser.has_root or not source
                    or parsed.scheme != 'https' or parsed.netloc != 'dev.pankgraph.org'
                    or parsed.query or parsed.fragment or parsed.params
                    or not re.fullmatch(r'/static/js/main\.[A-Za-z0-9_-]{1,128}\.js', parsed.path)):
                error = 'invalid_frontend_document'
            else:
                asset = parsed.path
        if not error and (asset != self.dev_asset or time.time()-self.dev_asset_checked > 300):
            fetched = await self.dev_get(asset, max_bytes=8*1024*1024)
            error = fetched.get('error')
            if not error and (fetched.get('content_type') not in ('application/javascript', 'text/javascript', 'application/ecmascript', 'text/ecmascript') or not fetched.get('body')):
                error = 'invalid_bundle_response'
            if not error:
                self.dev_asset = asset
                self.dev_asset_checked = time.time()
                self.dev_asset_sha256 = hashlib.sha256(fetched['body']).hexdigest()
        return observation('dev.frontend.delivery', 'Dev frontend HTML and main bundle',
            'unavailable' if error else 'healthy', required=True, checked_at=iso(time.time()), age_seconds=0,
            latency_ms=result.get('latency_ms'), error_category=error,
            details={'origin': 'https://dev.pankgraph.org', 'bundle': asset,
                     'html_sha256': hashlib.sha256(result['body']).hexdigest() if result.get('body') else None,
                     'bundle_checked_at': iso(self.dev_asset_checked) if self.dev_asset_checked else None,
                     'bundle_sha256': self.dev_asset_sha256 if asset == self.dev_asset else None},
            scope='Public dev HTML and JavaScript delivery only; no credentials, scientific queries or inference. Browser interaction requires separate acceptance.')

    async def collect(self):
        token = {'Authorization':'Bearer '+self.settings.agent_token} if self.settings.agent_token else {}
        agent, results, frontend, gateway, replica_a, replica_b, functional, dev_frontend = await asyncio.gather(
            self.service('agent',self.settings.agent_url,2,token),self.service('results',self.settings.results_url,1,{}),
            self.frontend(),self.cypher('cypher.gateway','Cypher gateway',self.settings.cypher_url,True),
            self.cypher('cypher.replica_a','Cypher replica A',self.settings.cypher_replica_a_url),
            self.cypher('cypher.replica_b','Cypher replica B',self.settings.cypher_replica_b_url),
            self.simple('functional.api','Functional data API',self.settings.functional_url), self.dev_frontend())
        components = agent[0]+results[0]+[frontend,dev_frontend,gateway,replica_a,replica_b,functional]
        resources = obj(results[2].get('resources'))
        # Resource operations remain observational, not fresh HTTP download probes.
        for key,value in list(resources.get('source_observations',{}).items())[:40] if isinstance(resources.get('source_observations'),dict) else []:
            if isinstance(value,dict): components.append({**component('resource',text(key),value), 'kind':'operation'})
        plot=resources.get('plot_generation',{})
        if isinstance(plot,dict): components.append({**component('resource','plots',plot),'kind':'operation'})
        budget = details(results[2].get('budget') or agent[3])
        budget = {k:v for k,v in budget.items() if k.endswith('_usd')}
        queues = {'agent':{k:v for k,v in details(agent[3]).items() if k in {'active_queries','queue_depth','capacity'}},
                  'results':{k:v for k,v in obj(results[2].get('queue')).items() if k in {'active','depth','capacity'} and isinstance(v,(int,float))}}
        now=time.time()
        snapshot={'version':1,'collected_at':iso(now),'collected_epoch':now,'interval_seconds':self.settings.interval,
                  'components':components,'metrics':{'agent':agent[1],'results':results[1]},'budget':budget,'queues':queues,
                  'layout':details(results[2].get('layout')),'resource_activity':details(resources),
                  'scope':'Read-only operational observations. No inference, scientific-query execution or automatic application repair.'}
        await asyncio.to_thread(self.history.save,snapshot,now)
        self.current=snapshot;self.last_cycle=now;self.last_error=None

    async def loop(self):
        while True:
            try: await asyncio.wait_for(self.collect(),20)
            except asyncio.CancelledError: raise
            except Exception: self.last_error='collection_failed'
            await asyncio.sleep(self.settings.interval)

    def snapshot(self):
        now=time.time();data=copy.deepcopy(self.current) if self.current else {'version':1,'components':[],'metrics':{},'queues':{},'budget':{},'collected_at':None}
        age=now-self.last_cycle if self.last_cycle else None
        stale=age is None or age>90
        for c in data['components']:
            if isinstance(c.get('age_seconds'),(int,float)): c['age_seconds']=round(c['age_seconds']+(age or 0),1)
            if c.get('operation'):
                operation = c['operation']
                if isinstance(operation.get('age_seconds'), (int,float)): operation['age_seconds'] += age or 0
                if stale: operation.update(last_observed_state=operation.get('state'),state='unknown',stale=True)
            if stale:c['state']='unknown';c['error_category']='collector_stale'
        data['monitor']={'state':'unknown' if stale else 'degraded' if self.last_error else 'healthy','age_seconds':age,'error_category':self.last_error,'started_at':iso(self.started)}
        data['stale']=stale
        return data

    async def start(self): self.task=asyncio.create_task(self.loop())
    async def close(self):
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        await self.http.aclose()
        await self.dev_http.aclose()
