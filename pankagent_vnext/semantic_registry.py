"""Release-scoped terminology and assay capabilities; never executable Cypher."""
from copy import deepcopy
from difflib import get_close_matches
import hashlib
import json
import re

VERSION = 'pankgraph-semantics-v1'
RELEASE = 'PanKgraph_08_04'
SOURCE = 'https://hpap.pmacs.upenn.edu/analysis'
STAGES = {
    '1': 'Stage 1: two or more autoantibodies, normal glucose metabolism level',
    '3': 'Stage 3: one or more autoantibodies and diagnostic hyperglycemia or T1D diagnosis',
}
# Property ownership is verified against this release, not inferred from global keys.
PROPERTIES = {
 'donor': 'id data_source data_version data_source_url diabetes_type hla_typing c_peptide_ng_ml aab_state other_disease_records gender hla_status age hba1c_percentage center_donor_id pancdb_id other_therapy creation_date hospital_stay_hours bmi pankbase_id t1d_stage race rrid diabetes_duration predicted_genetic_ancestry family_history_of_diabetes sex_at_birth donation_type cause_of_death derived_diabetes_status'.split(),
 'Sample_node': 'id data_source data_version data_modality anatomical_structure note contact'.split(),
 'data_modality': 'id data_source data_version'.split(),
 'disease': 'id name description data_source data_version synonyms data_source_url'.split(),
}
ALIASES = {'scrnaseq':'scRNA-seq', 'singlecellrnaseq':'scRNA-seq', 'snmultiomics':'snMultiomics',
 'multiome':'snMultiomics', 'multiomics':'snMultiomics', 'snrnaseq':'snRNA-seq', 'scatacseq':'scATAC-seq',
 'singlecellatacseq':'scATAC-seq', 'snatacseq':'snATAC-seq', 'citeseqprotein':'CITE-seq Protein'}
CAPABILITIES = {'scRNA-seq':['RNA'], 'scATAC-seq':['ATAC'], 'snMultiomics':['RNA','ATAC'], 'CITE-seq Protein':['protein']}
DIGEST = hashlib.sha256(json.dumps([VERSION, RELEASE, PROPERTIES, ALIASES, CAPABILITIES, SOURCE],sort_keys=True).encode()).hexdigest()


def donor_intent(step):
    return bool(re.search(r'\bdonors?\b|\bHPAP\b',step.get('question',''),re.I) or any(c.get('entity_type')=='donor' for c in step.get('constraints',[])))


def planner_guidance(question):
    if not re.search(r'\bdonors?\b|\bHPAP\b|multiom|scRNA|ATAC', question,re.I): return ''
    return ('\nDonor/sample terminology: stage labels and assay names are resolved by the application against current graph values. '
            'Keep requested stages in ordinary biological wording. donor.t1d_stage belongs only to donor. '
            'Sample_node.data_modality is the assay; tissue uses anatomical_structure -HAS_SAMPLE-> Sample_node. '
            'RNA can be a documented component of multiome; retain exact-only or paired requirements. '
            'Always connect each sample directly to its donor; do not add samples to a donor-only lookup.')


