"""Validate identity field usage against schema and observed resolver candidates."""
from .core import Feedback


def explicit_identity(value, question):
    import re
    return re.search(r"(?<!\w)"+re.escape(str(value))+r"(?!\w)", question) is not None


def validate_bindings(pack, pattern, contexts, question="", require_resolved_qualifiers=False, queries=None):
    candidates = [c for ctx in contexts if ctx.get('tool') == 'resolve_entity'
                  for c in ctx.get('output', {}).get('candidates', [])]
    for node in pattern['nodes']:
        fields = pack.get('identity_fields', {}).get(node['label'], {})
        primaries = [f for f,s in fields.items() if s.get('role') == 'primary']
        compatible = [c for c in candidates if node['label'] in c.get('labels', [])]
        for f in node.get('filters', []):
            if f['property'] not in primaries or f.get('op') not in ('eq', 'in'): continue
            for value in f['value'] if f['op'] == 'in' else [f['value']]:
                if any(c['properties'].get(f['property']) == value for c in compatible): continue
                matches = []
                for c in compatible:
                    for key, v in c['properties'].items():
                        if key == f['property']: continue
                        if value in (v if isinstance(v, list) else [v]):
                            matches.append({'field': key, 'value': value, 'node_identity': c.get('node_identity', {k:c['properties'].get(k) for k in primaries})})
                if matches:
                    raise Feedback('Resolved value belongs to another identity/name field, not the primary ID. Use exact returned node IDs or the original field: '+str(matches))

        # A symbol may resolve to several distinct stored nodes. Never silently
        # select one of those IDs unless the user explicitly supplied that ID.
        for ctx in contexts:
            if ctx.get('tool') != 'resolve_entity': continue
            text=ctx.get('input',{}).get('text','')
            roles=pack.get('search_roles',{}).get(node['label'],{})
            fields=set(roles.get('names',[])+roles.get('aliases',[])+roles.get('identifiers',[]))-set(primaries)
            def exact_value(value):
                return any(str(v).casefold()==text.casefold() for v in (value if isinstance(value,list) else [value]))
            exact=[c for c in ctx.get('output',{}).get('candidates',[]) if node['label'] in c.get('labels',[]) and any(exact_value(c['properties'].get(k,'')) for k in fields)]
            for primary in primaries:
                ids={c['properties'].get(primary) for c in exact if c['properties'].get(primary)}
                if len(ids)<2: continue
                for f in node.get('filters',[]):
                    if f['property']!=primary or f.get('op') not in ('eq','in'):continue
                    values=set(f['value'] if f['op']=='in' else [f['value']])
                    if values & ids and not ids <= values and not all(explicit_identity(v,question) for v in values):
                        raise Feedback('This resolved name/accession has multiple distinct node identities. Do not select an arbitrary primary ID: '+str(sorted(ids))+'. Use ONE node variable with an IN filter on '+primary+' containing ALL returned IDs (op=in, value='+str(sorted(ids))+'). Each matched row retains the distinct stored node identity; this does not merge nodes. Do not create one node variable per candidate: that would AND alternatives. Alternatively request explicit disambiguation.')

        qualifier_labels={m.get('target') for family in pack.get('scope_families',[]) for m in family['members'] if m.get('target')}
        if require_resolved_qualifiers and node['label'] in qualifier_labels:
            for f in node.get('filters',[]):
                if f['property'] not in primaries or f.get('op') not in ('eq','in'):continue
                for value in f['value'] if f['op']=='in' else [f['value']]:
                    if explicit_identity(value,question):continue
                    resolved=any(ctx.get('tool')=='resolve_entity' and str(ctx.get('input',{}).get('text','')).strip()!=str(value) and any(node['label'] in c.get('labels',[]) and c.get('properties',{}).get(f['property'])==value for c in ctx.get('output',{}).get('candidates',[])) for ctx in contexts)
                    if resolved:continue
                    # A name-filtered lookup is also grounded; a source inventory
                    # or lookup of an invented primary ID is not concept binding.
                    grounded=False
                    for q in (queries or {}).values():
                        lookup=q.get('pattern',{})
                        if not any(n['label']==node['label'] and any(x['property'] not in primaries for x in n.get('filters',[])) for n in lookup.get('nodes',[])):continue
                        if any(node['label'] in n.get('labels',[]) and n.get('properties',{}).get(f['property'])==value for n in q.get('graph',{}).get('nodes',[])):grounded=True
                    if not grounded:
                        raise Feedback('Qualifying endpoint ID '+str(value)+' was not resolved from a qualifying concept or supplied by the user. A valid ID from a source inventory is not evidence of relevance. Resolve the original qualifying concept in this endpoint category, or retain a literal qualifying-name filter; if unresolved, report partial. Do not substitute an unrelated source record to satisfy scope coverage.')


