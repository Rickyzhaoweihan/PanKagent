"""Bounded read-only adapter for the documented functional-data API."""
import math
import time
from urllib.parse import urlencode

BASE = 'https://functional.pankgraph.org'
VERSION = 'functional-adapter-5-aggregate'
FILTERS = {'disease','sex','center','race','age_min','age_max','bmi_min','bmi_max'}
TRACES = {'ins_ieq','ins_content','gcg_ieq','gcg_content'}
PATHS = {'health','api/data/summary','api/data/donors','api/charts/cohort-traces','api/charts/cohort-traces.png',
         'api/charts/trait-summary','api/charts/trait-summary.png','api/charts/association','api/charts/association.png'}

def parameters(values, trace=False):
    allowed=FILTERS | ({'trace_type'} if trace else {'trace_type','trait','x_key','x_trait','y_trait','limit'})
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
    params=dict(params)
    result_page=params.pop('result_page', None)
    if result_page is not None and (path != 'api/charts/cohort-traces.png' or result_page != 'Yes'):
        raise ValueError('invalid_functional_plot_format')
    params=parameters(params)
    if result_page is not None:params['result_page']=result_page
    async with http.stream('GET',BASE+'/'+path,params=params,timeout=15) as response:
        response.raise_for_status()
        content=bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content)>8*1024*1024:raise ValueError('functional_response_too_large')
        return bytes(content),response.headers.get('content-type','application/json')

def contributor_counts(data):
    """Count unique finite contributors, never selected inventory as a mean N.

    Older sources can omit individual series values. That means unknown, not
    zero contributors. Duplicate series for a donor count once per timepoint.
    """
    times = data.get('times', [])
    series = data.get('series', [])
    selected = {row.get('donor_id') for row in series if row.get('donor_id')}
    verified = (bool(series) or not any(v is not None for v in data.get('mean', []))) and all(isinstance(row.get('values'), list)
                   and len(row['values']) == len(times) and row.get('donor_id')
                   and all(v is None or (isinstance(v, (int, float))
                           and not isinstance(v, bool) and math.isfinite(v))
                           for v in row['values']) for row in series)
    contributors = [set() for _ in times]
    if verified:
        for row in series:
            for index, value in enumerate(row['values']):
                if value is not None:
                    contributors[index].add(row['donor_id'])
    counts = [len(ids) for ids in contributors] if verified else [None] * len(times)
    return {'selected_donors': len(selected),
            'contributing_donors': len(set().union(*contributors)) if verified else None,
            'contributing_donors_by_timepoint': counts,
            'contributor_counts_verified': bool(verified),
            'counting_unit': 'unique donors with finite measurements',
            'trace_points': [{'time_minutes': t, 'mean_response': m,
                              'contributing_donors': n}
                             for t, m, n in zip(times, data.get('mean', []), counts)]}


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
              'stimuli':data.get('stimuli',[]),'unique_donors':len({r.get('donor_id') for r in data.get('series',[]) if r.get('donor_id')}),
              **contributor_counts(data)},
          'provenance':[{'source':source,'retrieved_at':time.time(),'adapter_version':VERSION}],
          'validation':[{'valid':True,'checks':['typed_filters','bounded_trace_shape']}], 'queries':[]}
    result = {'nodes':[],'edges':[],'rows':rows,'steps':[step],'graph_version':graph_version,'completeness':'complete',
            'functional_filters':params,'scope_note':'Functional measurements from the selected cohort; no knowledge-graph or literature search was performed.'}
    from pankagent_vnext.output_scope import aggregate_only, project
    return project(result) if aggregate_only(question) else result


def synthesis_body(question, steps):
    import json
    selected=[]
    for index,step in enumerate(steps.values(),1):
        selected.append({**step, "evidence_id":"G"+str(index), "source_kind":"functional_measurements"})
    body=json.dumps({"question":question,"evidence":selected},ensure_ascii=False)
    if len(body.encode())>100000:raise ValueError("functional_synthesis_context_too_large")
    return body


def aggregate_plot(evidence):
    """Render only verified means; no upstream donor lines or labels enter assets."""
    from io import BytesIO
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    metadata = next(s['functional_metadata'] for s in evidence['steps'] if s.get('functional_metadata'))
    points = metadata['trace_points']
    fig = Figure(figsize=(10, 5), layout='constrained')
    FigureCanvasAgg(fig)
    axis = fig.subplots()
    axis.plot([p['time_minutes'] for p in points],
              [p['mean_response'] if p['mean_response'] is not None else float('nan') for p in points],
              color='#bd3838', label='Cohort mean', linewidth=2)
    for i, interval in enumerate(metadata.get('stimuli', [])):
        start,end,label = interval
        axis.axvspan(start,end,color=('#eef4f8' if i % 2 == 0 else '#d7e9ed'),alpha=.6,zorder=0)
        axis.text((start+end)/2,1.01,label,transform=axis.get_xaxis_transform(),ha='center',va='bottom',fontsize=7,rotation=35)
    axis.set(xlabel='Time (minutes)',ylabel=metadata.get('y_label') or 'Recorded response')
    axis.set_title(f"Selected donors: {metadata.get('unique_donors')}; contributing donors: {metadata.get('contributing_donors')}",pad=55)
    axis.legend(loc='best');axis.grid(alpha=.15)
    output=BytesIO();fig.savefig(output,format='png',dpi=130)
    return output.getvalue()