def resolve(step, vocabulary, release):
    out=deepcopy(step)
    if not donor_intent(out): return out
    q=out['question']; lower=q.lower(); constraints=out.setdefault('constraints',[])
    issues=[]; matches=[]; groups=[]
    if release!=RELEASE:
        out['semantic_issues']=['The terminology registry does not match this graph release.'];return out
    def bind(prop, owner, value, operator='=', kind='alias', requested=None):
        # Replace only the same semantic field, preserving unrelated constraints.
        nonlocal constraints
        constraints=[c for c in constraints if not (c.get('property')==prop and c.get('entity_type') in (None,owner))]
        c={'property':prop,'entity_type':owner,'operator':operator,'value':json.dumps(value) if isinstance(value,list) else value}
        constraints.append(c)
        matches.append({'requested':requested or prop,'canonical_binding':deepcopy(c),'match_kind':kind,'registry_version':VERSION,'source':SOURCE if kind=='capability' else 'verified graph categorical values'})
    stage=re.search(r'\bstage\s*[-:]?\s*(\d+|iii|ii|i)\b',q,re.I)
    if not stage:
        c=next((c for c in constraints if c.get('property')=='t1d_stage'),None)
        if c:stage=re.search(r'(\d+|iii|ii|i)',str(c['value']),re.I)
    if stage:
        number={'i':'1','ii':'2','iii':'3'}.get(stage.group(1).lower(),stage.group(1))
        candidates=[v for v in vocabulary.get('stages',[]) if re.match(r'^Stage '+re.escape(number)+r':',v)]
        if len(candidates)==1:bind('t1d_stage','donor',candidates[0],requested=stage.group(0))
        else:issues.append('Requested T1D stage cannot be uniquely matched to a recorded stage. No substitute stage was selected.')
    if re.search(r'\bHPAP\b',q,re.I):
        if 'HPAP' in vocabulary.get('sources',[]):bind('data_source','donor','HPAP',kind='exact',requested='HPAP')
        else:issues.append('HPAP donor source is not verified in this release.')
    disease=re.search(r'\bT([12])D\b|\btype\s*([12])\s*diabetes\b',q,re.I)
    if disease:
        number=disease.group(1) or disease.group(2)
        # Explicit cohort identity; never derive stage from diabetes status.
        constraints=[c for c in constraints if not (c.get('entity_type')=='disease' and c.get('property') in ('name','id'))]
        bind('id','disease','MONDO_0005147' if number=='1' else 'MONDO_0005148',requested=disease.group(0))
    tissues=[t for t in vocabulary.get('tissues',[]) if isinstance(t.get('name'),str) and re.search(r'(?<!\w)'+re.escape(t['name'])+r'(?!\w)',q,re.I)]
    if len(tissues)==1:
        constraints=[c for c in constraints if not (c.get('entity_type')=='anatomical_structure' and c.get('property') in ('id','name'))]
        bind('id','anatomical_structure',tissues[0]['id'],kind='exact',requested=tissues[0]['name'])
    elif len(tissues)>1:
        issues.append('Multiple sample tissues were named. Separate the tissue checks so each sample remains attached to its intended tissue.')
    old_assay=[c for c in constraints if c.get('property')=='data_modality' or c.get('entity_type')=='data_modality']
    assay_text=' '.join([q]+[str(c.get('value','')) for c in old_assay])
    rna=bool(re.search(r'(?<![a-z])(?:sc|sn)?RNA[\s-]*(?:seq)?|transcriptom',assay_text,re.I))
    atac=bool(re.search(r'ATAC|chromatin accessibility',assay_text,re.I))
    paired=bool(re.search(r'multiom|paired|joint',q,re.I))
    exact=bool(re.search(r'\bstandalone\b|\bexact(?:ly)?\b.{0,20}(?:RNA|ATAC)|(?:RNA[\s-]*seq|ATAC[\s-]*seq)\s+only\b|\bonly\s+(?:sc|sn)?(?:RNA|ATAC)|exclude\s+multiom',q,re.I))
    paired = paired and not exact and not bool(re.search(r'\b(?:include|including|also|or)\b.{0,60}multiom',q,re.I))
    if old_assay or rna or atac or paired:
        available=vocabulary.get('modalities',[])
        constraints=[c for c in constraints if c not in old_assay]
        if paired:
            groups=[['snMultiomics']]
        elif exact:
            raw=str(old_assay[0]['value']) if old_assay else ('scATAC-seq' if atac and not rna else 'scRNA-seq')
            canonical=ALIASES.get(re.sub(r'[^a-z0-9]','',raw.lower()),raw)
            groups=[[canonical]]
        else:
            if rna:groups.append(['scRNA-seq','snMultiomics'])
            if atac:groups.append(['scATAC-seq','snMultiomics'])
            if not groups and old_assay:
                raw=str(old_assay[0]['value']);canonical=ALIASES.get(re.sub(r'[^a-z0-9]','',raw.lower()),raw)
                groups=[[canonical]]
        for values in groups:
            unknown=[v for v in values if v not in available]
            if unknown:
                suggestions=get_close_matches(unknown[0],available,n=3,cutoff=.45)
                issues.append('Unresolved assay '+unknown[0]+'. Suggestions: '+', '.join(suggestions)+'. No fuzzy substitution was applied.')
            c={'property':'data_modality','entity_type':'Sample_node','operator':'IN' if len(values)>1 else '=','value':json.dumps(values) if len(values)>1 else values[0]}
            constraints.append(c)
            matches.append({'requested':'RNA/ATAC assay intent','canonical_binding':c,'match_kind':'capability' if len(values)>1 or paired else 'alias','registry_version':VERSION,'source':SOURCE})
    if groups and not re.search(r'\bHPAP\b',q,re.I):
        issues.append('Assay capability mapping is verified for HPAP; specify HPAP or an exact recorded assay before expanding the search.')
    out['constraints']=constraints
    out['resolved_constraints']=matches
    out['semantic_registry']={'version':VERSION,'sha256':DIGEST,'graph_release':release,'modality_links_verified':vocabulary.get('modality_links_verified',False)}
    out['semantic_issues']=issues
    out['sample_requirements']={'modality_groups':groups,'paired':paired,'separate_bindings':len(groups)>1,
        'source':SOURCE,'file_availability':'not_verified'}
    if groups:out['semantic_summary']='Include documented RNA/ATAC components of the recorded assays; show the original assay labels and distinguish indexed samples from downloadable files.' if not exact else 'Match only the explicitly requested assay label; do not include related multiome assays.'
    return out


