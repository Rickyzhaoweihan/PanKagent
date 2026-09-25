"""Read-only Neo4j profiling, no model/APOC. Output is a release candidate, never hot-loaded."""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import pickle
import difflib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pankagent_vnext.agent_schemas.profiling import profile_grouped


def quote(name):
    return '`'+name.replace('`','``')+'`'


async def extract(driver, database, definition, selected=None, checkpoint=None, prototypes_only=False):
    result=deepcopy(definition);queries=[];unknown=deepcopy(definition.get('observations',{}).get('unknown_definitions',[])) if prototypes_only else [];failures=[]
    async with driver.session(database=database, default_access_mode='READ') as session:
        async def read(q,params=None):
            queries.append(hashlib.sha256(q.encode()).hexdigest())
            return await (await session.run(q,params or {})).data()
        async def grouped_profile(q, params, count, pattern=None):
            queries.append(hashlib.sha256(q.encode()).hexdigest())
            rows=await session.run(q,params)
            with tempfile.TemporaryFile() as spool:
                async for row in rows:pickle.dump((row['value'],row['frequency']),spool)
                spool.seek(0)
                def records():
                    while True:
                        try:yield pickle.load(spool)
                        except EOFError:return
                return profile_grouped(records(),total_records=count,pattern=pattern)
        labels=await read('CALL db.labels() YIELD label RETURN label')
        relations=await read('CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType')
        result['observations']={'status':'complete','discovered_labels':sorted(x['label'] for x in labels),
            'discovered_relationships':sorted(x['relationshipType'] for x in relations)}
        for group, discovered in [('nodes',result['observations']['discovered_labels']),('relationships',result['observations']['discovered_relationships'])]:
            for owner in discovered:
                if selected and owner not in selected:continue
                if owner not in result[group]:unknown.append({'kind':group,'name':owner});continue
                spec=result[group][owner];match=('MATCH (n:'+quote(owner)+')' if group=='nodes' else 'MATCH (a)-[n:'+quote(owner)+']->(b)')
                count=(await read(match+' RETURN count(n) AS n'))[0]['n']
                props=[] if prototypes_only else await read(match+' UNWIND keys(n) AS k RETURN DISTINCT k ORDER BY k')
                for row in props:
                    key=row['k']
                    if key not in spec['properties']:unknown.append({'kind':group,'name':owner,'property':key});continue
                    prop=spec['properties'][key]
                    if prop['export']=='protected':prop['observations']={'status':'protected','exhaustive':False};continue
                    try:
                        q=match+' RETURN n[$key] AS value,count(*) AS frequency'
                        prop['observations']=await grouped_profile(q,{'key':key},count,prop.get('pattern'))
                        q=match+' WHERE n[$key] IS NOT NULL AND valueType(n[$key]) STARTS WITH "LIST" RETURN min(size(n[$key])) AS lo,max(size(n[$key])) AS hi'
                        lengths=await read(q,{'key':key})
                        if lengths[0]['lo'] is not None:
                            q=match+' WHERE valueType(n[$key]) STARTS WITH "LIST" UNWIND n[$key] AS value WITH DISTINCT n,value RETURN value,count(*) AS frequency'
                            prop['observations']['elements']=await grouped_profile(q,{'key':key},count);prop['observations']['elements'].pop('missing_records',None)
                            prop['observations']['list_lengths']={'min':lengths[0]['lo'],'max':lengths[0]['hi']}
                    except Exception:
                        failures.append(group+'.'+owner+'.properties.'+key)
                        prop['observations']={'status':'unavailable','exhaustive':False,'reason':'profile_query_failed'}
                if not prototypes_only:
                    stored={row['k'] for row in props}
                    for key,prop in spec['properties'].items():
                        if key not in stored:
                            prop['observations']=({'status':'protected','exhaustive':False} if prop['export']=='protected'
                                else profile_grouped([],total_records=count))
                for key,prop in spec['properties'].items():
                    if prop['export']=='protected':prop['observations']={'status':'protected','exhaustive':False}
                    elif prop.get('observations',{}).get('status')!='complete':failures.append(group+'.'+owner+'.properties.'+key)
                protected=group=='nodes' and spec['record_export']=='protected'
                if group=='relationships':
                    signatures=await read(match+' RETURN labels(a) AS source,labels(b) AS target,count(*) AS records')
                    spec['observations']={'status':'complete','count':count,'endpoint_signatures':signatures,'prototype_status':'protected' if any(any(result['nodes'].get(label,{}).get('record_export','protected')=='protected' for label in s['source']+s['target']) for s in signatures) else 'available'}
                    protected=spec['observations']['prototype_status']=='protected'
                else:spec['observations']={'status':'complete','count':count,'prototype_status':'protected' if protected else 'available'}
                if not protected:
                    keys=[k for k,v in spec['properties'].items() if v['export']=='public'];projection='{'+', '.join(quote(k)+': n.'+quote(k) for k in keys)+'}'
                    ret=projection+' AS properties' if group=='nodes' else 'a.id AS source_id,labels(a) AS source_types,b.id AS target_id,labels(b) AS target_types,elementId(n) AS record_id,'+projection+' AS properties'
                    order='n.id' if group=='nodes' else 'a.id,b.id,'+','.join('n.'+quote(k) for k in keys)
                    try:
                        spec['observations']['prototypes']=await read(match+' RETURN '+ret+' ORDER BY '+order+' LIMIT $limit',{'limit':4 if count<5 else 3})
                        spec['observations']['prototypes_exhaustive']=count<5
                        if group=='relationships':
                            spec['observations']['prototype_signatures']=[]
                            for signature in sorted(signatures,key=lambda s:(sorted(s['source']),sorted(s['target']))):
                                restricted=match+' WHERE size(labels(a))=size($source) AND all(k IN $source WHERE k IN labels(a)) AND size(labels(b))=size($target) AND all(k IN $target WHERE k IN labels(b))'
                                prototypes=await read(restricted+' RETURN '+ret+' ORDER BY '+order+',elementId(n) LIMIT $limit',
                                    {'source':signature['source'],'target':signature['target'],'limit':4 if signature['records']<5 else 3})
                                spec['observations']['prototype_signatures'].append({**signature,'prototypes':prototypes,'exhaustive':signature['records']<5})
                            spec['observations']['record_id_scope']='release-local Neo4j elementId; not a portable identity'
                    except Exception:
                        spec['observations']['prototype_status']='unavailable'
                        failures.append(group+'.'+owner+'.prototypes')
                if checkpoint:
                    snapshot=deepcopy(result);snapshot['observations']['status']='in_progress'
                    Path(checkpoint).write_text(json.dumps(snapshot,indent=2,ensure_ascii=False,default=str)+'\n')
                print(json.dumps({'profiled_type':owner,'records':count,'profile_failures':len(failures)}),flush=True)
        result['observations']['unknown_definitions']=unknown
        result['observations']['profile_failures']=failures
        result['observations']['query_sha256']=sorted(set(queries))
        result['observations']['status']='needs_review' if unknown or selected or failures else 'complete'
    return result


