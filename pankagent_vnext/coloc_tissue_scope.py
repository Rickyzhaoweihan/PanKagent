"""Bind explicit tissue restrictions through the reviewed release dataset map."""
from copy import deepcopy
import hashlib
from pathlib import Path
import re
from .coloc_scope import RELEASE, QTL_CONTEXT, DIGEST as MAPPING_DIGEST

VERSION='coloc-tissue-scope-v1'
DIGEST=hashlib.sha256(Path(__file__).read_bytes()+MAPPING_DIGEST.encode()).hexdigest()
# Names and IDs are the audited release's QTL categorical values.
TISSUES={'pancreas':('Pancreas','UBERON_0001264'), 'pancreatic':('Pancreas','UBERON_0001264'),
         'islet':('Islet','UBERON_0000006'), 'islets':('Islet','UBERON_0000006')}
SCOPE=re.compile(r'\b(?:in|from|within)\s+(?:the\s+)?(pancreatic|pancreas|islets?)(?:\s+(or|and)\s+(pancreatic|pancreas|islets?))?\s+(?:tissues?|tissue evidence)\b',re.I)


def compile_scope(question, grounding, plan):
    result=deepcopy(plan)
    steps=[s for s in result.get('steps',[]) if 'SIGNAL_COLOC_WITH' in s.get('relation_types',[])]
    matches=list(SCOPE.finditer(question))
    if not steps or not matches:return result,None
    if len(matches)!=1 or matches[0][2] and matches[0][2].lower()=='and':
        return result,'ambiguous_coloc_tissue_scope:independent_or_vs_joint_tissue_evidence'
    match=matches[0]
    wanted={TISSUES[word.lower()] for word in (match[1],match[3]) if word}
    categories=(grounding or {}).get('schema',{}).get('categories',{})
    if ((grounding or {}).get('identity',{}).get('graph_release')!=RELEASE
            or not {name for name,_ in wanted}<=set(categories.get('PART_OF_QTL_SIGNAL.tissue_name',[]))
            or not {identifier for _,identifier in wanted}<=set(categories.get('PART_OF_QTL_SIGNAL.tissue_id',[]))):
        return result,'unverified_coloc_tissue_mapping'
    genes={str(c.get('value')) for s in result.get('steps',[]) for c in s.get('constraints',[])
           if c.get('entity_type')=='Gene' and c.get('property') in {'id','name'}}
    if len(genes)!=1:return result,'ambiguous_coloc_tissue_scope:multiple_gene_scopes'
    tissues={identifier for _,identifier in wanted}
    datasets=sorted(key for key,(_,tissue) in QTL_CONTEXT.items() if tissue in tissues)
    if not datasets:return result,'unverified_coloc_tissue_mapping'
    for step in steps:
        if step.get('relation_types')!=['SIGNAL_COLOC_WITH']:
            return result,'uncompiled_coloc_tissue_scope'
        current=[c for c in step.get('constraints',[]) if c.get('property')=='coloc_dataset']
        if current:
            if any(c.get('operator','=') not in {'=','IN'} or c.get('relationship_type') not in {None,'SIGNAL_COLOC_WITH'} for c in current):
                return result,'unverified_coloc_dataset_filter'
            for c in current:
                values=c['value'] if isinstance(c.get('value'),list) else [c.get('value')]
                if not set(values)<=set(datasets):return result,'conflicting_coloc_tissue_filter'
        else:
            step.setdefault('constraints',[]).append({'entity_type':None,'owner_kind':'relationship',
                'relationship_type':'SIGNAL_COLOC_WITH','property':'coloc_dataset','operator':'IN','value':datasets})
        step['coloc_tissue_scope']={'version':VERSION,'mapping_sha256':MAPPING_DIGEST,
            'graph_release':RELEASE,'tissue_ids':sorted(tissues),'datasets':datasets,
            'source':'reviewed colocalization dataset to QTL source/tissue registry'}
    return result,None
