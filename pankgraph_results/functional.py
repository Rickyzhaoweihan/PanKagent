"""Bounded read-only adapter for the documented functional-data API."""
import math
import time
from urllib.parse import urlencode

BASE = 'https://functional.pankgraph.org'
VERSION = 'functional-adapter-2'
FILTERS = {'disease','sex','center','race','age_min','age_max','bmi_min','bmi_max'}
TRACES = {'ins_ieq','ins_content','gcg_ieq','gcg_content'}
PATHS = {'health','api/data/summary','api/data/donors','api/charts/cohort-traces','api/charts/cohort-traces.png',
         'api/charts/trait-summary','api/charts/trait-summary.png','api/charts/association','api/charts/association.png'}

def parameters(values, trace=False):
    allowed=FILTERS | ({'trace_type'} if trace else {'trace_type','trait','x_trait','y_trait','limit'})
    if set(values)-allowed: raise ValueError('unknown_functional_filter')
    out={k:str(v) for k,v in values.items() if v is not None and str(v)!=''}
    for k,v in out.items():
        if len(v)>200 or any(ord(c)<32 for c in v): raise ValueError('invalid_functional_filter')
        if k.endswith(('_min','_max')):
            n=float(v)
            if not math.isfinite(n) or n<0: raise ValueError('invalid_functional_range')
    for stem in ('age','bmi'):
        if stem+'_min' in out and stem+'_max' in out and float(out[stem+'_min'])>float(out[stem+'_max']):raise ValueError('reversed_functional_range')
    if out.get('trace_type','ins_ieq') not in TRACES:raise ValueError('unknown_trace_type')
    if trace:out.setdefault('trace_type','ins_ieq')
    return out

async def fetch(http,path,params):
    if path not in PATHS:raise ValueError('unknown_functional_endpoint')
    params=parameters(params)
    async with http.stream('GET',BASE+'/'+path,params=params,timeout=15) as response:
        response.raise_for_status()
        content=bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content)>8*1024*1024:raise ValueError('functional_response_too_large')
        return bytes(content),response.headers.get('content-type','application/json')

async def evidence(http,params,question,graph_version):
    import json
    params=parameters(params,trace=True)
    data,_=await fetch(http,'api/charts/cohort-traces',params)
    data=json.loads(data)
    times,mean=data.get('times',[]),data.get('mean',[])
    if len(times)!=len(mean) or len(times)>2000:raise ValueError('invalid_trace_shape')
    rows=[{'time_minutes':t,'mean_response':v,'unit':data.get('y_label')} for t,v in zip(times,mean) if v is not None]
    for row in rows:
        if not all(isinstance(row[k],(int,float)) and math.isfinite(row[k]) for k in ('time_minutes','mean_response')):raise ValueError('invalid_trace_value')
    source=BASE+'/api/charts/cohort-traces?'+urlencode(params)
    step={'step_id':'functional','status':'complete' if rows else 'empty','question':question,
          'nodes':[],'edges':[],'rows':rows,'graph_version':graph_version,'truncated':False,
          'functional_metadata':{'filters':params,'trace_type':data.get('trace_type'), 'y_label':data.get('y_label'),
              'stimuli':data.get('stimuli',[]),'unique_donors':len({r.get('donor_id') for r in data.get('series',[]) if r.get('donor_id')})},
          'provenance':[{'source':source,'retrieved_at':time.time(),'adapter_version':VERSION}],
          'validation':[{'valid':True,'checks':['typed_filters','bounded_trace_shape']}], 'queries':[]}
    return {'nodes':[],'edges':[],'rows':rows,'steps':[step],'graph_version':graph_version,'completeness':'complete',
            'functional_filters':params,'scope_note':'Functional measurements from the selected cohort; no knowledge-graph or literature search was performed.'}


def synthesis_body(question, steps):
    import json
    selected=[]
    for index,step in enumerate(steps.values(),1):
        selected.append({**step, "evidence_id":"G"+str(index), "source_kind":"functional_measurements"})
    body=json.dumps({"question":question,"evidence":selected},ensure_ascii=False)
    if len(body.encode())>100000:raise ValueError("functional_synthesis_context_too_large")
    return body
