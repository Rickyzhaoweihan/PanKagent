"""Deterministic matching for the full release anatomy inventory. No inference calls."""
import re
from copy import deepcopy
from difflib import SequenceMatcher

VERSION = 'anatomy-resolution-3'
RELEASE = 'PanKgraph_08_04'
# Reviewed against current named records; an alias is active only if ID and name agree.
ALIASES = {
 'CL_0000171': ('alpha cell', ['alpha']),
 'CL_0000169': ('beta cell', ['beta']),
 'CL_0000173': ('delta cell', ['delta']),
 'CL_0002079': ('ductal cell', ['ductal']),
 'CL_0000115': ('endothelial cell', ['endothelial']),
 'UBERON_0000006': ('pancreatic islet (islet of Langerhans)', ['islet','islet of Langerhans','pancreatic islet']),
 'UBERON_0015865': ('pancreaticosplenic lymph node (proxy for "pancreatic LN")', ['PLN','pancreatic lymph node','pancreatic LN']),
 'CL_0002064': ('pancreatic acinar cell', ['acinar cell']),
 'CL_0002410': ('pancreatic stellate cell', ['stellate cell']),
 'CL_0002410_active': ('pancreatic stellate cell active state', ['active pancreatic stellate cell','activated pancreatic stellate cell','ActiveStellate']),
 'CL_0002410_quiescent': ('pancreatic stellate cell quiescent state', ['quiescent pancreatic stellate cell','QuiescentStellate']),
 'CL_0002275': ('pancreatic PP cell (gamma cell)', ['gamma cell','PP cell']),
 'CL_0005019': ('pancreatic epsilon cell', ['epsilon cell']),
 'CL_0002079_MUC5B': ('pancreatic ductal cell MUC5B+', ['MUC5B+ ductal','MUC5B+ ductal cell']),
 'CL_0000738': ('leukocyte (immune cell)', ['leukocyte','immune cell']),
}

def normalize(value):
    value = str(value).translate(str.maketrans({'α':'alpha','β':'beta','δ':'delta','γ':'gamma','ε':'epsilon','Α':'alpha','Β':'beta','Δ':'delta','Γ':'gamma','Ε':'epsilon'}))
    value = re.sub(r'([a-z])([A-Z])', r'\1 \2', value).casefold()
    value = re.sub(r'[_\-\s]+', ' ', value).strip()
    return re.sub(r'\b(cells|islets|nodes|leukocytes|macrophages|fibroblasts|neutrophils|granulocytes|lymphocytes)\b', lambda m:m[0][:-1], value)

def one_typo(a,b):
    if a == b:return True
    if min(len(a),len(b)) < 5 or re.search(r'[^a-z]',a+b):return False
    if len(a)==len(b):
        different=[i for i in range(len(a)) if a[i]!=b[i]]
        return len(different)==1 or (len(different)==2 and different[1]==different[0]+1 and a[different[0]]==b[different[1]] and a[different[1]]==b[different[0]])
    if abs(len(a)-len(b))!=1:return False
    short,long=(a,b) if len(a)<len(b) else (b,a)
    return any(long[:i]+long[i+1:]==short for i in range(len(long)))

def resolve_anatomy(value, records, release, prop='name'):
    """Auto-match exact/normalized aliases or one unique conservative word typo.

    Fuzzy suggestions never authorize execution. Marker digits, +/- status, short
    cell-lineage tokens and tissue/state qualifiers cannot be removed by a typo.
    """
    records=[r for r in records if isinstance(r.get('id'),str) and isinstance(r.get('name'),str)]
    id_value=re.sub(r'^(CL|UBERON):',r'\1_',str(value),flags=re.I)
    id_matches=[r for r in records if r['id'].casefold()==id_value.casefold()]
    if id_matches:return outcome(id_matches,'exact_id',value)
    if prop=='id':
        # A planner can put an ordinary exact name in its ID field. Correct
        # that type error only against a recorded name; never fuzz an ID.
        if not re.search(r'\d|[:_]',str(value)):
            exact_names=[r for r in records if r['name'].casefold()==str(value).casefold()]
            if exact_names:return outcome(exact_names,'verified_name_in_id_field',value)
        return {'state':'not_found','candidates':[],'match_kind':'unresolved_id','resolver_version':VERSION}
    q=normalize(value);forms=[]
    for record in records:
        forms.append((normalize(record['name']),record,'normalized_name'))
        if release==RELEASE and record['id'] in ALIASES:
            name,aliases=ALIASES[record['id']]
            if record['name']==name:
                for alias in aliases:forms.append((normalize(alias),record,'dataset_proxy' if record['id']=='UBERON_0015865' else 'verified_alias'))
    exact=[(r,k) for term,r,k in forms if term==q]
    if exact:return outcome([r for r,k in exact],exact[0][1],value)
    words=q.split();typos=[]
    for term,r,k in forms:
        tokens=term.split()
        if len(words)==len(tokens) and sum(a!=b for a,b in zip(words,tokens))==1 and all(one_typo(a,b) for a,b in zip(words,tokens)):
            typos.append(r)
    if typos:return outcome(typos,'spelling_correction',value)
    scores={}
    for term,r,k in forms:
        score=SequenceMatcher(None,q,term).ratio()
        if score>=.45 and score>scores.get(r['id'],({},0))[1]:scores[r['id']]=(r,score)
    ranked=sorted(scores.values(),key=lambda item:(-item[1],item[0]['id']))[:5]
    return {'state':'needs_clarification' if ranked else 'not_found','candidates':[{**r,'similarity':round(score,3)} for r,score in ranked],
            'match_kind':'suggestions_only','resolver_version':VERSION,'original_term':value}

def outcome(records,kind,value):
    unique={r['id']:deepcopy(r) for r in records};candidates=list(unique.values())
    result={'state':'resolved' if len(candidates)==1 else 'ambiguous','candidates':candidates,'match_kind':kind,
            'resolver_version':VERSION,'original_term':value}
    if len(candidates)==1:result.update(candidates[0],entity_type='anatomical_structure',unique_pattern_match=True)
    return result
