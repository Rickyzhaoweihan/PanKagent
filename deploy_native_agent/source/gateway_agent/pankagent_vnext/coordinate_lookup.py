"""Bounded gene-coordinate -> interval lookup, with schema-owned mappings.

No LLM SQL, inferred assembly, or inferred coordinate offset. The source pairing
must be verified before enabling it in schema 1 and configuring a private DSN.
"""
import asyncio
import json
import re
from copy import deepcopy
from .agent_schemas import module


def draft(question, grounding):
    database = module('database_schema')
    if (not grounding or grounding.get('status') != 'ready'
            or grounding.get('identity', {}).get('graph_release') != database['release']):
        return None
    recipe = module('query_patterns')['coordinate_lookup']
    mentions = [m for m in grounding.get('mentions', []) if m.get('state') == 'resolved' and m.get('identity_complete') is not False
                and len(m.get('candidates', [])) == 1
                and m['candidates'][0].get('entity_type') == recipe['anchor_type']]
    if len(mentions) != 1:
        return None
    mention = mentions[0]
    syntax = module('semantic_interpretation')['retrieval_interpretation']['coordinate_lookup']
    match = re.fullmatch(syntax['request_pattern'].format(entity=re.escape(mention['requested'])),question,re.I)
    if not match:
        return None
    if match['around']:
        return {'interpreted_question':question,'steps':[], 'clarification':syntax['clarification']}
    return {'interpreted_question':question,'clarification':None,'steps':[{
        'id':'coordinates','question':question,'relation_types':[], 'depends_on':[],
        'constraints':[{'entity_type':recipe['anchor_type'],'property':'id','operator':'=',
                        'value':mention['candidates'][0]['id']}], 'complete':True,
        'coordinate_lookup':{'selection':'overlap','display_count':int(match['count']) if match['count'] else None},
        'evidence_combination':'independent'}]}


def _identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',value):
        raise ValueError('invalid_coordinate_schema_identifier')
    return '"'+value+'"'


def verified_spec(release):
    spec = deepcopy(module('database_schema')['postgresql'])
    check = spec.get('verification',{})
    recipe = module('query_patterns')['coordinate_lookup']
    if (not spec.get('enabled') or not check.get('verified')
            or check.get('graph_release') != release or not check.get('assembly')
            or any(check.get('coordinate_conventions',{}).get(recipe[key]) not in
                   {'0-based_half-open','1-based_inclusive'} for key in ('anchor_mapping','result_mapping'))):
        raise ValueError('postgresql_coordinate_mapping_unverified')
    return spec


def lookup(connection, spec, recipe, gene_id, max_records, timeout_ms):
    """Return PostgreSQL records in one read-only repeatable-read snapshot."""
    def table(mapping):
        return _identifier(recipe['sql_schema'])+'.'+_identifier(mapping)
    anchor, target = recipe['anchor_mapping'],recipe['result_mapping']
    a,b=spec['mappings'][anchor],spec['mappings'][target]
    def columns(mapping):
        return ', '.join(_identifier(mapping[key]) if key=='id' else _identifier(mapping['properties'][key])
                         for key in ('id','chr','start','end'))
    def bounds(row,mapping):
        start,end=row[2],row[3]
        if isinstance(start,bool) or isinstance(end,bool) or not isinstance(start,int) or not isinstance(end,int):
            raise ValueError('invalid_coordinate_types')
        if spec['verification']['coordinate_conventions'][mapping]=='1-based_inclusive':start-=1
        if start<0 or end<=start:raise ValueError('invalid_coordinate_bounds')
        return start,end
    try:
        connection.set_session(readonly=True, isolation_level='REPEATABLE READ', autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, true)",(str(timeout_ms),))
            cursor.execute('SELECT '+columns(a)+' FROM '+table(anchor)+' WHERE '+_identifier(a['id'])+' = %s',(gene_id,))
            rows=cursor.fetchmany(2)
            if len(rows)!=1:raise ValueError('coordinate_anchor_not_unique')
            gene=rows[0];start,end=bounds(gene,anchor)
            # Normalize both tables to half-open intervals for overlap. Never
            # use a guessed +/-1 offset derived from a single example record.
            offset=1 if spec['verification']['coordinate_conventions'][target]=='1-based_inclusive' else 0
            cursor.execute('SELECT '+columns(b)+' FROM '+table(target)+' WHERE '
                +_identifier(b['properties']['chr'])+' = %s AND ('+_identifier(b['properties']['start'])
                +' - %s) < %s AND '+_identifier(b['properties']['end'])+' > %s', (gene[1],offset,end,start))
            peaks=cursor.fetchmany(max_records+1)
            if len(peaks)>max_records:raise ValueError('coordinate_materialization_limit')
            if any(bounds(row,target)[1]<=start or bounds(row,target)[0]>=end or row[1]!=gene[1] for row in peaks):
                raise ValueError('coordinate_overlap_verification_failed')
            return {'gene':gene,'peaks':peaks,'assembly':spec['verification']['assembly'],
                    'coordinate_conventions':spec['verification']['coordinate_conventions']}
    finally:
        connection.rollback()
        connection.close()


