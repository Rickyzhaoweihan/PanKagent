"""Explicit direction/ranking intent for registered measured relationships.

Ranks by effect size, significance and expression abundance are distinct. No
ranking metric or significance cutoff is invented for an ambiguous 'top' set.
"""
import copy
import hashlib
import math
import re
from pathlib import Path

VERSION='requested-measurement-ranking-v1'
RELEASE='PanKgraph_08_04'
FIELDS={
 'T1D_DEG_IN':{'effect':'log2_fold_change','adjusted':'adjusted_p_value','nominal':'p_value'},
 'GENE_ENRICHED_IN':{'effect':'log2_fold_change','adjusted':'padj','nominal':'pvalue'},
 'GENE_DETECTED_IN':{'median log cpm':'median_donor_log_cpm','mean log cpm':'mean_donor_log_cpm',
                     'median cpm':'median_donor_cpm','mean cpm':'mean_donor_cpm',
                     'median percent':'median_pct_cells_expressing','mean percent':'mean_pct_cells_expressing'},
}
DIGEST=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def parse_intent(text, relation):
    text=re.sub(r'[-_]',' ',str(text or '').lower())
    fields=FIELDS.get(relation,{})
    result={}
    n=re.search(r'\b(?:top|first|the)\s+(\d+)\s+(?!percent\b)(?:most\s+|highest\s+|lowest\s+|genes\b|results\b)',text)
    if not n:n=re.search(r'\btop\s*(\d+)\b(?!\s*%)',text)
    if n:result['top_n']=int(n.group(1))
    up=bool(re.search(r'\b(?:up\s*regulated|increased|enriched)\b',text))
    down=bool(re.search(r'\b(?:down\s*regulated|decreased|depleted)\b',text))
    selecting = bool(result.get('top_n') or re.search(r'\bgenes\b|\bwhich\s+gene\b', text))
    if 'effect' in fields:
        if selecting and up and not down:result['effect_direction']='positive'
        elif selecting and down and not up:result['effect_direction']='negative'
        if re.search(r'\bmost\s+(?:up\s*regulated|increased|enriched)\b',text):
            result.update(property=fields['effect'],order='DESC')
        if re.search(r'\bmost\s+(?:down\s*regulated|decreased|depleted)\b',text):
            result.update(property=fields['effect'],order='ASC')
        if re.search(r'\bhighest\s+log2?\s*(?:fold\s*change|fc)\b',text):result.update(property=fields['effect'],order='DESC')
        if re.search(r'\blowest\s+log2?\s*(?:fold\s*change|fc)\b',text):result.update(property=fields['effect'],order='ASC')
        if re.search(r'\b(?:sort|rank|order)\b.{0,35}\b(?:effect\s*size|fold\s*change)\b',text):
            result.update(property=fields['effect'],order='ASC' if down and not up or re.search(r'\bascending\b',text) else 'DESC',effect_order_explicit=True)
        # A specifically requested significance order is not effect-size order.
        if re.search(r'\bmost\s+(?:statistically\s+)?significan(?:t|tly)\b|\b(?:sort|rank|order)\b.{0,35}\b(?:adjusted|padj|q\s*value)\b',text):
            result.update(property=fields['adjusted'],order='ASC')
        if re.search(r'\b(?:sort|rank|order|lowest|smallest)\b.{0,35}\b(?:nominal|unadjusted|raw)\s+p\s*value',text):
            result.update(property=fields['nominal'],order='ASC')
    if relation=='GENE_DETECTED_IN':
        ranking=bool(re.search(r'\b(?:highest|largest|lowest|smallest|sort|rank|order)\b',text))
        for key,field in fields.items():
            pattern=key.replace(' ','\\s*')
            if ranking and re.search(pattern,text):
                result.update(property=field,order='ASC' if re.search(r'\b(?:lowest|smallest|ascending)\b',text) else 'DESC')
    if (result or selecting) and (up and down or re.search(r'\b(?:not|exclude|excluding|without|neither)\b.{0,20}\b(?:up\s*regulated|down\s*regulated|increased|decreased)\b',text)):
        result['ambiguous_effect']=True
    if result:
        result.update(version=VERSION,graph_release=RELEASE,relation_type=relation)
    return result


