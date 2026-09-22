"""Compare retrieved signal records without generating another graph join."""
from copy import deepcopy
from .coloc_scope import RELEASE, RELATIONS, QTL_CONTEXT, _credible_set, _comparison_only, _verified_pair

VERSION = 'recorded-signal-comparison-v1'


def single_identity(step, kind):
    constraints = step.get('constraints', [])
    if len(constraints) != 1 or step.get('graph_version') != RELEASE:
        return None
    c = constraints[0]
    records = step.get('resolved_entities', [])
    if c.get('entity_type') != kind or c.get('property') != 'id' or c.get('operator', '=') != '=':
        return None
    matching = [r for r in records if r.get('constraint_index') == 0 and r.get('state') == 'resolved'
                and r.get('graph_version') == RELEASE and r.get('id') == c.get('value')
                and r.get('entity_type') == kind]
    return c['value'] if len(matching) == 1 else None


def compile_comparisons(plan, release):
    result = deepcopy(plan)
    if release != RELEASE:
        return result
    steps = result.get('steps', [])
    by_id = {s['id']:s for s in steps}
    removed = {}
    for step in steps:
        deps = step.get('depends_on', [])
        if (set(step.get('relation_types', [])) != RELATIONS or len(deps) != 3
                or len(set(deps)) != 3 or step.get('constraints') or step.get('complete') is not True
                or any(step.get(k) for k in ('ranking_contract','ranking_issue','semantic_issues','schema_bindings'))
                or any(step['id'] in s.get('depends_on', []) for s in steps)):
            continue
        parents = [by_id.get(d, {}) for d in deps]
        if any(len(p.get('relation_types', [])) != 1 or steps.index(p) >= steps.index(step) for p in parents):
            continue
        roles = {p['relation_types'][0]:p for p in parents}
        if set(roles) != RELATIONS:
            continue
        coloc, qtl, gwas = (roles[r] for r in ('SIGNAL_COLOC_WITH','PART_OF_QTL_SIGNAL','PART_OF_GWAS_SIGNAL'))
        pair = _verified_pair(coloc, ('Gene','disease'))
        if (not pair or single_identity(qtl, 'Gene') != pair[0] or qtl.get('depends_on')
                or single_identity(gwas, 'disease') != pair[1]
                or gwas.get('depends_on') not in ([qtl['id']], [coloc['id']])):
            continue
        question = step.get('question', '')
        for term in ('gwas_lead_vars','qtl_lead_vars'):
            question = question.replace(term, term[:4] + ' lead variants')
        if not _comparison_only(question, parents, {'lead','variant','variants','found','separate','records'}):
            continue
        removed[step['id']] = deps
        result.setdefault('record_comparison_operations', []).append({
            'id':step['id'], 'version':VERSION, 'depends_on':deps,
            'operation':'compare_recorded_signal_memberships', 'no_new_retrieval':True,
            'original_step':deepcopy(step)})
    if removed:
        result['steps'] = [s for s in steps if s['id'] not in removed]
        for group in result.get('display_groups', []):
            key = 'step_ids' if 'step_ids' in group else 'steps'
            if isinstance(group.get(key), list):
                group[key] = list(dict.fromkeys(d for k in group[key] for d in removed.get(k, [k])))
    return result


def membership_facts(steps, evidence_ids):
    """Exact release-specific IDs and QTL context; lack of a match stays unknown."""
    records = [(eid,e) for step,eid in zip(steps,evidence_ids)
               if step.get('graph_version') == RELEASE and step.get('status') in {'complete','partial'}
               for e in step.get('edges', [])]
    for eid,edge in records:
        if edge.get('type') != 'SIGNAL_COLOC_WITH':
            continue
        props = edge.get('properties') or {}
        gene,disease = edge.get('start_id'),edge.get('end_id')
        signal = _credible_set(props.get('gwas_signal_id'))
        context = QTL_CONTEXT.get(props.get('coloc_dataset'))
        gwas = [(ref,e) for ref,e in records if signal and e.get('type') == 'PART_OF_GWAS_SIGNAL'
                and e.get('end_id') == disease and (e.get('properties') or {}).get('credible_set_id') == signal]
        qtl = [(ref,e) for ref,e in records if context and props.get('qtl_signal_id') and e.get('type') == 'PART_OF_QTL_SIGNAL'
               and e.get('end_id') == gene and (e.get('properties') or {}).get('credible_set') == props['qtl_signal_id']
               and (e.get('properties') or {}).get('data_source') == context[0]
               and (e.get('properties') or {}).get('tissue_id') == context[1]]
        if not gwas and not qtl:
            continue
        yield {'evidence_id':eid, 'support':sorted({ref for ref,_ in gwas+qtl}),
               'gwas_signal_id':signal,'qtl_signal_id':props.get('qtl_signal_id'),
               'gwas_members':sorted({str(e['start_id']) for _,e in gwas}),
               'qtl_members':sorted({str(e['start_id']) for _,e in qtl})}
