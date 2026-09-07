"""Freeze 40 source-backed UX tasks; refuses to overwrite an existing manifest."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

EXAMPLES = [
'Is CFTR specifically enriched in ductal cells?',
'Is INS beta-cell restricted in the PanKgraph expression data?',
'Is HLA-DRA mainly detected in immune cells, or does it also appear in pancreatic epithelial/endocrine compartments?',
'Is PLEKHM1 a T1D effector gene or key marker gene in pancreatic cell types?',
'Does ADCY3 connect to signaling pathways that may be relevant to endocrine or immune regulation?',
'For HLA-DRA, does PanKgraph support an immune antigen-presentation use case more strongly than a beta-cell intrinsic use case?',
'What pathways and interaction partners connect HLA-DRA to antigen presentation in T1D?',
'Does the T1D GWAS signal near PLEKHM1 colocalize with a QTL signal for PLEKHM1?',
'For CFTR, does the T1D GWAS signal colocalize with a pancreas splicing QTL?',
'For ADCY3, does the T1D-associated GWAS signal rs13393590 colocalize with ADCY3 molecular QTL evidence?']
R_GOALS = ['Restrict GO to biological processes', 'Disable literature', 'Enable literature',
'Keep T2D distinct from T1D', 'Compare ND and T1D beta-cell expression',
'Add human genetics and respond to a repeated correction', 'Include non-beta populations and preserve completeness',
'Add regulatory and physical interactions', 'Include immune and stromal cells', 'Narrow dopamine receptors to DRD3',
'Resolve transcription-factor naming and a missing AP1 entity', 'Exclude beta-cell material after requesting immune evidence']
E_GOALS = [(0,'Inspect every visible relationship and its full evidence'), (9,'Distinguish a lead variant from a credible set'),
(0,'Interpret enrichment measurements and ND-only scope'), (7,'Inspect and export the colocalization table'),
(6,'Trace a biological claim to its cited publication'), (9,'Open an empirical association plot'),
(9,'Download supplementary association data and verify its identity'), (8,'Trace a splicing-QTL result to its source file')]
HOLDOUT = {'C06','N09','N10','R03','R07','R12','E04','E08','X03','X04'}
CHECKS = ['Task goal reached', 'Required entities and filters preserved', 'Conclusion matches evidence',
'Important terminology explained', 'Limits and uncertainty stated', 'Source inspectable']


def build(history):
    tasks=[]
    def add(id,family,question,goal,**extras):
        tasks.append({'id':id,'family':family,'question':question,'goal':goal,'holdout':id in HOLDOUT,
            'source':{'kind':'official_example','url':'https://pankgraph.org/','build':'main.2cd17a32.js'},
            'actor_identity':'audit_replay','checklist':CHECKS,'prerequisites':['official and isolated sites reachable'],
            'official':{'status':'pending'},'vnext':{'status':'pending'},**extras})
    conventional=[('qtl_by_gene',{'gene_id':'ENSG00000001626'},'Which SNP serves as the lead QTL for CFTR?'),
      ('qtl_by_variant_gene',{'gene_id':'ENSG00000138031','variant_id':'rs13393590'},EXAMPLES[9]),
      ('qtl_by_variant',{'variant_id':'rs13393590'},EXAMPLES[9]),
      ('gwas_by_variant',{'variant_id':'rs13393590'},EXAMPLES[9]),
      ('coloc_by_gene',{'gene_id':'ENSG00000138031'},EXAMPLES[9]),
      ('expression_by_gene',{'gene_id':'ENSG00000254647'},EXAMPLES[1])]
    for i,(template,params,q) in enumerate(conventional,1):
        add(f'C{i:02}','conventional',q,'Complete conventional '+template+' search',template_id=template,parameters=params,
            intervention='Use the conventional template with entities from the official example')
    for i,q in enumerate(EXAMPLES,1):add(f'N{i:02}','natural_language',q,'Obtain and understand the biological answer')
    for i,line in enumerate(history['selected_revision_lines'],1):
        selected=next(row for row in history['records'] if row['line']==line)
        revisions=[row for row in history['records'] if row['session_hash']==selected['session_hash'] and 'revis' in row['event']]
        q=selected['data'].get('question')
        if not q:raise ValueError('Missing recorded question')
        add(f'R{i:02}','revision',q,R_GOALS[i-1],
            source={'kind':'production_history','sha256':history['source_sha256'],'line':line,'session_hash':selected['session_hash'],'actor_identity':'unknown_production_session'},
            revisions=[{'instruction':row['data'].get('user_prompt',row['data'].get('prompt')),'source_line':row['line']} for row in revisions])
    for i,(index,goal) in enumerate(E_GOALS,1):
        add(f'E{i:02}','evidence_inspection',EXAMPLES[index],goal,parent_task=f'N{index+1:02}',
            intervention='Inspect the saved answer; do not count this as an independent inference sample')
    add('X01','recovery',EXAMPLES[0],'Refresh and resume a saved investigation',parent_task='N01',intervention='Reload and compare state and model-call count')
    add('X02','recovery',EXAMPLES[6],'Cancel an investigation and prevent further work',intervention='Cancel during active work on isolated service; use available official control only')
    empty=next(row for row in history['empty_candidates'] if row['line']==3126)
    add('X03','recovery',empty['data']['question'],'Interpret an empty GWAS/QTL result without asserting biological absence',
        source={'kind':'production_history','sha256':history['source_sha256'],'line':3126,'actor_identity':'unknown_production_session'})
    add('X04','recovery',EXAMPLES[0],'Preserve the graph answer when literature is unavailable',parent_task='N01',
        intervention='Fault injection only in a disposable local service; official failure not induced')
    assert len(tasks)==40 and len({t['id'] for t in tasks})==40
    assert all(r['instruction'] for t in tasks for r in t.get('revisions',[]))
    return {'version':1,'created_at':datetime.now(timezone.utc).isoformat(),'study_type':'agent_run_usability_audit',
      'audience':'general biology; unfamiliar with fine-mapping, QTL, colocalization and perfusion',
      'official':'https://pankgraph.org/','history_sha256':history['source_sha256'],
      'holdout_policy':'Ten tasks are excluded from prompt tuning and improvement selection; preserve frozen baseline captures for later validation.',
      'timing_policy':'UI timings and API timings are separate; unknown official cache state is never called cold.',
      'tasks':tasks}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--history',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    result=build(json.loads(a.history.read_text()));raw=json.dumps(result,ensure_ascii=False,indent=2).encode()
    with os.fdopen(os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as f:f.write(raw)
    print(json.dumps({'tasks':len(result['tasks']),'holdouts':sum(t['holdout'] for t in result['tasks']),'manifest_sha256':hashlib.sha256(raw).hexdigest()}))