def recovery(message,relation=None):
    value={'category':'ranking_needs_clarification','title':'Choose how to rank these results',
            'message':message,'retryable':False,'suggestions':[
                {'label':'Rank by effect size','instruction':'Rank by effect size using the recorded log2 fold change. Keep the requested expression direction, other filters and number of results.'},
                {'label':'Rank by statistical support','instruction':'Rank by the smallest recorded adjusted p-value. Keep the requested expression direction, other filters and number of results.'}]}
    if relation=='GENE_DETECTED_IN':
        value['suggestions']=[
            {'label':'Rank by median expression','instruction':'Rank by highest recorded median log CPM, keeping my other filters and requested number.'},
            {'label':'Rank by detection percentage','instruction':'Rank by highest recorded median percent of cells expressing each gene, keeping my other filters and requested number.'}]
    return value


def attach_to_plan(plan, graph_release=RELEASE):
    """Derive contracts from explicit intent; preserve parent requirements on revisions."""
    output=copy.deepcopy(plan);steps=output.get('steps')or[];trace=output.get('revision_trace')or{}
    if graph_release != RELEASE:
        return output
    previous=trace.get('before_steps')or[]
    for step in steps:
        step.pop('ranking_contract',None);step.pop('ranking_issue',None)
        relations=[r for r in step.get('relation_types',[]) if r in FIELDS]
        if len(relations)!=1:continue
        relation=relations[0];derived={};source='step_question'
        if trace:
            parents=[s for s in previous if relation in s.get('relation_types',[])]
            if len(parents)==1:
                parent=parents[0];derived=copy.deepcopy(parent.get('ranking_contract') or parse_intent(parent.get('question'),relation))
                source='parent_contract'
            instruction=str(trace.get('instruction') or '')
            # A revision can explicitly replace a previously selected direction.
            changes=parse_intent(instruction+' genes' if derived and not re.search(r'\bgenes\b', instruction, re.I) else instruction,relation)
            if changes:
                derived.update(changes);source='revision_instruction'
                if changes.get('effect_direction') and not changes.get('property') and derived.get('property') == FIELDS[relation].get('effect'):
                    derived['order']='DESC' if changes['effect_direction']=='positive' else 'ASC'
            if re.search(r'\b(?:all|every)\b.{0,20}\b(?:genes|results)\b|\bremove\s+(?:the\s+)?(?:limit|top)',str(trace.get('instruction','')),re.I):
                derived.pop('top_n',None)
                step['complete']=True
            if not derived:derived=parse_intent(step.get('question'),relation)
            if changes.get('effect_order_explicit') and derived.get('effect_direction')=='negative' and not re.search(r'\b(?:ascending|descending)\b',str(trace.get('instruction','')),re.I):
                derived['order']='ASC'
        else:
            original=parse_intent(output.get('original_question'),relation) if sum(relation in other.get('relation_types',[]) for other in steps)==1 else {}
            derived=original or parse_intent(step.get('question'),relation)
            source='original_question' if original else 'step_question'
        if not derived:continue
        if derived.pop('ambiguous_effect',False):
            step['ranking_issue']=recovery('The requested effect direction or ranking has more than one meaning. Specify an increase, a decrease, or separate ranked lists; the remaining filters will be kept.',relation)
        elif 'top_n' in derived and derived['top_n']<1:
            step['ranking_issue']=recovery('The requested ranked list must contain at least one result. Choose a positive number while keeping your biological filters.',relation)
        elif 'top_n' in derived and 'property' not in derived:
            step['ranking_issue']=recovery('“Top” does not identify a ranking statistic. Choose effect size or a supported expression statistic before we select a limited set; your biological filters will be kept.',relation)
        else:
            derived['intent_source']=source;step['ranking_contract']=derived
            if derived.get('top_n'):step['complete']=False
    return output


def same_number(left,right):
    if isinstance(left,bool) or isinstance(right,bool):return False
    try:return math.isfinite(float(left)) and math.isfinite(float(right)) and float(left)==float(right)
    except (ValueError,TypeError,OverflowError):return False


def guidance(step):
    contract=step.get('ranking_contract')or{}
    if not contract:return ''
    kind=contract['relation_type'];parts=[]
    direction=contract.get('effect_direction')
    if direction:parts.append(f"require {kind}.log2_fold_change {'> 0' if direction=='positive' else '< 0'}")
    if contract.get('property'):parts.append(f"primary ORDER BY the SAME {kind}.{contract['property']} {contract['order']}")
    if contract.get('top_n'):parts.append(f"select exactly the requested top {contract['top_n']} measurement rows using LIMIT {contract['top_n']} after this ordering and BEFORE collect/count/graph aggregation")
    return '\nExplicit requested ranking contract: '+'; '.join(parts)+'. Use one measured Gene-to-cell path; return the selected gene identities and supporting measurements. Do not add Cartesian nodes, extra paths, or joins before selection. Do not substitute p-value ranking for fold-change ranking or add an unrequested significance threshold.'


