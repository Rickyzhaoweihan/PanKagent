"""A narrow output contract for measured gene/cell and coloc evidence.

Validation of a MATCH does not prove that RETURN retains its measurements.
Recognize lossless records, legitimate aggregates, and identified statistics;
unknown projection expressions require the generator to return whole records.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path

VERSION = 'scientific-projection-v1'
SUPPORTED = frozenset({'GENE_ENRICHED_IN','GENE_DETECTED_IN','MARKER_GENE_OF',
    'T1D_DEG_IN','GENE_ACTIVITY_SCORE_IN','SIGNAL_COLOC_WITH'})
LINKAGE_RELATIONS = frozenset({'SIGNAL_COLOC_WITH', 'PART_OF_GWAS_SIGNAL', 'PART_OF_QTL_SIGNAL'})


def _requested(step):
    supported = SUPPORTED | LINKAGE_RELATIONS if step.get('coloc_scope') else SUPPORTED
    return set(step.get('relation_types') or []) & supported
# Provenance and identity fields alone do not retain the requested measurement.
# Intersect these reviewed categories with the pinned release inventory below.
MEASUREMENT_FIELDS = {
    'GENE_ENRICHED_IN': {'base_mean','lfc_se','log2_fold_change','padj','pvalue','rank_in_cell_type','stat'},
    'GENE_DETECTED_IN': {'expression_call','max_donor_log_cpm','max_pct_cells_expressing',
        'mean_donor_cpm','mean_donor_log_cpm','mean_pct_cells_expressing','median_donor_cpm',
        'median_donor_log_cpm','median_pct_cells_expressing','min_donor_log_cpm','total_cells'},
    'T1D_DEG_IN': {'adjusted_p_value','log2_fold_change','p_value','se_of_log2_fold_change'},
    'GENE_ACTIVITY_SCORE_IN': {prefix+'ocr_gene_activity_score_'+summary
        for prefix in ('','aab_pos_','non_diabetic_','type_1_diabetes_','type_2_diabetes_')
        for summary in ('mean','median')},
    'SIGNAL_COLOC_WITH': {'nsnp','pp_h0_abf','pp_h1_abf','pp_h2_abf','pp_h3_abf','pp_h4_abf'},
    'MARKER_GENE_OF': set(),
}


def _measurement_kinds(kinds, property_name):
    from .release_schema import REGISTRY
    return {kind for kind in kinds
            if property_name in MEASUREMENT_FIELDS.get(kind,set())
            and property_name in REGISTRY['relations'].get(kind,{}).get('properties',[])}


@dataclass
class View:
    full: set = field(default_factory=set)
    aggregate: set = field(default_factory=set)
    stats: set = field(default_factory=set)
    identities: set = field(default_factory=set)
    nodes: set = field(default_factory=set)
    collection: bool = False

    def merge(self, other):
        return View(*(getattr(self, name) | getattr(other, name)
                      for name in ('full','aggregate','stats','identities','nodes')),
                    collection=self.collection or other.collection)


def _split(tokens, separator=','):
    groups=[]; start=0; depth=0
    for i,t in enumerate(tokens):
        if t.value in {'(','[','{'}:depth+=1
        elif t.value in {')',']','}'}:depth-=1
        elif depth==0 and t.value==separator:
            groups.append(tokens[start:i]);start=i+1
    return groups+[tokens[start:]]


def _strip_alias(tokens):
    depth=0
    for i,t in enumerate(tokens):
        if t.value in {'(','[','{'}:depth+=1
        elif t.value in {')',']','}'}:depth-=1
        elif depth==0 and t.kind=='WORD' and t.value.upper()=='AS':
            if i+2==len(tokens) and tokens[i+1].kind in {'WORD','IDENT'}:
                return tokens[:i],tokens[i+1].value
            return tokens,None
    return tokens,None


def _expr(tokens, symbols, relations, paths):
    if not tokens:return View()
    if tokens[0].kind=='WORD' and tokens[0].value.upper()=='DISTINCT':tokens=tokens[1:]
    if not tokens:return View()
    pieces=_split(tokens,'+')
    if len(pieces)>1:
        value=View()
        for piece in pieces:
            part=_expr(piece,symbols,relations,paths)
            if part.aggregate or part.stats:return View()
            value=value.merge(part)
        return value
    if len(tokens)==1 and tokens[0].kind in {'WORD','IDENT'}:
        return symbols.get(tokens[0].value,View())
    if len(tokens)==3 and tokens[1].value=='.' and tokens[0].value in symbols:
        base=symbols[tokens[0].value]
        return View(stats=_measurement_kinds(base.full,tokens[2].value),
                    identities=set(base.nodes) if tokens[2].value in {'id','name'} else set())
    if tokens[0].value=='{' and tokens[-1].value=='}':
        value=View()
        for item in _split(tokens[1:-1]):
            if len(item)<3 or item[1].value!=':':return View()
            value=value.merge(_expr(item[2:],symbols,relations,paths))
        value.collection=True
        return value
    if tokens[0].value=='[' and tokens[-1].value==']':
        value=View()
        for item in _split(tokens[1:-1]):value=value.merge(_expr(item,symbols,relations,paths))
        value.collection=True
        return value
    if len(tokens)>=4 and tokens[1].value=='(' and tokens[-1].value==')':
        # Closing parenthesis must end this function, not head(collect(r))[0].
        depth=0
        for i,t in enumerate(tokens[1:],1):
            if t.value=='(':depth+=1
            elif t.value==')':depth-=1
            if depth==0 and i!=len(tokens)-1:return View()
        function=tokens[0].value.lower()
        inner=tokens[2:-1]
        if inner and inner[0].value.upper()=='DISTINCT':inner=inner[1:]
        value=_expr(inner,symbols,relations,paths)
        if function=='collect':
            return View(value.full,value.aggregate,value.stats,value.identities,value.nodes,True)
        if function=='relationships' and len(inner)==1 and inner[0].value in paths:
            return View(full=set(paths[inner[0].value].full),collection=True)
        if function=='nodes' and len(inner)==1 and inner[0].value in paths:
            nodes=set(paths[inner[0].value].nodes)
            return View(nodes=nodes,identities=nodes,collection=True)
        if function=='count':
            if value.collection or value.aggregate:return View()
            wildcard = len(inner)==1 and inner[0].value=='*'
            if wildcard and any(v.collection or v.aggregate for v in symbols.values()):return View()
            kinds=value.full|value.stats
            if value.nodes or value.identities or (len(inner)==1 and inner[0].value=='*'):
                kinds|=set(relations)
            return View(aggregate=kinds)
        if function in {'sum','avg','min','max','stdev','stdevp'} and value.stats:
            return View(aggregate=value.stats)
        if function=='properties' and len(inner)==1:
            return View(stats={kind for kind in value.full if MEASUREMENT_FIELDS.get(kind)})
    return View()


def _branch_contract(tokens,step,parameters):
    from .graph import _pattern_bindings, _predicate_present
    from .release_schema import relationship_bindings
    requested=_requested(step)
    bindings, pattern_paths=_pattern_bindings(tokens,graph_release=step.get('graph_version'))
    relations=relationship_bindings(tokens)
    symbols={name:View(full=set(kinds)&(SUPPORTED | LINKAGE_RELATIONS)) for name,kinds in relations.items()}
    symbols.update({name:View(nodes={name},identities={name}) for name in bindings})
    # Match aliases that name a complete path, including anonymous relations.
    paths={}
    clause=''
    for i,t in enumerate(tokens[:-2]):
        if t.kind=='WORD' and t.value.upper() in {'MATCH','WHERE','WITH','RETURN'}:clause=t.value.upper()
        if (clause=='MATCH' and t.kind in {'WORD','IDENT'} and tokens[i+1].value=='='
                and tokens[i+2].value=='('):
            # Stop at the next MATCH/WHERE/projection clause or a top-level comma.
            end=i+2;depth=0
            while end<len(tokens):
                current=tokens[end]
                if depth==0 and end>i+2 and (current.value==',' or current.kind=='WORD' and current.value.upper() in {'MATCH','WHERE','WITH','RETURN'}):break
                if current.value in {'(','[','{'}:depth+=1
                elif current.value in {')',']','}'}:depth-=1
                end+=1
            _, typed=_pattern_bindings([tokens[i-1],*tokens[i+2:end]],graph_release=step.get('graph_version')) if i and tokens[i-1].value.upper()=='MATCH' else ({},[])
            kinds=set().union(*(k for _,_,k in typed)) if typed else set()
            nodes={variable for source,target,_ in typed for variable in (source,target)}
            paths[t.value]=View(full=kinds&(SUPPORTED | LINKAGE_RELATIONS),nodes=nodes,identities=nodes)
            symbols[t.value]=paths[t.value]
    fixed=set()
    for constraint in step.get('constraints') or []:
        owner=constraint.get('entity_type')
        if constraint.get('property') not in {'id','name'} or constraint.get('operator','=')!='=' or not owner:continue
        for variable, labels in bindings.items():
            if owner in labels and _predicate_present(tokens,constraint,parameters,{variable}):fixed.add(variable)
    output=View()
    i=0
    while i<len(tokens):
        token=tokens[i]
        if token.kind!='WORD' or token.value.upper() not in {'WITH','RETURN'}:
            i+=1;continue
        clause=token.value.upper();end=i+1;depth=0
        while end<len(tokens):
            t=tokens[end]
            if depth==0 and t.kind=='WORD' and t.value.upper() in {'MATCH','WHERE','WITH','RETURN','ORDER','LIMIT','SKIP'}:break
            if t.value in {'(','[','{'}:depth+=1
            elif t.value in {')',']','}'}:depth-=1
            end+=1
        values=View();new_symbols={}
        for item in _split(tokens[i+1:end]):
            expression,alias=_strip_alias(item)
            view=_expr(expression,symbols,requested,paths)
            values=values.merge(view)
            if alias:new_symbols[alias]=view
            elif len(expression)==1 and expression[0].value=='*':
                new_symbols.update(symbols)
                for value in symbols.values():values=values.merge(value)
            elif len(expression)==1 and expression[0].value in symbols:new_symbols[expression[0].value]=view
        if clause=='WITH':symbols=new_symbols
        else:output=values
        i=end
    identified=set();full=set()
    for source,target,kinds in pattern_paths:
        if source in fixed|output.identities and target in fixed|output.identities:
            identified|=kinds&output.stats
        # Neo4j relationship endpoints alone are bare node references. Require
        # endpoint objects so the adapter receives canonical IDs and names.
        if source in output.nodes and target in output.nodes:
            full|=kinds&output.full
    represented=full|output.aggregate|identified
    missing=requested-represented
    kind=('full_records' if requested and requested<=full else 'aggregate_result'
          if requested and requested<=output.aggregate else 'identified_statistics'
          if requested and requested<=identified else 'mixed_evidence'
          if requested and not missing else 'unverified_projection')
    return {'representation':kind,'record_membership_enumerated':bool(requested and requested<=full),
            'represented_relations':sorted(represented&requested),'missing_relations':sorted(missing)}


def projection_contract(tokens,step,parameters=None):
    requested=_requested(step)
    if not requested:return {'representation':'not_applicable','record_membership_enumerated':False,'represented_relations':[],'missing_relations':[]}
    # Match validate_cypher's single-statement terminator handling when this
    # helper is called directly by coverage on the executed query text.
    if tokens and tokens[-1].kind=='SYMBOL' and tokens[-1].value==';':
        tokens=tokens[:-1]
    if any(token.kind=='SYMBOL' and token.value==';' for token in tokens):
        return {'version':VERSION,'representation':'unverified_projection',
                'record_membership_enumerated':False,'represented_relations':[],
                'missing_relations':sorted(requested)}
    branches=[];branch=[]
    for token in tokens:
        if token.kind=='WORD' and token.value.upper()=='UNION':branches.append(branch);branch=[]
        elif not branch and token.kind=='WORD' and token.value.upper()=='ALL':continue
        else:branch.append(token)
    branches.append(branch)
    values=[_branch_contract(branch,step,parameters or {}) for branch in branches]
    missing=set().union(*(set(v['missing_relations']) for v in values))
    # These checks explicitly promise signal/variant membership inspection.
    # A scalar count cannot provide the identifiers needed for that linkage.
    # Ordinary count questions outside this normalized investigation still work.
    if step.get('coloc_scope') and not all(v['record_membership_enumerated'] for v in values):
        missing.update(requested)
    kinds={v['representation'] for v in values}
    return {'version':VERSION,'representation':next(iter(kinds)) if len(kinds)==1 else 'mixed_evidence',
            'record_membership_enumerated':all(v['record_membership_enumerated'] for v in values),
            'represented_relations':sorted(requested-missing),'missing_relations':sorted(missing)}


def validation_errors(tokens,step,parameters):
    return ['scientific_projection_missing:'+kind+':return_the_relationship_and_endpoints_or_an_identified_statistic_or_count'
            for kind in projection_contract(tokens,step,parameters)['missing_relations']]


DIGEST=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
