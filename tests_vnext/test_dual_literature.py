import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from pankagent_vnext.config import Settings
from pankagent_vnext.literature_sources import GLKBLiteratureAdapter, LiteratureSources, append_references, normalize_sources, numbered_answer
from pankagent_vnext.store import Store
from tests_vnext.test_runtime import service, Gateway, wait_state


@pytest.fixture
def anyio_backend(): return 'asyncio'


def test_numbering_identity_and_markers():
    registry = []
    append_references(registry, [{'pmid': '39261729', 'doi': 'https://doi.org/10.1/ABC'}], 'glkb')
    append_references(registry, [{'doi': '10.1/abc', 'title': 'Shared paper'}], 'hirn')
    append_references(registry, [{'pmid': '19357644'}], 'hirn')
    assert [r['number'] for r in registry] == [1, 2]
    assert registry[0]['sources'] == ['glkb', 'hirn']
    text = numbered_answer('Count 1; [G1]. Paper [1]; [PMID: 19357644].', [{'pmid':'19357644'}], registry, 'hirn')
    assert text == 'Count 1; [G1]. Paper [2](#citation-2); [2](#citation-2).'
    assert numbered_answer('[39261729](https://pubmed.ncbi.nlm.nih.gov/39261729/)', [{'pmid':'39261729'}], registry, 'glkb') == '[1](#citation-1)'


@pytest.mark.anyio
async def test_glkb_contract_and_bounded_context():
    requests = []
    async def handler(request):
        body = json.loads(request.content); requests.append(body)
        return httpx.Response(200, json={'answer_plain':'Answer', 'references':[{'pmid':'12345678','evidence':'excerpt'}], 'direct_citations':[{'verified':True}], 'model':'gpt-6-luna'})
    adapter = GLKBLiteratureAdapter(Settings(), transport=httpx.MockTransport(handler))
    out = await adapter.search('Q'*6000, ['history'*8000], None, 'graph'*10000)
    assert out['status'] == 'complete'
    assert out['references'][0]['evidence'] == 'excerpt'
    assert len(requests[0]['question']) <= 8000 and 'Q'*6000 in requests[0]['question']
    assert requests[0]['max_articles'] == 20 and 'session_id' not in requests[0]
    await adapter.close()


@pytest.mark.anyio
@pytest.mark.parametrize('status, detail', [(422, [{'msg':'bad'}]), (429, 'full'), (504, 'timeout')])
async def test_glkb_errors_no_retry(status, detail):
    calls=[]
    async def handler(request):
        calls.append(request); return httpx.Response(status, json={'detail':detail})
    adapter=GLKBLiteratureAdapter(Settings(), transport=httpx.MockTransport(handler))
    result=await adapter.search('test', [], None)
    assert result['status']=='unavailable' and len(calls)==1
    await adapter.close()


class Source:
    def __init__(self, name, ready=None, fail=False): self.name,self.ready,self.fail=name,ready,fail
    async def search(self, question, history, emit, *args):
        if self.ready: await self.ready.wait()
        if self.fail: raise RuntimeError('private error')
        return {'status':'complete','answer':self.name+' [1]', 'references':[{'pmid':'12345678','title':'Shared'}]}
    async def probe(self): return {'state':'healthy'}
    async def close(self): pass


@pytest.mark.anyio
async def test_parallel_sibling_survives_failure():
    release=asyncio.Event(); updates=[]
    adapter=LiteratureSources(Settings(), Source('HIRN',release,True), Source('GLKB'))
    async def emit(kind,value): updates.append(value)
    task=asyncio.create_task(adapter.search('Q',[],emit))
    for _ in range(20):
        await asyncio.sleep(.001)
        if any(v.get('sources',{}).get('glkb',{}).get('status')=='complete' for v in updates): break
    assert not task.done()
    assert updates[-1]['sources']['glkb']['answer'].startswith('GLKB')
    release.set(); result=await task
    assert result['status']=='partial' and result['sources']['glkb']['status']=='complete'


def test_early_followup_snapshot_and_restore(tmp_path):
    store=Store(tmp_path)
    first=store.create('INS')
    store.update(first['run_id'],status='running',graph_answer='INS in beta cells',followup_ready=True)
    child=store.create('What about it?',first['session_id'],parent_run_id=first['run_id'])
    snapshot=store.audit_metadata(child['run_id'])
    store.update(first['run_id'],literature={'perspectives':[{'answer':'LATE PAPER','status':'complete'}]})
    assert 'INS in beta cells' in str(snapshot['history_snapshot'])
    assert 'LATE PAPER' not in str(snapshot)
    assert snapshot['followup_parent_snapshot']['run_id']==first['run_id']
    other=store.create('other')
    with pytest.raises(KeyError): store.create('bad',other['session_id'],parent_run_id=first['run_id'])
    store.close(); store=Store(tmp_path)
    assert store.get(first['run_id'])['followup_ready'] is True
    assert store.audit_metadata(child['run_id'])==snapshot
    store.close()


@pytest.mark.anyio
async def test_graph_slot_released_and_followup_before_literature(tmp_path):
    release=asyncio.Event()
    adapter=LiteratureSources(Settings(),Source('HIRN',release),Source('GLKB',release))
    async with service(tmp_path,literature=adapter,max_concurrent=1) as (client,runtime,*_):
        first=runtime.store.create('INS literature')
        plan={'interpreted_question':'INS literature','steps':[], 'literature':True,'literature_intent':{'reason':'explicit_request'},'plan_mode':'literature_only'}
        runtime.store.update(first['run_id'],plan=plan,status='queued')
        async def graph(run_id, run):
            runtime.store.update(run_id,graph_answer='INS graph',evidence={'nodes':[], 'steps':[]})
            return {},True
        runtime.graph_answer=graph
        runtime.launch(first['run_id'],runtime.execution(first['run_id']))
        for _ in range(100):
            if runtime.store.get(first['run_id'])['followup_ready']: break
            await asyncio.sleep(.005)
        assert runtime.store.get(first['run_id'])['status']=='running'
        assert runtime.active==0 and first['run_id'] in runtime.literature_runs
        await asyncio.wait_for(runtime.semaphore.acquire(),.1);runtime.semaphore.release()
        response=await client.post('/v2/plans',json={'question':'Which cells express it?', 'session_id':first['session_id'],'parent_run_id':first['run_id']})
        assert response.status_code==202
        child=runtime.store.get(response.json()['run_id'])
        assert 'INS graph' in str(runtime.planning_history(child))
        release.set()
        await wait_state(client,first['run_id'],{'completed','partial'})
        saved=runtime.store.get(first['run_id'])['literature']
        assert len(saved['references'])==1 and saved['references'][0]['sources']==['hirn','glkb']


def test_reference_merge_preserves_rich_metadata():
    registry=[]
    append_references(registry,[{'pmid':'40875294','title':'Full publication title','authors':['Author']}],'glkb')
    append_references(registry,[{'pmid':'40875294','title':'PMID 40875294','authors':[]}],'hirn')
    assert registry[0]['title']=='Full publication title'
    assert registry[0]['authors']==['Author']
    assert registry[0]['sources']==['glkb','hirn']