def bindings(tokens):
    relationships={};fields={};roots={}
    for i,t in enumerate(tokens[:-3]):
        if t.value=='[' and tokens[i+1].kind in ('WORD','IDENT') and tokens[i+2].value==':':
            end=next((j for j in range(i+3,len(tokens)) if tokens[j].value in ('{',']')),len(tokens))
            kinds={tokens[j+1].value for j in range(i+2,end-1) if tokens[j].value in (':','|')}
            name=tokens[i+1].value
            relationships[name]=kinds;roots[name]=name
    def start_of_expression(index):
        return index > 0 and tokens[index-1].value.upper() in {'RETURN','WITH',',','DISTINCT'}
    for i,t in enumerate(tokens[1:-1],1):
        if t.value.upper()!='AS' or t.kind!='WORD':continue
        new=tokens[i+1].value
        property_alias=(roots[tokens[i-3].value],tokens[i-1].value) if i>=3 and tokens[i-2].value=='.' and tokens[i-3].value in relationships and start_of_expression(i-3) else None
        node_alias=tokens[i-1].value if start_of_expression(i-1) and tokens[i-1].value in relationships else None
        field_alias=fields.get(tokens[i-1].value) if start_of_expression(i-1) else None
        node_binding=(set(relationships[node_alias]),roots[node_alias]) if node_alias else None
        fields.pop(new,None);relationships.pop(new,None);roots.pop(new,None)
        if property_alias:fields[new]=property_alias
        elif node_alias:
            relationships[new],roots[new]=node_binding
        elif field_alias:fields[new]=field_alias
    return relationships,fields,roots


def direction_present(tokens,direction,parameters,allowed_roots,aliases,roots):
    """Only an exact, mandatory simple numeric comparison establishes sign."""
    from .graph import _value
    clause='';optional=False
    for i,t in enumerate(tokens):
        if t.kind=='WORD' and t.value.upper() in {'MATCH','WHERE','RETURN','WITH','UNWIND','ORDER'}:
            clause=t.value.upper()
            if clause=='MATCH':optional=i>0 and tokens[i-1].value.upper()=='OPTIONAL'
        if clause!='WHERE' or optional or t.kind not in {'WORD','IDENT'}:continue
        if i==0 or tokens[i-1].value.upper() not in {'WHERE','AND','('}:continue
        if tokens[i-1].value=='(' and i>1 and tokens[i-2].kind=='WORD' and tokens[i-2].value.upper() not in {'WHERE','AND'}:continue
        if i+2<len(tokens) and tokens[i+1].value=='.':
            field=(roots.get(t.value,t.value),tokens[i+2].value);at=i+3
        else:field=aliases.get(t.value);at=i+1
        if not field or field[0] not in allowed_roots or field[1]!='log2_fold_change' or at>=len(tokens):continue
        if tokens[at].value!=('>' if direction=='positive' else '<'):continue
        actual,end=_value(tokens,at+1,parameters)
        if not isinstance(actual,(int,float)) or isinstance(actual,bool) or actual!=0:continue
        if end<len(tokens) and tokens[end].value.upper() not in {'AND','RETURN','WITH','ORDER','LIMIT',')',';'}:continue
        return True
    return False