async def main(args):
    from pankagent_vnext.config import Settings
    from pankagent_vnext.graph import GraphAdapter
    settings=Settings();graph=GraphAdapter(settings)
    source=Path(args.source);before=json.loads(source.read_text())
    try:
        data=await extract(graph.driver,settings.neo4j_database,before,args.types,args.output+'.checkpoint.json',getattr(args,'prototypes_only',False))
        # Review candidate files remain private until this entire-payload check
        # has also ruled out protected IDs in public relationship properties.
        from pankagent_vnext.agent_schemas.export_privacy import audit_export
        protected_ids=set()
        async with graph.driver.session(database=settings.neo4j_database, default_access_mode='READ') as session:
            for label,spec in data['nodes'].items():
                if spec['record_export']!='protected':continue
                field=spec['identity']['id_field']
                rows=await session.run('MATCH (n:'+quote(label)+') RETURN n[$field] AS id',{'field':field})
                async for row in rows:
                    if row['id'] is not None:protected_ids.add(str(row['id']))
        privacy=audit_export(data,protected_ids)
        Path(args.output+'.privacy.json').write_text(json.dumps(privacy,indent=2)+'\n')
        if not privacy['passed']:raise ValueError('schema_export_privacy_review_required')
    finally:await graph.close()
    from jsonschema import Draft202012Validator
    contract=json.loads((Path(__file__).resolve().parents[1]/'pankagent_vnext/agent_schemas/contracts/database_schema.schema.json').read_text())
    Draft202012Validator(contract).validate(data)
    Path(args.output+'.diff').write_text(''.join(difflib.unified_diff(
        source.read_text().splitlines(True), (json.dumps(data,indent=2,ensure_ascii=False,default=str)+'\n').splitlines(True),
        fromfile=source.name,tofile=Path(args.output).name)))
    Path(args.output).write_text(json.dumps(data,indent=2,ensure_ascii=False,default=str)+'\n')
    Path(args.output+'.audit.json').write_text(json.dumps({'extracted_at':datetime.now(timezone.utc).isoformat(),'graph_release':settings.graph_version,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'output_sha256':hashlib.sha256(Path(args.output).read_bytes()).hexdigest(),'status':data['observations']['status']},indent=2)+'\n')

if __name__=='__main__':
    import os
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('source');parser.add_argument('output');parser.add_argument('--types',nargs='+');parser.add_argument('--prototypes-only',action='store_true');asyncio.run(main(parser.parse_args()))