def generation_guidance(step):
    if not step.get('semantic_registry'):return ''
    notes='\nCanonical bindings above override shorthand stage/assay spellings in the question. t1d_stage is a donor property; sample fields: id, data_modality, anatomical_structure (text). No anatomical_structure_id or anatomical_structure_ref. Use anatomy -HAS_SAMPLE-> sample and donor -HAS_SAMPLE-> that same sample. Disease -HAS_DONOR-> donor. Return donor/sample nodes and linking evidence; no invented rank or extra sample requirements for donor-only questions.'
    notes+=' Do not filter sample.anatomical_structure: this is descriptive text, not a tissue identifier; constrain the linked anatomy node instead.'
    if not step.get('sample_requirements',{}).get('modality_groups') and not any(c.get('entity_type')=='anatomical_structure' for c in step.get('constraints',[])):
        notes+=' DONOR-ONLY lookup: do not MATCH Sample_node, data_modality, anatomical_structure or HAS_SAMPLE. Return all matching donors and their disease link, without any assay restriction.'
    if step.get('sample_requirements',{}).get('separate_bindings'):notes+=' For RNA AND ATAC bind two sample variables linked to the SAME donor and requested tissue; each must satisfy its corresponding modality group. The two variables may identify the same multiome sample.'
    return notes


def meaningful_row(row):
    """Empty graph wrappers are not evidence; scalar zero remains meaningful."""
    if isinstance(row,dict):
        # Preserve scalar nulls as missing measurements, rather than converting
        # an existing result row into a claim that no records matched.
        if any(not isinstance(v,(dict,list,tuple)) for v in row.values()):return bool(row)
        return any(meaningful_row(v) for v in row.values())
    if isinstance(row,(list,tuple)):return any(meaningful_row(v) for v in row)
    return row is not None