def row_shape_errors(tokens,step,contract):
    """Limit ranked retrieval to one measured path and retained gene identities."""
    from .graph import _pattern_bindings
    node_types,paths=_pattern_bindings(tokens,graph_release=step.get('graph_version'))
    clause='';nodes=[];relationships=[]
    for i,t in enumerate(tokens[:-2]):
        if t.kind=='WORD' and t.value.upper() in {'MATCH','WHERE','RETURN','WITH','UNWIND','ORDER'}:clause=t.value.upper()
        if clause=='MATCH' and t.value=='(' and tokens[i+1].kind in {'WORD','IDENT'} and tokens[i+2].value in {':',')','{'}:nodes.append(tokens[i+1].value)
        if clause=='MATCH' and t.value=='[' and tokens[i+1].kind in {'WORD','IDENT'} and tokens[i+2].value==':':relationships.append(tokens[i+1].value)
    if len(nodes)!=2 or len(relationships)!=1 or any(t.kind=='WORD' and t.value.upper() in {'UNWIND','OPTIONAL'} for t in tokens):
        return ['ranking_requires_one_measured_path_without_row_multiplication']
    genes={name for name in nodes if 'Gene' in node_types.get(name,set())}
    if len(genes)!=1:return ['ranking_gene_identity_binding_missing']
    projections={name:('node',name in genes) for name in nodes}
    projections.update({name:('relationship',False) for name in relationships})
    def split_at_level(expression,separator):
        depth=0;parts=[];part=[]
        for t in expression:
            if t.value in {'(','[','{'}:depth+=1
            elif t.value in {')',']','}'}:depth-=1
            if depth==0 and t.value==separator:parts.append(part);part=[]
            else:part.append(t)
        parts.append(part)
        return parts
    def encloses(expression,left,right):
        if len(expression)<2 or expression[0].value!=left or expression[-1].value!=right:return False
        depth=0
        for i,t in enumerate(expression):
            if t.value==left:depth+=1
            elif t.value==right:depth-=1
            if depth==0 and i<len(expression)-1:return False
        return depth==0
    def projection(expression):
        # Exact grammar: references, ID/name fields, unsliced collections,
        # literal lists/maps and collection concatenation. No CASE, arithmetic,
        # identity suffixes, slicing, size/count or arbitrary functions.
        if not expression:return None
        if expression[0].value.upper()=='DISTINCT':return projection(expression[1:])
        parts=split_at_level(expression,'+')
        if len(parts)>1:
            values=[projection(part) for part in parts]
            return ('collection',any(v[1] for v in values)) if all(v and v[0]=='collection' for v in values) else None
        if len(expression)==1:
            if expression[0].value=='*':return ('star',any(v[1] for v in projections.values()))
            return projections.get(expression[0].value)
        if len(expression)==3 and expression[1].value=='.' and expression[0].value in projections:
            owner=projections[expression[0].value]
            return ('scalar',owner[1] and owner[0]=='node' and expression[2].value in {'id','name'})
        if expression[0].value.lower()=='collect' and encloses(expression[1:],'(',')'):
            value=projection(expression[2:-1])
            return ('collection',value[1]) if value and value[0] in {'node','relationship','scalar','map'} else None
        if encloses(expression,'[',']'):
            values=[projection(part) for part in split_at_level(expression[1:-1],',')]
            return ('collection',any(v[1] for v in values)) if all(values) else None
        if encloses(expression,'{','}'):
            values=[]
            for part in split_at_level(expression[1:-1],','):
                if len(part)<3 or part[1].value!=':':return None
                values.append(projection(part[2:]))
            return ('map',any(v[1] for v in values)) if all(values) else None
        return None
    i=0;returned=False
    while i<len(tokens):
        if tokens[i].kind!='WORD' or tokens[i].value.upper() not in {'WITH','RETURN'}:i+=1;continue
        kind=tokens[i].value.upper();i+=1;parts=[];part=[];depth=0
        while i<len(tokens):
            t=tokens[i]
            if depth==0 and t.kind=='WORD' and t.value.upper() in {'WHERE','ORDER','LIMIT','MATCH','OPTIONAL','UNWIND','WITH','RETURN'}:break
            if t.value in {'(','[','{'}:depth+=1
            elif t.value in {')',']','}'}:depth-=1
            if depth==0 and t.value==',':parts.append(part);part=[]
            else:part.append(t)
            i+=1
        if part:parts.append(part)
        projected={};found=False
        for part in parts:
            if part and part[0].value.upper()=='DISTINCT':part=part[1:]
            alias=next((j for j,t in enumerate(part) if t.kind=='WORD' and t.value.upper()=='AS'),None)
            expr=part[:alias] if alias is not None else part
            value=projection(expr);found|=bool(value and value[1])
            if value:
                if value[0]=='star':projected.update(projections)
                elif alias is not None and alias+1<len(part):projected[part[alias+1].value]=value
                elif len(expr)==1:projected[expr[0].value]=value
        if kind=='WITH':projections=projected
        else:returned=found
    return [] if returned else ['ranking_must_return_selected_gene_identities']


