"""Deterministic display groups and evidence coverage; flat checks stay public."""
from copy import deepcopy
import re
GROUPS = (
 ('cell_context','Where the gene is detected and how it differs',{'GENE_DETECTED_IN','GENE_ENRICHED_IN','MARKER_GENE_OF','T1D_DEG_IN','GENE_ACTIVITY_SCORE_IN'}),
 ('genetic_support','Disease and genetic evidence',{'EFFECTOR_GENE_OF','PART_OF_QTL_SIGNAL','PART_OF_GWAS_SIGNAL','SIGNAL_COLOC_WITH'}),
 ('function_context','Functions, pathways and interaction partners',{'ASSOCIATED_WITH_GO','FUNCTION_ANNOTATION','FGSEA_ENRICHED_IN','PHYSICAL_INTERACTION','GENETIC_INTERACTION'}),
)

def group_plan(plan):
    plan = deepcopy(plan)
    if len(plan.get('steps',[])) > 12:
        return {**plan,'steps':[],'clarification':'This request needs more than twelve evidence checks. Please narrow its scope.'}
    groups = {}
    for step in plan.get('steps',[]):
        kinds=set(step.get('relation_types',[]))
        group=next(((key,title) for key,title,members in GROUPS if kinds & members),('cohort','Find donors and their recorded samples'))
        # A cohort and molecular mixed question still has at most three groups.
        if group[0] not in groups and len(groups)==3: group=(next(reversed(groups)),groups[next(reversed(groups))]['title'])
        groups.setdefault(group[0],{'id':group[0],'title':group[1],'step_ids':[]})['step_ids'].append(step['id'])
        step['evidence_categories']=sorted(kinds)
    plan['display_groups']=list(groups.values())
    plan['requested_evidence_categories']=sorted(required_categories(plan.get('original_question') or plan.get('interpreted_question','')) | {k for s in plan.get('steps',[]) for k in s.get('relation_types',[])})
    plan['planning_contract']='grouped-investigations-v2'
    return plan


def coverage(plan, previous):
    return [{'step_id':step['id'],'categories':step.get('relation_types',[]),
             'status':previous.get(step['id'],{}).get('status','awaiting_confirmation')}
            for step in plan.get('steps',[])]


GENERIC_GENE_CATEGORIES = {
 'GENE_DETECTED_IN','GENE_ENRICHED_IN','MARKER_GENE_OF','T1D_DEG_IN',
 'EFFECTOR_GENE_OF','PART_OF_QTL_SIGNAL','SIGNAL_COLOC_WITH','ASSOCIATED_WITH_GO',
 'FUNCTION_ANNOTATION','FGSEA_ENRICHED_IN','PHYSICAL_INTERACTION','GENETIC_INTERACTION',
}

def required_categories(question):
    """Explicit category language only; revisions remain guarded by their parent.

    Generic comprehensive gene profiles have a versioned registered scope.
    No entity matching or additional inference occurs here.
    """
    if re.search(r'\b(?:revise|instead|remove|exclude|switch|only)\b',question,re.I): return set()
    if re.search(r'\bcomprehensive\b',question,re.I) and re.search(r'\b(?:gene|profile|overview|insights)\b',question,re.I):
        return set(GENERIC_GENE_CATEGORIES)
    expressions = {
      'GENE_DETECTED_IN':r'\b(?:detected|detection)\b',
      'GENE_ENRICHED_IN':r'\b(?:gene enrichment|detected or enriched)\b',
      'MARKER_GENE_OF':r'\bmarker gene',
      'T1D_DEG_IN':r'\bT1D differential expression\b',
      'EFFECTOR_GENE_OF':r'\beffector[ -]gene\b',
      'PART_OF_QTL_SIGNAL':r'\bQTL evidence\b',
      'SIGNAL_COLOC_WITH':r'\b(?:colocalization|coloc) evidence\b',
      'ASSOCIATED_WITH_GO':r'\bGO annotations?\b',
      'FUNCTION_ANNOTATION':r'\b(?:pathway annotations?|pathways have function annotations)\b',
      'FGSEA_ENRICHED_IN':r'\bfGSEA\b',
      'PHYSICAL_INTERACTION':r'\bphysical interaction',
      'GENETIC_INTERACTION':r'\bgenetic interaction',
    }
    return {key for key,pattern in expressions.items() if re.search(pattern,question,re.I)}


