import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest

from pankgraph_health.app import create_app, valid_auth, PREFIX
from pankgraph_health.collector import Collector, component, Scripts, observation
from pankgraph_health.settings import Settings, protected_values
from pankgraph_health.store import History


def settings(tmp_path):
    digest=hashlib.pbkdf2_hmac('sha256',b'example',b'salt',100000).hex()
    return Settings(tmp_path,'test',f'pbkdf2_sha256$100000$salt${digest}')


def auth():return {'Authorization':'Basic '+base64.b64encode(b'test:example').decode()}


def test_job_owner_health_and_redaction(tmp_path):
    async def run():
        for expires, expected in ((time.time()+60, 'healthy'), (time.time()-1, 'unavailable')):
            def handle(req):
                response = fixture(req)
                if req.url.path.endswith('/components'):
                    payload = response.json()
                    payload['ownership'] = {'state': 'active', 'epoch': 4, 'expires_at': expires, 'owner_id': 'PRIVATE_OWNER'}
                    return httpx.Response(200, json=payload)
                return response
            collector = Collector(settings(tmp_path), History(tmp_path), httpx.MockTransport(handle))
            rows, *_ = await collector.service('results', 'http://127.0.0.1:8795', 1, {})
            owner = next(row for row in rows if row['id'] == 'results.ownership')
            assert owner['state'] == expected and owner['details']['owner_epoch'] == 4
            assert 'PRIVATE_OWNER' not in json.dumps(rows)
            await collector.close()
    asyncio.run(run())


def fixture(request):
    agent=request.url.port==8794
    if request.url.path.endswith('metrics'):return httpx.Response(200,text='pankagent_queue_depth 0\npank_results_active 0\nsecret_token abc\n')
    if request.url.path.endswith('/components'):
        return httpx.Response(200,json={'version':2 if agent else 1,'components':{'claude':{'state':'healthy','stale':False,'details':{'model':'configured','token':'NEVER_EXPORT'},'recent_inference':{'state':'unknown','stale':True}}},'budget':{'remaining_usd':5,'api_key':'NEVER_EXPORT'}})
    if request.url.path.endswith('/live'):return httpx.Response(200,json={'version':2 if agent else 1,'state':'healthy','service':'pankagent-vnext' if agent else 'pankgraph-results'})
    if request.url.path.endswith('/ready'):return httpx.Response(200,json={'version':2 if agent else 1,'state':'healthy','ready':True})
    if request.url.path=='/pankgraph-vnext/':return httpx.Response(200,text='<div id="root"></div><script src="/pankgraph-vnext/static/js/main.demo.js"></script>')
    if request.url.path.endswith('.js'):return httpx.Response(200,text='console.log("bundle")')
    return httpx.Response(200,json={'status':'healthy','backends_up':2})


def test_read_only_cycle_redacts_and_persists(tmp_path):
    async def run():
        calls=[]
        def handle(req):calls.append(req);return fixture(req)
        store=History(tmp_path);c=Collector(settings(tmp_path),store,httpx.MockTransport(handle))
        await c.collect();snap=c.snapshot()
        assert all(req.method=='GET' for req in calls)
        assert all('/v1/cypher' not in req.url.path for req in calls)
        assert 'NEVER_EXPORT' not in json.dumps(snap)
        assert snap['budget']['remaining_usd']==5
        assert snap['monitor']['state']=='healthy'
        assert next(x for x in snap['components'] if x['id']=='agent.claude')['operation']['state']=='unknown'
        assert store.history()['samples']==1
        await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('status,error',[(401,'authentication'),(403,'authentication'),(500,'http_500')])
def test_probe_http_failures(tmp_path,status,error):
    async def run():
        c=Collector(settings(tmp_path),History(tmp_path),httpx.MockTransport(lambda _:httpx.Response(status)))
        r=await c.json_get('http://127.0.0.1:8794/health/components');assert r['error']==error
        await c.close()
    asyncio.run(run())


def test_bad_json_wrong_version_timeout_and_size(tmp_path):
    async def run():
        cases=[(lambda _:httpx.Response(200,text='not json'),'invalid_response'),(lambda _:httpx.Response(200,json=[]),'invalid_response')]
        for fn,error in cases:
            c=Collector(settings(tmp_path),History(tmp_path),httpx.MockTransport(fn));assert (await c.json_get('http://localhost'))['error']==error;await c.close()
        def timeout(_):raise httpx.ReadTimeout('secret exception')
        c=Collector(settings(tmp_path),History(tmp_path),httpx.MockTransport(timeout));assert (await c.get('http://localhost'))['error']=='timeout';await c.close()
        c=Collector(settings(tmp_path),History(tmp_path),httpx.MockTransport(lambda _:httpx.Response(200,json={'version':9})))
        out=await c.service('agent','http://localhost',2,{})
        assert out[0][2]['error_category']=='unsupported_contract';await c.close()
        c=Collector(settings(tmp_path),History(tmp_path),httpx.MockTransport(lambda _:httpx.Response(200,text='a'*100)))
        assert (await c.get('http://localhost',max_bytes=10))['error']=='response_too_large';await c.close()
    asyncio.run(run())


def test_stale_collector_and_idle_observations(tmp_path):
    c=Collector(settings(tmp_path),History(tmp_path));c.last_cycle=time.time()-100
    c.current={'components':[observation('agent.claude','Claude','healthy',age_seconds=1)]}
    snap=c.snapshot();assert snap['stale'] and snap['components'][0]['state']=='unknown'
    hard=component('agent','claude',{'state':'unavailable','stale':True,'error_category':'billing'})
    assert hard['state']=='unavailable'
    asyncio.run(c.close())