def validation_errors(tokens,step,parameters):
    contract=step.get('ranking_contract')or{}
    if not contract:return []
    if contract.get('graph_release')!=step.get('graph_version') or contract.get('version')!=VERSION:
        return ['ranking_contract_stale']
    from .graph import _value
    if any(t.kind=='WORD' and t.value.upper()=='UNION' for t in tokens):
        return ['ranked_union_requires_explicit_global_scope']
    rels,aliases,roots=bindings(tokens);variables={v for v,kinds in rels.items() if kinds=={contract['relation_type']}}
    if not variables:return ['ranking_relationship_binding_missing']
    errors=row_shape_errors(tokens,step,contract) if contract.get('property') or contract.get('top_n') else []
    direction=contract.get('effect_direction')
    if any(t.kind=='WORD' and t.value.upper() in {'OR','XOR','NOT'} for t in tokens):
        errors.append('ranking_requires_unambiguous_boolean_scope')
    if direction and not direction_present(tokens,direction,parameters,{roots[v] for v in variables},aliases,roots):
        errors.append('missing_requested_effect_direction:'+direction)
    orders=[i for i,t in enumerate(tokens[:-1]) if t.kind=='WORD' and t.value.upper()=='ORDER' and tokens[i+1].value.upper()=='BY']
    selected=None
    if contract.get('property'):
        if len(orders)!=1:errors.append('missing_or_ambiguous_requested_order')
        else:
            i=orders[0]+2;term=None;end=i+1
            if i+2<len(tokens) and tokens[i+1].value=='.':term=(roots.get(tokens[i].value,tokens[i].value),tokens[i+2].value);end=i+3
            elif i<len(tokens):term=aliases.get(tokens[i].value)
            order=tokens[end].value.upper() if end<len(tokens) and tokens[end].value.upper() in ('ASC','DESC') else 'ASC'
            after=end+1 if end<len(tokens) and tokens[end].value.upper() in ('ASC','DESC') else end
            simple_end=after==len(tokens) or tokens[after].value==',' or tokens[after].value.upper() in {'LIMIT','RETURN','WITH'}
            if not simple_end or not term or term[0] not in variables or term[1]!=contract['property'] or order!=contract['order']:
                errors.append('wrong_requested_order:'+contract['relation_type']+'.'+contract['property']+':'+contract['order'])
            else:
                selected=orders[0]
                if any(t.kind=='WORD' and t.value.upper() in {'MATCH','OPTIONAL','WHERE','UNWIND','SKIP'} for t in tokens[after:]):
                    errors.append('ranking_scope_changes_after_order')
                if direction and not direction_present(tokens,direction,parameters,{term[0]},aliases,roots):
                    errors.append('effect_direction_not_bound_to_ranked_relationship')
    limits=[i for i,t in enumerate(tokens) if t.kind=='WORD' and t.value.upper()=='LIMIT']
    if contract.get('top_n'):
        if len(limits)!=1 or selected is None or limits[0]<=selected:errors.append('missing_requested_top_limit')
        else:
            value,end=_value(tokens,limits[0]+1,parameters)
            if value!=contract['top_n'] or isinstance(value,bool):errors.append('wrong_requested_top_limit')
            if end<len(tokens) and tokens[end].value.upper() not in {'RETURN','WITH',';'}:errors.append('unsupported_top_limit_expression')
            aggregates={'collect','count','sum','avg','min','max','percentilecont','percentiledisc','stdev','stdevp'}
            if any(t.kind=='WORD' and t.value.lower() in aggregates and i+1<len(tokens) and tokens[i+1].value=='(' for i,t in enumerate(tokens[:limits[0]])):
                errors.append('ranking_limit_must_precede_graph_aggregation')
            if any(t.kind=='WORD' and t.value.upper() in {'MATCH','OPTIONAL','WHERE','UNWIND','SKIP'} for t in tokens[end:]):errors.append('ranking_scope_changes_after_limit')
    # Ranking does not authorize filtering away less significant results.
    clause=''
    for i,t in enumerate(tokens):
        if t.kind=='WORD' and t.value.upper() in {'WHERE','RETURN','WITH','ORDER','MATCH'}:clause=t.value.upper()
        if i+2>=len(tokens):continue
        if clause=='MATCH' and t.value in set(FIELDS[contract['relation_type']].values()) and tokens[i+1].value==':':
            errors.append('ranking_statistic_map_requires_explicit_where:'+t.value)
        if clause!='WHERE':continue
        field=(roots.get(tokens[i-2].value),t.value) if i>=2 and tokens[i-1].value=='.' and tokens[i-2].value in variables else aliases.get(t.value)
        if not field or field[0] not in {roots[v] for v in variables} or field[1] not in set(FIELDS[contract['relation_type']].values()):continue
        operator=tokens[i+1].value
        if operator not in {'<','<=','>','>=','='}:
            errors.append('unsupported_ranking_statistic_predicate:'+field[1])
            continue
        value,end=_value(tokens,i+2,parameters)
        if end==i+2:continue
        requested_direction=field[1]=='log2_fold_change' and value==0 and not isinstance(value,bool) and operator==('>' if direction=='positive' else '<') and direction
        requested_constraint=any(c.get('relationship_type')==contract['relation_type'] and c.get('property')==field[1] and c.get('operator','=')==operator and same_number(c.get('value'),value) for c in step.get('constraints',[]))
        if not requested_direction and not requested_constraint:errors.append('unrequested_ranking_cutoff:'+field[1])
    return list(dict.fromkeys(errors))
