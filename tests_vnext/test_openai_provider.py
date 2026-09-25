import asyncio
import json
import pytest
import httpx
from pankagent_vnext.openai_provider import OpenAIClient, normalize_response, request_body, ProviderProtocolError, ProviderStatusError
from pankagent_vnext.budget import Budget, BudgetExceeded


def response(**changes):
    return dict(id='resp_test', model='gpt-6-sol', status='completed',
                output=[{'type':'function_call','call_id':'call_1','name':'record_plan','arguments':'{"steps":[]}'}],
                usage={'input_tokens':1000,'output_tokens':100,'input_tokens_details':{'cached_tokens':200},'output_tokens_details':{'reasoning_tokens':30}}, **changes)


def args():
    return dict(model='gpt-6-sol',max_tokens=100,system=[{'type':'text','text':'contract','cache_control':{'type':'ephemeral'}}],
                tools=[{'name':'record_plan','input_schema':{'type':'object','properties':{'optional':{'type':'string'}}}}],
                tool_choice={'type':'any','disable_parallel_tool_use':True},messages=[
                    {'role':'user','content':'raw question'},
                    {'role':'assistant','content':[{'type':'tool_use','id':'call_1','name':'resolve_entities','input':{'name':'INS'}}]},
                    {'role':'user','content':[{'type':'tool_result','tool_use_id':'call_1','content':'verified IDs'}]}])


def test_tool_roundtrip_and_optional_schema():
    b=request_body(args(),'none')
    assert b['input'][1]['call_id']==b['input'][2]['call_id']=='call_1'
    assert b['input'][1]['arguments']=='{"name": "INS"}'
    assert b['tools'][0]['strict'] is False
    assert b['tool_choice']=='required' and b['parallel_tool_calls'] is False
    assert b['store'] is False and b['service_tier']=='default'
    assert 'cache_control' not in json.dumps(b) and 'thinking' not in b
    assert b['input'][0]['content']=='raw question'
    a=args();a['tool_choice']={'type':'tool','name':'record_plan'}
    assert request_body(a,'none')['tool_choice']=={'type':'function','name':'record_plan'}


def test_normalize_usage_and_arguments(tmp_path):
    r=normalize_response(response())
    assert r.content[0].input=={'steps':[]} and r.stop_reason=='tool_use'
    assert r.usage.input_tokens==800 and r.usage.reasoning_tokens==30
    b=Budget(tmp_path/'budget.sqlite3',20)
    rid=b.reserve('gpt-6-sol','plan',5000,500)
    b.settle(rid,r.usage.model_dump())
    assert b.snapshot()['spent_usd']==.00264  # reasoning already included in output
    d=response();d['output'][0]['arguments']='{broken'
    assert normalize_response(d).content[0].input=={'_invalid_arguments':True}


def test_long_context_and_conservative_reservations(tmp_path):
    b=Budget(tmp_path/'budget.sqlite3',2)
    with pytest.raises(BudgetExceeded): b.reserve('gpt-6-sol','plan',500000,1000)
    rid=b.reserve('gpt-6-sol','plan',280000,1000)
    b.settle(rid,{'input_tokens':280000,'total_input_tokens':280000,'output_tokens':100})
    assert b.snapshot()['spent_usd']==1.1215


def test_stream_and_incomplete_usage():
    async def run():
        d=response();d['output']=[]
        events=[{'type':'response.output_text.delta','delta':'Hello '},{'type':'response.output_text.delta','delta':'[G1]'}, {'type':'response.completed','response':d}]
        async def handler(req):return httpx.Response(200,text=''.join('data: '+json.dumps(e)+'\n\n' for e in events))
        c=OpenAIClient('test',10,transport=httpx.MockTransport(handler))
        async with c.stream(**args()) as stream:
            chunks=[t async for t in stream.text_stream]; final=await stream.get_final_message()
        assert ''.join(chunks)=='Hello [G1]' and final.usage.output_tokens==100
        await c.close()
        async def missing(req):return httpx.Response(200,text='data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
        c=OpenAIClient('test',10,transport=httpx.MockTransport(missing))
        with pytest.raises(ProviderProtocolError):
            async with c.stream(**args()) as s: [t async for t in s.text_stream]
        await c.close()
    asyncio.run(run())


def test_definitive_rejection_and_missing_usage():
    async def run():
        async def reject(req):return httpx.Response(401,json={'error':{'message':'secret should not be logged'}})
        c=OpenAIClient('test',10,transport=httpx.MockTransport(reject))
        with pytest.raises(ProviderStatusError) as e:await c.create(**args())
        assert e.value.status_code==401 and 'secret' not in str(e.value)
        await c.close()
    asyncio.run(run())
    d=response();d.pop('usage')
    with pytest.raises(ProviderProtocolError):normalize_response(d)
    d=response();d.update(status='incomplete',incomplete_details={'reason':'max_output_tokens'})
    assert normalize_response(d).stop_reason=='max_tokens'


def test_gateway_rejection_settles_but_ambiguous_disconnect_keeps_reserve(tmp_path):
    from pankagent_vnext.config import Settings
    from pankagent_vnext.llm import ClaudeGateway
    async def run():
        gateway=ClaudeGateway(Settings(model='gpt-6-sol',openai_key='test',state_dir=tmp_path,budget_usd=20))
        await gateway.client.close()
        async def reject(req):return httpx.Response(429,json={'error':{'message':'rate limited'}})
        gateway.client=OpenAIClient('test',10,transport=httpx.MockTransport(reject))
        rid=await gateway._reserve('plan','system','question',100)
        with pytest.raises(ProviderStatusError):await gateway._create(rid,**args())
        assert gateway.budget.snapshot()['spent_usd']==0 and gateway.budget.snapshot()['pending_calls']==0
        await gateway.client.close()
        async def lost(req):raise httpx.ReadTimeout('ambiguous outcome')
        gateway.client=OpenAIClient('test',10,transport=httpx.MockTransport(lost))
        rid=await gateway._reserve('plan','system','question',100)
        with pytest.raises(httpx.ReadTimeout):await gateway._create(rid,**args())
        assert gateway.budget.snapshot()['pending_calls']==1
        await gateway.close()
    asyncio.run(run())


def test_openai_key_is_selected_without_anthropic_key(tmp_path):
    from pankagent_vnext.config import Settings
    from pankagent_vnext.llm import ClaudeGateway
    async def run():
        g=ClaudeGateway(Settings(model='gpt-6-sol',openai_key='test-openai',anthropic_key='',state_dir=tmp_path))
        assert g.api_key=='test-openai' and g._options()=={}
        assert g.settings.provider_status_url==''
        await g.close()
    asyncio.run(run())