async def execute(graph,step,base):
    try:
        if not graph._resolution_verified(step):
            raise ValueError('coordinate_anchor_unverified')
        spec=verified_spec(graph.settings.graph_version)
        dsn=getattr(graph.settings,'postgresql_dsn','')
        if not dsn:raise ValueError('postgresql_not_configured')
        recipe=module('query_patterns')['coordinate_lookup']
        identities=[r for r in step.get('resolved_entities',[]) if r.get('state')=='resolved'
                    and r.get('entity_type')==recipe['anchor_type']]
        constraints=step.get('constraints',[])
        if (len(identities)!=1 or len(constraints)!=1
                or constraints[0].get('entity_type')!=recipe['anchor_type']
                or constraints[0].get('property')!='id' or constraints[0].get('operator','=')!='='
                or step.get('coordinate_lookup',{}).get('selection')!='overlap'):
            raise ValueError('coordinate_anchor_unverified')
        budget=step.get('retrieval_budget') or {}
        max_nodes=min(graph.settings.max_nodes, budget.get('max_nodes',graph.settings.max_nodes))
        max_records=min(max_nodes-1, budget.get('max_rows',max_nodes-1))
        max_bytes=min(graph.settings.max_bytes,budget.get('max_bytes',graph.settings.max_bytes))
        if max_records < 1:
            raise ValueError('coordinate_materialization_limit')
        def run():
            import psycopg2
            connection=psycopg2.connect(dsn,connect_timeout=max(1,int(graph.settings.graph_timeout)))
            return lookup(connection,spec,recipe,identities[0]['id'],max_records,
                          int(graph.settings.graph_timeout*1000))
        records=await asyncio.to_thread(run)
        if len(json.dumps(records).encode())>max_bytes:
            raise ValueError('coordinate_materialization_limit')
        ids=[(recipe['anchor_type'],records['gene'][0])]+[(recipe['result_type'],p[0]) for p in records['peaks']]
        evidence=await graph.hydrate_viewer_ids(graph.settings.graph_version,ids)
        found={(label,n['id']) for n in evidence.get('nodes',[]) for label in n.get('labels',[])}
        if evidence.get('truncated') or not set(ids)<=found:raise ValueError('coordinate_graph_membership_unverified')
        return {**base,**evidence,'rows':[{'gene_id':records['gene'][0],'peak_id':p[0],
            'chromosome':p[1],'start':p[2],'end':p[3],'source':'postgresql_overlap'} for p in records['peaks']],
            'status':'complete' if records['peaks'] else 'empty','provenance':[{
                'provider':'postgresql','operation':'gene_coordinates_then_peak_overlap',
                'graph_release':graph.settings.graph_version,'assembly':records['assembly'],
                'coordinate_conventions':records['coordinate_conventions'],
                'gene_record':list(records['gene']),'membership_verified':True}],
            'retrieval_execution':{'completed':True,'cursor_exhausted':True,'mode':'read_only'}}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Connection exceptions may contain secrets. Emit bounded diagnostics only.
        reason=str(exc) if isinstance(exc,ValueError) and re.fullmatch(r'[a-z_]+',str(exc)) else 'coordinate_provider_unavailable'
        return {**base,'error':{'category':reason,'message':'The coordinate lookup could not be verified.'},
                'validation':[{'valid':False,'reasons':[reason]}]}
