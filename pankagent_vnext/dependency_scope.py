"""Preserve canonical predicates and avoid unnecessary serial graph checks."""
from copy import deepcopy
import json
import re

VERSION = 'dependency-scope-v2'
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
        if relations == {'PART_OF_GWAS_SIGNAL'} and not intersection and len(step.get('depends_on', [])) > 1:
            # Independent checks must not become an accidental intersection of
            # QTL variants and coloc leads. Prefer the gene-scoped coloc input.
            choices = [d for d in step['depends_on'] if by_id.get(d, {}).get('relation_types') == ['SIGNAL_COLOC_WITH']]
            if len(choices) == 1:
                step['dependency_scope'] = {'version':VERSION, 'selected_variant_input':choices[0],
                    'removed_unnecessary_dependencies':[d for d in step['depends_on'] if d != choices[0]]}
                step['depends_on'] = choices
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


def compile_inputs(plan):
    """Represent a gene-scoped GWAS check through an existing variant source.

    No extra graph check, relation or biological identity is invented. Identity
    predicates move only to an existing independently requested matching check.
    """
    result = normalize(plan)
    steps = result.get('steps', [])
    def genes(step):
        return {str(c.get('value')) for c in step.get('constraints', [])
                if c.get('entity_type') == 'Gene' and c.get('property') in {'id','name','hgnc_symbol'}
                and c.get('operator','=') == '='}
    all_genes = set().union(*(genes(s) for s in steps)) if steps else set()
    for step in steps:
        if step.get('relation_types') != ['PART_OF_GWAS_SIGNAL'] or step.get('depends_on'):
            continue
        if any(c.get('entity_type') == 'variants' and c.get('property') in {'id','name'} for c in step.get('constraints',[])):
            continue
        target = genes(step) or all_genes
        if len(target) != 1:
            continue
        if any(c.get('entity_type') == 'Gene' and c.get('property') not in {'id','name','hgnc_symbol'} for c in step.get('constraints',[])):
            continue
        sources = []
        for relation in ('SIGNAL_COLOC_WITH','PART_OF_QTL_SIGNAL'):
            sources = [s for s in steps if s['id'] != step['id'] and s.get('relation_types') == [relation]
                       and genes(s) == target and not s.get('depends_on')]
            if len(sources) == 1: break
        if len(sources) != 1: continue
        source = sources[0]
        step['depends_on'] = [source['id']]
        step['constraints'] = [c for c in step.get('constraints',[]) if c.get('entity_type') != 'Gene']
        step['dependency_scope'] = {'version':VERSION, 'necessary_variant_input':source['id'],
                                   'inherited_gene_identity':sorted(target)}
    # Stable topological ordering; reject cycles instead of silently dropping a dependency.
    ordered, pending, seen = [], list(steps), set()
    while pending:
        ready = next((s for s in pending if set(s.get('depends_on',[])) <= seen), None)
        if ready is None: raise ValueError('invalid_plan_dependencies')
        ordered.append(ready);seen.add(ready['id']);pending.remove(ready)
    result['steps'] = ordered
    return result
