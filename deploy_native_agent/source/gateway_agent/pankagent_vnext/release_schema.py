"""Reviewed full-release structural inventory; no answer queries or model calls."""
from .agent_schemas import module as schema_module
import hashlib
import json
import re
from pathlib import Path

_RAW = json.dumps(schema_module('graph_storage')['registry'], sort_keys=True).encode()
REGISTRY = json.loads(_RAW)
DIGEST = hashlib.sha256(_RAW + Path(__file__).read_bytes()).hexdigest()


def relationship_bindings(tokens):
    result = {}
    for i, token in enumerate(tokens[:-3]):
        if token.value == '[' and tokens[i+1].kind in ('WORD', 'IDENT') and tokens[i+2].value == ':':
            end = next((j for j in range(i+3,len(tokens)) if tokens[j].value in ('{',']')),len(tokens))
            result[tokens[i+1].value] = {tokens[j+1].value for j in range(i+2,end-1) if tokens[j].value in (':','|')}
    for i, token in enumerate(tokens[1:-1], 1):
        if token.value.upper() == 'AS' and tokens[i-1].value in result:
            result[tokens[i+1].value] = set(result[tokens[i-1].value])
    return result


def structural_errors(tokens, step, parameters):
    from .graph import _pattern_bindings, _predicate_owner, _predicate_present
    # OPTIONAL patterns also require structural validation, but cannot satisfy
    # mandatory filters in the independent mandatory-pattern validator.
    structural = [t for t in tokens if not (t.kind == 'WORD' and t.value.upper() == 'OPTIONAL')]
    undirected_patterns = []
    nodes, paths = _pattern_bindings(structural, undirected_patterns=undirected_patterns)
    relationships = relationship_bindings(tokens)
    if step.get('graph_version') != REGISTRY['release'] and not any(k in REGISTRY['relations'] for kinds in relationships.values() for k in kinds):
        return []
    errors = []
    for source, target, kinds in paths:
        for kind in kinds:
            spec = REGISTRY['relations'].get(kind)
            if not spec:
                errors.append('unknown_relationship:' + kind)
                continue
            def fits(a, b):
                return any((not nodes.get(a) or nodes[a] <= set(p['source'])) and
                           (not nodes.get(b) or nodes[b] <= set(p['target'])) for p in spec['paths'])
            undirected = (source, target, kinds) in undirected_patterns
            if not fits(source, target) and not (undirected and fits(target, source)):
                errors.append('invalid_relationship_endpoints:' + kind)
    interaction_scope = step.get('complete',True) and not re.search(r'outgoing|incoming|as (?:the )?(?:source|target)|directional', step.get('question',''), re.I)
    if interaction_scope:
        for i,t in enumerate(tokens[:-3]):
            if t.value != '[' or tokens[i+2].value != ':': continue
            if tokens[i+3].value not in {'PHYSICAL_INTERACTION','GENETIC_INTERACTION'}: continue
            end=next((j for j in range(i+3,len(tokens)) if tokens[j].value==']'),len(tokens)-1)
            if '<' in [t.value for t in tokens[max(0,i-2):i]] or '>' in [t.value for t in tokens[end+1:end+3]]:
                errors.append('interaction_partner_scope_requires_undirected_match')
        if any(k in {'PHYSICAL_INTERACTION','GENETIC_INTERACTION'} for kinds in relationships.values() for k in kinds):
            from .graph import _constraint_choices
            for index,c in enumerate(step.get('constraints',[])):
                if c.get('entity_type')=='Gene' and c.get('operator','=')=='=' and c.get('property') in ('id','name'):
                    choices=_constraint_choices(step,index,c)
                    anchors={v for v,labels in nodes.items() if 'Gene' in labels and any(_predicate_present(tokens,choice,parameters,{v}) for choice in choices)}
                    for i,t in enumerate(tokens[1:-1],1):
                        if t.value.upper()=='AS' and tokens[i-1].value in anchors:anchors.discard(tokens[i+1].value)
                    if len(anchors)>1:errors.append('overspecified_interaction_anchor')
    for variable, kinds in relationships.items():
        if not any(kinds & p[2] for p in paths):
            errors.append('unsupported_relationship_pattern:' + variable)
    # Infer possible endpoint classes only from the full directed registry.
    # A property is safe on an unlabelled endpoint only if every possible class
    # combination supports it; this does not narrow or rewrite the query.
    possible = {}
    for source, target, kinds in paths:
        for kind in kinds:
            for path in REGISTRY['relations'].get(kind, {}).get('paths', []):
                if nodes.get(source) and not nodes[source] <= set(path['source']): continue
                if nodes.get(target) and not nodes[target] <= set(path['target']): continue
                for variable, side in ((source,'source'),(target,'target')):
                    possible.setdefault(variable, []).append(path[side])
    for i,t in enumerate(tokens[1:-1],1):
        if t.value.upper()=='AS' and tokens[i-1].value in possible:
            possible[tokens[i+1].value]=possible[tokens[i-1].value]
    for variable, labels in nodes.items():
        if labels - REGISTRY['nodes'].keys():
            errors.append('unknown_node_type:' + variable)
    for i, token in enumerate(tokens):
        dot = i >= 2 and tokens[i-1].value == '.'
        mapped = i > 0 and i+1 < len(tokens) and tokens[i-1].value in ('{', ',') and tokens[i+1].value == ':'
        if not (dot or mapped) or token.kind not in ('WORD','IDENT'):
            continue
        owner = _predicate_owner(tokens, i)
        if owner in relationships:
            allowed = set.intersection(*(set(REGISTRY['relations'].get(k, {}).get('properties', [])) for k in relationships[owner]))
            if token.value not in allowed:
                errors.append('invalid_relationship_property:' + ','.join(sorted(relationships[owner])) + '.' + token.value)
        elif owner in nodes:
            if not nodes[owner]:
                candidates=possible.get(owner, [])
                if not candidates or not all(any(token.value in REGISTRY['nodes'].get(label, []) for label in labels) for labels in candidates):
                    errors.append('untyped_property_owner:' + str(owner))
            elif not any(token.value in REGISTRY['nodes'].get(label, []) for label in nodes[owner]):
                errors.append('invalid_node_property:' + ','.join(sorted(nodes[owner])) + '.' + token.value)
    for c in step.get('constraints', []):
        kind = c.get('relationship_type')
        if kind:
            variables = {v for v, kinds in relationships.items() if kind in kinds}
            if not variables or not _predicate_present(tokens, c, parameters, variables):
                errors.append('missing_relationship_filter:' + kind + '.' + c['property'])
    return sorted(set(errors))