def category_issue(question, plan):
    required=required_categories(question)
    present={kind for step in plan.get('steps',[]) for kind in step.get('relation_types',[])}
    missing=required-present
    if missing and not plan.get('clarification'):
        return 'missing_requested_categories:'+','.join(sorted(missing))
    return None


PROFILE_CHECKS = (
 ('GENE_DETECTED_IN','Find all recorded detection evidence for {gene}.'),
 ('GENE_ENRICHED_IN','Find all recorded gene enrichment evidence for {gene}.'),
 ('MARKER_GENE_OF','Find all recorded marker-gene annotations for {gene}.'),
 ('T1D_DEG_IN','Find all recorded T1D differential-expression evidence for {gene}.'),
 ('EFFECTOR_GENE_OF','Find all recorded effector-gene prioritization evidence for {gene}.'),
 ('PART_OF_QTL_SIGNAL','Find all recorded molecular QTL evidence for {gene} across tissues.'),
 ('SIGNAL_COLOC_WITH','Find all recorded colocalization evidence for {gene} across diseases.'),
 ('ASSOCIATED_WITH_GO','Find all recorded GO annotations for {gene}.'),
 ('FUNCTION_ANNOTATION','Find all recorded pathway annotations for {gene} across supported collections.'),
 ('FGSEA_ENRICHED_IN','Find all recorded pathway enrichment evidence for the pathways annotated to {gene} from the preceding pathway-annotation check.'),
 ('PHYSICAL_INTERACTION','Find all recorded physical interaction partners of {gene} at either endpoint.'),
 ('GENETIC_INTERACTION','Find all recorded genetic interaction partners of {gene} at either endpoint.'),
)

def generic_profile_gene(question):
    # A full match prevents losing additional disease, tissue or revision scope.
    match=re.fullmatch(r'\s*(?:(?:give me|show|provide)\s+)?(?:a\s+)?comprehensive\s+(?:gene\s+)?(?:profile|overview|insights)\s+(?:of|for|on)\s+([A-Za-z][A-Za-z0-9_.-]{0,39}?)[.?!]?\s*',question,re.I)
    if match is None:
        match=re.fullmatch(r'\s*tell me about (?:the\s+)?gene\s+([A-Za-z][A-Za-z0-9_.-]{0,39}?)(?:\s+in\s+(?:T1D|type 1 diabetes))?[.?!]?\s*',question,re.I)
    return match.group(1) if match else None


def expand_registered_profile(question, gene):
    if generic_profile_gene(question) != gene: raise ValueError('profile_scope_mismatch')
    disease_scope=bool(re.search(r'\bin\s+(?:T1D|type 1 diabetes)[.?!]?\s*$',question,re.I))
    steps=[]
    for index,(kind,wording) in enumerate(PROFILE_CHECKS,1):
        pathway_check=kind=='FGSEA_ENRICHED_IN'
        steps.append({'id':'s'+str(index),'question':wording.format(gene=gene),
                      'relation_types':[kind],'depends_on':['s9'] if pathway_check else [],
                      'constraints':[] if pathway_check else [{'entity_type':'Gene','property':'name','operator':'=','value':gene}],
                      'complete':True,'evidence_combination':'independent'})
    if disease_scope:
        for step in steps:
            if step['relation_types'][0] in {'EFFECTOR_GENE_OF','SIGNAL_COLOC_WITH'}:
                step['constraints'].append({'entity_type':'disease','property':'name','operator':'=','value':'type 1 diabetes'})
                step['question']=step['question'].replace(' across diseases','')+' Restrict disease evidence to type 1 diabetes.'
    return {'interpreted_question':question,'steps':steps,'clarification':None,
            'profile_scope_source':'registered-gene-profile-v1'}
