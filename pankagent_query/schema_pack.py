"""Compile data facts from a frozen release and its published component schema."""
import hashlib
import json
from pathlib import Path


def compile_pack(root, published=None):
    root=Path(root);raw=(root/'inputs.json').read_bytes();manifest=json.loads(raw)
    data=json.loads((root/'data_schema.json').read_text())
    viewer=published or json.loads((root/'viewer.schema.json').read_text())
    digest=hashlib.sha256(raw).hexdigest()
    assert data['input_manifest_sha256']==digest
    if published:assert published['release_id']==data['release_id']
    nodes,relations,roles={}, {}, {}
    types={'node':{},'edge':{}};categories={};sources={'node':{},'edge':{}}
    def fields(item):
        out={}
        for f in item['files'].values():
            for header in f['header']:
                if header.startswith(':'):continue
                name,kind=header.rsplit(':',1)
                if name in out and out[name]!=kind:
                    assert {out[name],kind}<={'int','long'} or {out[name],kind}<={'float','double'},('Conflicting property types',name,out[name],kind)
                out[name]=kind
        return out
    def merge(target, key, mapping):
        previous=target.setdefault(key,{})
        for name,kind in mapping.items():
            if name in previous and previous[name]!=kind:
                # Co-occurring broad labels do not imply one property type.
                old=previous[name] if isinstance(previous[name],list) else [previous[name]]
                previous[name]=sorted(set(old+[kind]))
            else:previous[name]=kind
    for key,item in data['nodes'].items():
        fs=fields(item)
        for label in sorted({l for group in item['label_sets'] for l in group.split(';')}):
            nodes[label]=sorted(set(nodes.get(label,[]))|set(fs));merge(types['node'],label,fs)
        sources['node'][key]={k:item[k] for k in ['count','sources','source_urls','licenses','data_versions']}
    for key,item in data['edges'].items():
        typ=item['relationship_type'];profile=viewer['profiles']['edge:'+key];fs=fields(item)
        entry=relations.setdefault(typ,{'properties':[],'paths':[],'description':'','titles':[],'contexts':[]})
        if profile['title'] not in entry['titles']:entry['titles'].append(profile['title'])
        entry['description']='; '.join(entry['titles'])
        entry['properties']=sorted(set(entry['properties'])|set(fs));merge(types['edge'],typ,fs)
        path={'source':data['nodes'][item['start_type']]['required_labels'],'target':data['nodes'][item['end_type']]['required_labels']}
        if path not in entry['paths']:entry['paths'].append(path)
        entry['contexts'].append({**path,'description':profile.get('note',''),'count':item['count']})
        sources['edge'][key]={k:item[k] for k in ['count','sources','source_urls','licenses','data_versions']}
    for label,fs in nodes.items():
        roles[label]={'identifiers':[k for k in fs if k=='id' or k.startswith('alt_id_')],
                      'names':[k for k in ['name'] if k in fs],'aliases':[k for k in ['synonyms'] if k in fs]}
    components={}
    bound=json.loads((root/'viewer.schema.json').read_text()).get('table_bindings',{})
    for key,p in viewer['profiles'].items():
        tables=[t for t in p.get('tables',[]) if t in viewer['tables'] and (t in bound or viewer['tables'][t].get('component'))]
        if tables:components[key]={'kind':p['kind'],'display_profile_id':p['id'],
            'match':p.get('match',{}),'table_ids':tables}
        if p['kind']=='node':
            canonical=p.get('canonical_type',p['id']);item=data['nodes'].get(canonical)
            if item:
                for field,value in p.get('match',{}).get('properties',{}).items():
                    for label in item['required_labels']:
                        values=categories.setdefault(label+'.'+field,[])
                        if value not in values:values.append(value)
    return {'version':'release-schema-v2','release':data['release_id'],
        'identity':{'input_manifest_sha256':digest,'nodes':data['node_count'],'edges':data['edge_count']},
        'nodes':nodes,'relations':relations,'property_types':types,'node_variants':{k:{'required_labels':v['required_labels'],'label_sets':v['label_sets'],'property_types':fields(v)} for k,v in data['nodes'].items()},'search_roles':roles,
        'categories':categories,'aliases':{},'source_metadata':sources,
        'sql_components':components,'sql_tables':{t:{k:v for k,v in viewer['tables'][t].items() if k in ['title','columns','all_fields','filters','note','description','request_filters']} for t in sorted({t for c in components.values() for t in c['table_ids']})},'source_snapshots':{k:v['snapshot_id'] for k,v in manifest['sql'].items()},
        'linked_evidence':{'interface':'inspect_result(kind=evidence, query_id, ref, table_id, fields, filters, cursor)',
            'ownership':'Only server-issued typed refs present in that query graph; API decides exact snapshot and table ownership.',
            'profiles':components},
        'notes':['Node types here are actual stored labels. Display profiles are not labels and do not assert a type hierarchy.',
            'Multiple labels refer to one node. Property types/categories come from the pinned release, not label-name heuristics.',
            'A broad shared label can expose multiple property types. Use a precise stored label and node_variants; it is not inheritance.',
            'Array membership uses has; in tests a scalar property against a supplied list. Do not use string CONTAINS for an array.',
            'Inspect exact-object evidence to read registered tables. Do not infer ownership from matching IDs in rows or neighboring endpoints.',
            'Source versions and paired snapshot IDs are distinct. Preserve units, unknown licenses and missing/false/zero.',
            *manifest.get('source_limitations',[])]}


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--published',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    pack=compile_pack(a.root,json.loads(a.published.read_text()) if a.published else None)
    a.out.write_text(json.dumps(pack,ensure_ascii=False,indent=2)+'\n')