def normalize_constraints(step):
    """Canonicalize only unique recorded categories and reviewed aliases."""
    from copy import deepcopy
    result = deepcopy(step)
    relations = result.get('relation_types', [])
    resolutions = result.setdefault('resolved_constraints', [])
    for c in result.get('constraints', []):
        requested = deepcopy(c)
        value = c.get('value')
        if c.get('property') == 'name' and isinstance(value, str):
            aliases = REGISTRY['aliases'].get(c.get('entity_type'), {})
            canonical = aliases.get(value.casefold())
            if canonical:
                c['value'] = canonical
                resolutions.append({'requested':requested, 'canonical_binding':deepcopy(c), 'match_kind':'verified_alias', 'registry_version':REGISTRY['version'], 'source':'reviewed release entity inventory'})
        if c.get('property') not in ('id','name'):
            owners = [r for r in relations if c.get('property') in REGISTRY['relations'].get(r,{}).get('properties', [])]
            # Explicit node ownership is never overwritten by a relationship guess.
            if len(owners) == 1 and (not c.get('entity_type') or c.get('property') not in REGISTRY['nodes'].get(c.get('entity_type'), [])):
                c['entity_type'] = None
                c['relationship_type'] = owners[0]
                c['owner_kind'] = 'relationship'
                values = REGISTRY['categories'].get(owners[0]+'.'+c['property'], [])
                matches = [v for v in values if isinstance(v,str) and isinstance(value,str) and v.casefold()==value.casefold()]
                if len(matches)==1:
                    c['value'] = matches[0]
                resolutions.append({'requested':requested,'canonical_binding':deepcopy(c),'match_kind':'recorded_property_binding','registry_version':REGISTRY['version'],'source':'full-release property/category inventory'})
    return result


def guidance(relations):
    lines = []
    for kind in relations:
        spec = REGISTRY['relations'].get(kind)
        if not spec: continue
        priority = ('Gene','donor','Sample_node','data_modality','disease','anatomical_structure','GO_term','kegg','reactome','variants','OCR_peak')
        def label(labels): return next((v for v in priority if v in labels), labels[0])
        paths = sorted(set('(a:'+label(p['source'])+')-[:'+kind+']->(b:'+label(p['target'])+')' for p in spec['paths']))
        if kind in ('PHYSICAL_INTERACTION','GENETIC_INTERACTION'):
            paths=[p.replace(']->',']-') for p in paths]
        lines.append('; '.join(paths)+'; relationship properties: '+', '.join(spec['properties']))
    return '\nVerified query paths (use the shown direction, or undirected interaction pattern; never move edge properties onto nodes):\n'+'\n'.join(lines)


def canonicalize_symbols(query):
    """Only unique case variants of declared schema symbols; preserve literals.

    This is a recorded schema normalization of GPU output, not a query template
    or a repair of scientific predicates, joins, direction or limits.
    """
    token = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"|`(?:``|[^`])*`|[A-Za-z_][A-Za-z_0-9]*|[^\s]")
    spans=list(token.finditer(query));stack=[];changes=[];previous=None
    for m in spans:
        value=m.group()
        if value.startswith(('//','/*',"'",'"')): continue
        if value in ('(','[','{'):stack.append(value)
        elif value in (')',']','}'):
            if stack:stack.pop()
        elif previous == ':' and stack and stack[-1] in ('(','['):
            names=REGISTRY['nodes'] if stack[-1]=='(' else REGISTRY['relations']
            raw=value.strip('`');matches=[n for n in names if n.casefold()==raw.casefold()]
            if len(matches)==1 and raw != matches[0]:
                changes.append({'start':m.start(),'end':m.end(),'requested':raw,'canonical':matches[0],'kind':'unique_schema_case_match'})
        previous=value
    normalized=query
    for c in reversed(changes):normalized=normalized[:c['start']]+'`'+c['canonical']+'`'+normalized[c['end']:]
    return normalized, [{k:v for k,v in c.items() if k not in ('start','end')} for c in changes]
