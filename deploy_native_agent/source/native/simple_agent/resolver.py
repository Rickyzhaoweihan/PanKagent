"""Indexed retrieval only; the agent selects identities."""
import re,time
from .core import quoted

def lexical(text,mode):
    tokens=re.findall(r'\w+',text)
    if not tokens:return None
    if mode=='fuzzy':return ' AND '.join(t+'~1' for t in tokens)
    if mode=='broad':return ' OR '.join(tokens)
    return '"'+' '.join(tokens)+'"'

async def resolve(transport,args):
    begin=time.perf_counter();label=args.get('label');text=args['text'];mode=args.get('mode','identity');roles=args.get('search_roles',{})
    result={'candidates':[],'search_attempts':[],'search_failures':[],'index_scope':[],'limitations':[], 'retrieval_cap':200,'candidate_cap':12,'identity_decision':'Scores order suggestions. The agent chooses identities.'}
    def done():
        result['elapsed_s']=time.perf_counter()-begin
        result['search_status']=('incomplete' if result['search_failures'] else
                                 'unsearched' if not result['search_attempts'] else 'bounded')
        if not result['candidates'] and result['search_status'] != 'bounded':
            result['status']='tool_error' if result['search_failures'] else 'unsearched'
            result['feedback']='No absence conclusion is possible: retrieval failed or no usable search path was executed.'
        return result
    if mode not in ('identity','fuzzy','broad'):
        result['limitations'].append('Description-only retrieval is unavailable: no description index. Descriptions are candidate context.' if mode=='description' else 'Unknown mode.')
        return done()
    try:indexes=[i for i in await transport.initialize() if i['state']=='ONLINE']
    except Exception as e:
        result['search_failures'].append({'pass':'inventory','feedback':str(e)});return done()
    labels=[label] if label else list(roles)
    bindings=[(l,f) for l in labels for f in roles.get(l,{}).get('identifiers',[]) if any(i['type']=='RANGE' and i['labelsOrTypes']==[l] and i['properties']==[f] for i in indexes)]
    if label and not bindings:
        # A node may have several labels. Borrow only a schema-declared identity
        # access path and retain the requested label in the indexed query.
        requested_fields=set(roles.get(label,{}).get('identifiers',[]))
        bindings=sorted({(l,f) for l,spec in roles.items() for f in spec.get('identifiers',[])
                         if f in requested_fields and any(i['type']=='RANGE' and
                         i.get('labelsOrTypes')==[l] and i.get('properties')==[f] for i in indexes)})
    records=[];capped=False;retrieved=0
    if bindings:
        q=' UNION '.join('MATCH (n:'+quoted(l)+') USING INDEX n:'+quoted(l)+'('+quoted(f)+') WHERE n.'+quoted(f)+' = $text'+(' AND n:'+quoted(label) if label and label!=l else '')+' RETURN elementId(n) AS element_id, labels(n) AS labels, properties(n) AS properties' for l,f in bindings)
        t=time.perf_counter()
        try:
            raw=await transport._raw(q,{'text':text},200);records.extend(n for n in raw['rows'] if not label or label in n['labels']);capped|=raw.get('truncated',False)
            result['search_attempts'].append({'pass':'indexed_identifier','elapsed_s':time.perf_counter()-t,'execution_s':raw.get('elapsed_s'),'candidate_count':len(raw['rows'])})
        except Exception as e:result['search_failures'].append({'pass':'indexed_identifier','feedback':str(e)})
        result['index_scope'].append({'kind':'RANGE','bindings':bindings,'requested_label':label})
    if not records:
        expression=lexical(text,mode)
        for i in indexes:
            if retrieved>=200:
                result['limitations'].append('Total lexical retrieval cap exhausted; additional indexes were not searched.')
                break
            if i['type']!='FULLTEXT' or label and label not in i['labelsOrTypes']:continue
            covered=[l for l in labels if l in i['labelsOrTypes'] and set(i['properties']) & set(sum((roles.get(l,{}).get(k,[]) for k in ('identifiers','names','aliases')),[]))]
            if not covered or not expression:continue
            t=time.perf_counter()
            try:
                raw=await transport._fulltext(i['name'],expression,200-retrieved);retrieved+=len(raw['rows']);capped|=retrieved>=200
                records.extend(n for n in raw['rows'] if set(n['labels']) & set(covered))
                result['search_attempts'].append({'pass':'indexed_fulltext','index':i['name'],'elapsed_s':time.perf_counter()-t,'execution_s':raw.get('elapsed_s'),'retrieved_count':len(raw['rows'])})
            except Exception as e:result['search_failures'].append({'pass':'indexed_fulltext','feedback':str(e)})
            result['index_scope'].append({'kind':'FULLTEXT','name':i['name'],'labels':covered,'properties':i['properties']})
    missing=[l for l in labels if not any(i['type']=='FULLTEXT' and l in i['labelsOrTypes'] for i in indexes)]
    if missing:result['limitations'].append({'no_lexical_index_for_types':missing})
    candidates={}
    for n in records:
        matched=[]
        for l in n['labels']:
            if label and l!=label:continue
            for role in ('identifiers','names','aliases'):
                for f in roles.get(l,{}).get(role,[]):
                    v=n['properties'].get(f);vs=v if isinstance(v,list) else [v]
                    if any(isinstance(x,str) and x.casefold()==text.casefold() for x in vs):matched.append({'field':f,'role':role})
        n={**n,'matched_fields':matched,'match_class':'exact' if matched else 'lexical_suggestion'}
        key=n.get('element_id') or (tuple(n['labels']),n['properties'].get('id'))
        if key not in candidates or n.get('score',0)>candidates[key].get('score',0):candidates[key]=n
    priority={'identifiers':0,'names':1,'aliases':2}
    ordered=sorted(candidates.values(),key=lambda n:(min((priority[m['role']] for m in n['matched_fields']),default=3),-n.get('score',0)))
    all_types=sorted({l for n in ordered for l in n['labels']})
    visible_types={l for n in ordered[:12] for l in n['labels']}
    result['omitted_candidate_types']=[l for l in all_types if l not in visible_types]
    result['candidate_limit_note']='The 12-candidate limit may hide identities. Refine the requested type/text to inspect omitted candidates.'
    result.update(candidates=ordered[:12],truncated=capped or len(ordered)>12,retrieval_cap_reached=capped,candidate_count_before_limit=len(ordered),match_mode=mode,
        coverage='Existing declared indexed fields only. Retrieval cap applies before requested-type filtering and may hide matches.' if capped else 'Existing declared indexed fields only; absence does not establish absence across unindexed fields/types.')
    return done()