def reject_broadened_request(pattern, queries, pack=None):
    """Scope relaxation is diagnostic, not replacement evidence for a conjunction."""
    import json
    def scope(p):
        labels={n['key']:n['label'] for n in p['nodes']}
        if any(e['source'] not in labels or e['target'] not in labels for e in p['edges']):
            raise Feedback('Edge endpoints must refer to declared node keys')
        def role(key):
            return (labels[key], tuple(sorted((e['type'], 'either' if e.get('direction')=='either' else 'source' if e['source']==key else 'target', labels[e['target'] if e['source']==key else e['source']]) for e in p['edges'] if key in (e['source'],e['target']))))
        shape=tuple(sorted(role(n['key']) for n in p['nodes']))
        filters={(str(role(n['key'])),json.dumps(f,sort_keys=True)) for n in p['nodes'] for f in n.get('filters',[])}
        filters.update((str((role(e['source']),e['type'],role(e['target']))),json.dumps(f,sort_keys=True)) for e in p['edges'] for f in e.get('filters',[]))
        return shape,filters,p.get('distinct_nodes',False)
    shape,filters,distinct=scope(pattern)
    for q in queries.values():
        if q.get('purpose')!='requested':continue
        oldshape,oldfilters,olddistinct=scope(q['pattern'])
        if oldshape==shape and olddistinct==distinct and filters < oldfilters:
            raise Feedback('This removes constraints from an executed requested query. Use purpose=diagnostic for a relaxed probe; it cannot replace the qualifying evidence. Revise the question explicitly if its scope changed.')

    # Switching schema-declared endpoint alternatives must retain a qualifying
    # endpoint constraint. A source-wide probe is diagnostic even when empty.
    for family in (pack or {}).get('scope_families',[]):
        targets={m.get('target') for m in family['members'] if m.get('target')}
        relations={m['relation'] for m in family['members']}
        def endpoints(p):
            nodes={n['key']:n for n in p['nodes']}
            return [nodes[e['target']] for e in p['edges'] if e['type'] in relations and nodes[e['target']]['label'] in targets]
        if any(not n.get('filters') for n in endpoints(pattern)) and any(n.get('filters') for q in queries.values() if q.get('purpose')=='requested' for n in endpoints(q['pattern'])):
            raise Feedback('A qualifying endpoint constraint was omitted while switching schema alternatives. Resolve the same requested concept in this endpoint category and retain its filter; a source-wide probe must be diagnostic, or disclose the unresolved condition as partial.')


