"""Preserve canonical predicates and avoid unnecessary serial graph checks."""
from copy import deepcopy
import json
import re

VERSION = 'dependency-scope-v1'
OWNERS = {'PART_OF_QTL_SIGNAL': {'Gene', 'variants'},
          'PART_OF_GWAS_SIGNAL': {'variants', 'disease'},
          'SIGNAL_COLOC_WITH': {'Gene', 'disease'},
          'HAS_SAMPLE': {'donor', 'Sample_node', 'anatomical_structure', 'disease'},
          'HAS_DONOR': {'donor', 'disease'}}


def normalize(plan):
    result=deepcopy(plan)
    question=result.get('original_question') or result.get('interpreted_question','')
    intersection=bool(re.search(r'\b(?:intersection|intersect|overlap|only those|satisfy both|shared variants)\b',question,re.I))
    by_id={}
    for step in result.get('steps',[]):
        relations=set(step.get('relation_types',[]));constraints=step.setdefault('constraints',[])
        identities={c.get('entity_type') for c in constraints if c.get('property') in {'id','name'}
                    and c.get('operator','=') in {'=','IN'} and c.get('value')}
        known=(relations=={'PART_OF_GWAS_SIGNAL'} and {'variants','disease'}<=identities
               or relations=={'SIGNAL_COLOC_WITH'} and {'Gene','disease'}<=identities
               or relations=={'PART_OF_QTL_SIGNAL'} and bool(identities & {'Gene','variants'}))
        if known and step.get('depends_on') and not intersection and step.get('evidence_combination','independent')=='independent':
            step['dependency_scope']={'version':VERSION,'removed_unnecessary_dependencies':list(step['depends_on'])}
            step['depends_on']=[]
        owners=set().union(*(OWNERS.get(r,set()) for r in relations))
        for dependency in step.get('depends_on',[]):
            parent=by_id.get(dependency,{})
            for predicate in parent.get('constraints',[]):
                # Identity sets are bound by the dependency itself. Clinical,
                # source and assay predicates keep their exact owner and value.
                if predicate.get('property') in {'id','name'} or predicate.get('entity_type') not in owners:
                    continue
                matches=[c for c in constraints if c.get('entity_type')==predicate.get('entity_type')
                         and c.get('property')==predicate.get('property')]
                key=lambda c:json.dumps({k:c.get(k) for k in ('property','entity_type','relationship_type','operator','value')},sort_keys=True)
                if matches and not any(key(c)==key(predicate) for c in matches):
                    raise ValueError('changed_dependency_scope:'+step['id']+':'+predicate['property'])
                if not matches:constraints.append(deepcopy(predicate))
        by_id[step['id']]=step
    result['dependency_scope_version']=VERSION
    return result