def validation_errors(tokens, step, parameters, bindings, paths, predicate, choices):
    errors=[]
    # Reject unsupported properties even when a key happens to exist elsewhere.
    from .graph import _predicate_owner
    for i,t in enumerate(tokens):
        dot=i>=2 and tokens[i-1].value=='.'
        mapped=i>0 and i+1<len(tokens) and tokens[i-1].value in ('{',',') and tokens[i+1].value==':'
        if dot or mapped:
            owner=_predicate_owner(tokens,i)
            labels=bindings.get(owner,set())
            supported=[set(PROPERTIES[l]) for l in labels if l in PROPERTIES]
            if supported and t.kind in {'WORD','IDENT'} and not any(t.value in props for props in supported):
                errors.append('invalid_node_property:'+','.join(sorted(labels))+'.'+t.value)
    if not step.get('semantic_registry'):return errors
    donors={v for v,labels in bindings.items() if 'donor' in labels}
    def constraints_at(variable, label):
        relevant=[group for c,group in zip(step.get('constraints',[]),choices) if c.get('entity_type')==label]
        return all(any(predicate(tokens,c,parameters,{variable}) for c in group) for group in relevant)
    donors={v for v in donors if constraints_at(v,'donor')}
    diseases={v for v,labels in bindings.items() if 'disease' in labels and constraints_at(v,'disease')}
    disease_required=any(c.get('entity_type')=='disease' for c in step.get('constraints',[]))
    if disease_required:donors={d for d in donors if any(a in diseases and b==d and 'HAS_DONOR' in kinds for a,b,kinds in paths)}
    if not donors:errors.append('missing_required_donor_cohort_path')
    requirement=step.get('sample_requirements',{})
    groups=requirement.get('modality_groups',[])
    tissue_required=any(c.get('entity_type')=='anatomical_structure' for c in step.get('constraints',[]))
    anatomy={v for v,labels in bindings.items() if 'anatomical_structure' in labels and constraints_at(v,'anatomical_structure')}
    if not groups and not tissue_required and any('HAS_SAMPLE' in kinds for a,b,kinds in paths):
        errors.append('unrequested_sample_join_for_donor_only_lookup')
    if groups or tissue_required:
        candidates=[]
        for group in groups or [None]:
            valid=set()
            for sample,labels in bindings.items():
                if 'Sample_node' not in labels:continue
                if group:
                    c={'property':'data_modality','operator':'IN' if len(group)>1 else '=','value':group if len(group)>1 else group[0]}
                    match=predicate(tokens,c,parameters,{sample})
                    if not match and step.get('semantic_registry',{}).get('modality_links_verified'):
                        match=any('data_modality' in bindings.get(a,set()) and b==sample and 'HAS_SAMPLE' in kinds
                            and predicate(tokens,{**c,'property':'id'},parameters,{a}) for a,b,kinds in paths)
                    if not match:continue
                if tissue_required and not any(a in anatomy and b==sample and 'HAS_SAMPLE' in kinds for a,b,kinds in paths):continue
                valid.add(sample)
            candidates.append(valid)
        aliases={tokens[i+1].value:tokens[i-1].value for i,t in enumerate(tokens[1:-1],1)
                 if t.value.upper()=='AS' and tokens[i-1].value in bindings and (i<2 or tokens[i-2].value!='.')}
        def canonical(v):
            seen=set()
            while v in aliases and v not in seen:seen.add(v);v=aliases[v]
            return v
        valid_donor=False
        for donor in donors:
            linked={b for a,b,kinds in paths if a==donor and 'HAS_SAMPLE' in kinds}
            sets=[{canonical(v) for v in s & linked} for s in candidates]
            if all(sets) and (not requirement.get('separate_bindings') or len(set.union(*sets))>=len(sets)):
                valid_donor=True
        if not valid_donor:errors.append('missing_same_donor_sample_tissue_modality_path')
    return errors


def donor_summary(evidence):
    nodes={str(n['id']):n for n in evidence.get('nodes',[]) if n.get('id')}
    donors={i:n for i,n in nodes.items() if 'donor' in n.get('labels',[])}
    if not donors:return None
    samples={i:n for i,n in nodes.items() if 'Sample_node' in n.get('labels',[])}
    links={d:set() for d in donors}
    for edge in evidence.get('edges',[]):
        if edge.get('type')=='HAS_SAMPLE' and str(edge.get('start_id')) in links and str(edge.get('end_id')) in samples:
            links[str(edge['start_id'])].add(str(edge['end_id']))
    return {'unique_donors':len(donors),'unique_samples':len(samples),'counts_scope':'retrieved evidence',
        'rows':[{'donor_id':d,'recorded_stage':donors[d].get('properties',{}).get('t1d_stage'),
        'sample_count':len(links[d]),'recorded_assays':sorted({samples[s].get('properties',{}).get('data_modality','unknown') for s in links[d]}),
        'file_availability':'not_verified'} for d in sorted(donors)],
        'assay_capabilities':{m:{'components':CAPABILITIES.get(m,[]),'source':SOURCE,'scope':'HPAP protocol; not a per-file check'} for m in sorted({n.get('properties',{}).get('data_modality','unknown') for n in samples.values()})}}