def missing_scope_alternatives(pack, question, queries, selections):
    """Validate selected scope against release-declared alternative families.

    Never generate or route a query. Explicit source/subtype qualifiers narrow
    only their own schema family; missing coverage is returned as tool feedback.
    """
    import re
    selected=[]; branches=[]
    for item in selections:
        if item['purpose'] != 'requested': continue
        p=queries[item['query_id']]['pattern'];nodes={n['key']:n['label'] for n in p['nodes']}
        branch=[{'relation':e['type'],'target':nodes[e['target']], 'ends':{e['source'],e['target']}} for e in p['edges']]
        branches.append(branch); selected.extend(branch)
    def matches(member,edge):return member['relation']==edge['relation'] and ('target' not in member or member['target']==edge['target'])
    missing=[]; required=[]
    for family in pack.get('scope_families',[]):
        members=family['members']
        if not any(matches(m,e) for m in members for e in selected):continue
        def qualified(spec):return any(re.search(r'(?<!\w)'+re.escape(q)+(r'(?!\w)' if q[-1:].isalnum() else ''),question,re.I) for q in spec.get('qualifiers',[]))
        explicit=[m for m in members if qualified(m)]
        groups=[]
        for group in family.get('qualifier_groups',[]):
            if qualified(group) and not any(matches(g,m) for g in group['members'] for m in explicit):
                groups.extend(group['members'])
        applicable=[m for m in members if m in explicit or any(matches(g,m) for g in groups)] or members
        required.append(applicable)
        for member in applicable:
            if not any(matches(member,e) for e in selected):missing.append({k:v for k,v in member.items() if k!='qualifiers'})
    # Alternatives in joined families must be covered in the same query and
    # at a shared node. Separate marginal coverage cannot prove the conjunction.
    import itertools
    for left,right in itertools.combinations(required,2):
        def joined(branch,a,b):
            return any(matches(a,x) and matches(b,y) and x is not y and x['ends'] & y['ends'] for x in branch for y in branch)
        if not any(joined(branch,a,b) for branch in branches for a in left for b in right):
            continue
        for a,b in itertools.product(left,right):
            if not any(joined(branch,a,b) for branch in branches):
                missing.append({'joined_alternatives':[{k:v for k,v in m.items() if k!='qualifiers'} for m in (a,b)]})
    return missing


def unbound_user_concepts(question, queries, selections, contexts):
    """Require literally user-named resolved concepts to survive final selection.

    Union coverage permits alternatives across separate requested branches.
    This validates observed bindings, not natural-language scientific meaning.
    """
    import re
    def norm(text):return ' '.join(re.findall(r'\w+',str(text).casefold()))
    original=' '+norm(question)+' '
    groups={}
    for ctx in contexts:
        if ctx.get('tool')!='resolve_entity':continue
        text=ctx.get('input',{}).get('text','');key=norm(text)
        if len(key)<2 or ' '+key+' ' not in original:continue
        groups.setdefault(key,[]).extend(ctx.get('output',{}).get('candidates',[]))
    patterns=[queries[s['query_id']]['pattern'] for s in selections if s['purpose']=='requested']
    missing=[]
    for concept,candidates in groups.items():
        bound=False
        for p in patterns:
            for n in p['nodes']:
                for f in n.get('filters',[]):
                    values=f['value'] if isinstance(f['value'],list) else [f['value']]
                    if any(norm(v)==concept for v in values):bound=True
                    for c in candidates:
                        if n['label'] not in c.get('labels',[]):continue
                        observed=c.get('properties',{}).get(f['property'])
                        observed=observed if isinstance(observed,list) else [observed]
                        if any(v in observed for v in values):bound=True
        if not bound:missing.append({'unbound_user_concept':concept,'instruction':'Bind this resolved user concept in the full requested conjunction. An empty prefix does not exercise the omitted condition.'})
    return missing


def selected_prefixes(queries, selections):
    """A one-edge probe cannot qualify alongside its stricter joined query."""
    import json
    selected=[s['query_id'] for s in selections if s['purpose']=='requested']
    def filters(n):return {json.dumps(f,sort_keys=True) for f in n.get('filters',[])}
    missing=[]
    for qid in selected:
        p=queries[qid]['pattern']
        if len(p['edges'])!=1:continue
        edge=p['edges'][0];nodes={n['key']:n for n in p['nodes']}
        for other in selected:
            full=queries[other]['pattern'];fn={n['key']:n for n in full['nodes']}
            if len(full['edges'])<=1:continue
            for e in full['edges']:
                if e['type']!=edge['type'] or e.get('direction','out')!=edge.get('direction','out') or not filters(edge)<=filters(e):continue
                orientations=[(e['source'],e['target'])]
                if e.get('direction')=='either':orientations.append((e['target'],e['source']))
                if any(all(nodes[k]['label']==fn[v]['label'] and filters(nodes[k])<=filters(fn[v]) for k,v in zip((edge['source'],edge['target']),ends)) for ends in orientations):
                    missing.append({'query_id':qid,'prefix_of':other,'instruction':'Exclude this relaxed prefix from requested selections; select the complete conjunction. Source probes belong to diagnostics.'});break
            else:continue
            break
    return missing
