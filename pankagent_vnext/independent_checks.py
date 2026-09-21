"""Split independent gene-evidence categories without dropping scoped predicates."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

VERSION='independent-gene-checks-v1'
DIGEST=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SIGNALS={'PART_OF_GWAS_SIGNAL','PART_OF_QTL_SIGNAL','SIGNAL_COLOC_WITH'}
CONTEXT={'PHYSICAL_INTERACTION','GENETIC_INTERACTION','FUNCTION_ANNOTATION','ASSOCIATED_WITH_GO'}
OWNERS={'PART_OF_GWAS_SIGNAL':{'variants','disease'},'PART_OF_QTL_SIGNAL':{'variants','Gene'},
        'SIGNAL_COLOC_WITH':{'Gene','disease'}, **{r:{'Gene'} for r in CONTEXT}}
TITLES={'PART_OF_GWAS_SIGNAL':'Check GWAS association evidence','PART_OF_QTL_SIGNAL':'Check molecular-QTL evidence',
        'SIGNAL_COLOC_WITH':'Check colocalization evidence','PHYSICAL_INTERACTION':'Check physical interactions',
        'GENETIC_INTERACTION':'Check genetic interactions','FUNCTION_ANNOTATION':'Check pathway annotations',
        'ASSOCIATED_WITH_GO':'Check GO annotations'}


def split(plan, question):
    result=deepcopy(plan)
    if re.search(r'\b(?:intersection|intersect|overlap|only those|satisfy both|shared variants)\b',question,re.I):
        return result
    original=result.get('steps',[]);identifiers={s['id'] for s in original};rewritten=[];mapping={}
    for step in original:
        relations=step.get('relation_types',[])
        if (len(relations)<2 or len(set(relations))!=len(relations)
                or not (set(relations)<=SIGNALS or set(relations)<=CONTEXT)
                or step.get('evidence_combination','independent')!='independent'
                or step.get('depends_on') or any(step['id'] in s.get('depends_on',[]) for s in original)
                or any(step.get(k) for k in ('ranking_contract','ranking_issue','semantic_issues','schema_bindings'))):
            rewritten.append(step);continue
        constraints=step.get('constraints',[])
        genes=[c for c in constraints if c.get('entity_type')=='Gene' and c.get('property') in {'id','name','hgnc_symbol'} and c.get('operator','=')=='=']
        if len(genes)!=1:
            rewritten.append(step);continue
        # Unknown ownership stays blocked by normal validation, never discarded.
        if any(not (c.get('entity_type') in {'Gene','variants','disease'}
                    and c.get('property') in {'id','name','hgnc_symbol'}
                    and c.get('operator','=')=='=' or c.get('relationship_type') in relations)
               for c in constraints):
            rewritten.append(step);continue
        if set(relations)<=CONTEXT and any(c.get('entity_type') not in {None,'Gene'} for c in constraints):
            rewritten.append(step);continue
        children=[]
        for index,relation in enumerate(relations,1):
            child=deepcopy(step);identifier=step['id']+'_check_'+str(index)
            if identifier in identifiers:raise ValueError('independent_check_id_collision')
            identifiers.add(identifier)
            selected=[c for c in constraints if c.get('relationship_type')==relation
                      or not c.get('relationship_type') and c.get('entity_type') in OWNERS[relation]]
            # Retain the gene until compile_inputs binds a necessary variant source.
            if relation=='PART_OF_GWAS_SIGNAL' and not any(c.get('entity_type')=='variants' for c in selected):
                selected+=genes
            child.update(id=identifier,relation_types=[relation],constraints=deepcopy(selected),
                question='Retrieve recorded '+relation+' evidence under these exact constraints: '+json.dumps(selected,sort_keys=True),
                title=TITLES[relation],evidence_combination='independent',
                independent_check_origin={'version':VERSION,'step_id':step['id'],'question':step.get('question')})
            child.pop('evidence_id',None)
            children.append(child)
        # Keep the public twelve-check boundary; oversized requests fail normally.
        mapping[step['id']]=[s['id'] for s in children];rewritten.extend(children)
    if len(rewritten)>12:raise ValueError('plan_too_large_after_independent_checks')
    result['steps']=rewritten
    if mapping:
        result['independent_check_compilation']={'version':VERSION,'source_steps':mapping}
        for group in result.get('display_groups',[]):
            key='step_ids' if 'step_ids' in group else 'steps'
            if isinstance(group.get(key),list):
                group[key]=[d for k in group[key] for d in mapping.get(k,[k])]
    return result