def test_incident_debounce_recovery_restart_retention(tmp_path):
    h=History(tmp_path);now=time.time()
    def save(state,offset):h.save({'components':[observation('a','A',state),observation('idle','Idle','unknown',kind='operation')]},now+offset)
    save('unavailable',0);assert h.incidents()==[]
    save('unavailable',30);assert len(h.incidents())==1
    restarted=History(tmp_path);assert len(restarted.incidents())==1
    save('healthy',60);assert h.incidents()[0]['ended'] is None
    save('healthy',90);assert h.incidents()[0]['ended'] is not None
    save('healthy',8*86400);assert h.history(now=now+8*86400)['samples']==1


def test_history_preserves_bad_state_in_buckets(tmp_path):
    h=History(tmp_path);now=time.time()
    for i in range(190):h.save({'components':[observation('a','A','unavailable' if i==1 else 'healthy')]},now+i)
    result=h.history(now=now+200);assert len(result['points'])<=180
    assert any(p['components']['a']=='unavailable' for p in result['points'])
    assert any(p['samples']==0 for p in result['points'])


def test_auth_routes_and_no_collection_from_reads(tmp_path):
    async def run():
        cfg=settings(tmp_path);c=Collector(cfg,History(tmp_path),httpx.MockTransport(fixture));await c.collect();before=c.last_cycle
        app=create_app(cfg,c)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as http:
            assert (await http.get(PREFIX+'/api/snapshot')).status_code==401
            r=await http.get(PREFIX+'/api/snapshot',headers=auth());assert r.status_code==200
            assert 'NEVER_EXPORT' not in r.text
            assert (await http.post(PREFIX+'/api/snapshot',headers=auth())).status_code==405
            assert (await http.get(PREFIX+'/api/history?hours=999',headers=auth())).status_code==422
            assert (await http.get(PREFIX+'/api/metrics',headers=auth())).status_code==200
            empty = await http.get(PREFIX+'/api/incidents',headers=auth())
            assert empty.status_code == 200 and isinstance(empty.json()['incidents'], list)
            for offset in (0,30):
                c.history.save({'components':[observation('test.api','Test API','unavailable',error_category='connection')]},time.time()+offset)
            incidents = await http.get(PREFIX+'/api/incidents',headers=auth())
            assert incidents.status_code == 200
            assert any(row['component']=='test.api' and row['state']=='unavailable' for row in incidents.json()['incidents'])
            historical = await http.get(PREFIX+'/api/history?hours=1',headers=auth())
            assert historical.status_code == 200 and isinstance(historical.json()['points'],list)
            html=await http.get(PREFIX+'/',headers=auth());assert html.status_code==200 and 'Content-Security-Policy' in html.headers
            assert (await http.get(PREFIX+'/settings.py',headers=auth())).status_code==404
            assert (await http.get('/health/live',headers={'x-forwarded-for':'1.2.3.4'})).status_code==401
        assert c.last_cycle==before;await c.close()
    asyncio.run(run())


def test_protected_configuration_and_password(tmp_path):
    cfg=settings(tmp_path);assert valid_auth(auth()['Authorization'],cfg)
    assert not valid_auth('Basic invalid',cfg)
    p=tmp_path/'env';p.write_text('TOKEN="literal$no_execution"\n');p.chmod(0o644)
    with pytest.raises(ValueError):protected_values(p)
    p.chmod(0o600);assert protected_values(p)['TOKEN']=='literal$no_execution'
    link=tmp_path/'link';link.symlink_to(p)
    with pytest.raises(ValueError):protected_values(link)


def test_false_green_and_malformed_nested_payloads(tmp_path):
    async def run():
        for body,status in [({'status':'healthy','backends_up':2,'ok':False},200),({'status':'healthy','backends_up':2},503)]:
            c=Collector(settings(tmp_path),History(tmp_path),httpx.MockTransport(lambda _:httpx.Response(status,json=body)))
            assert (await c.simple('replica','Replica','http://localhost',True))['state']=='unavailable'
            await c.close()
        c=Collector(settings(tmp_path),History(tmp_path),httpx.MockTransport(lambda _:httpx.Response(200,json=[])))
        assert (await c.service('agent','http://localhost',2,{}))[0][0]['state']=='unavailable'
        assert (await c.simple('replica','Replica','http://localhost',True))['state']=='unavailable'
        await c.close()
        assert component('agent','runtime',None)['state']=='unknown'
        c=Collector(settings(tmp_path),History(tmp_path),httpx.MockTransport(lambda _:httpx.Response(503,text='<div id="root"></div><script src="/pankgraph-vnext/static/js/main.demo.js"></script>')))
        assert (await c.frontend())['state']=='unavailable'
        await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('state',[[],{},None,True])
def test_malformed_state_never_breaks_collection(tmp_path,state):
    async def run():
        def handle(req):
            if req.url.path.endswith('metrics'):return httpx.Response(503,text='pankagent_queue_depth 0')
            return httpx.Response(200,json={'version':2,'ready':True,'state':state,'components':{'claude':{'state':'unavailable','error_category':[]},'runtime':None}})
        c=Collector(settings(tmp_path),History(tmp_path),httpx.MockTransport(handle))
        out=await c.service('agent','http://localhost',2,{})
        assert out[0][-1]['state']=='unavailable'
        assert (await c.simple('replica','Replica','http://localhost',True))['state']=='unknown'
        assert component('agent','claude',{'state':state,'error_category':[]})['state']=='unknown'
        await c.close()
    asyncio.run(run())
