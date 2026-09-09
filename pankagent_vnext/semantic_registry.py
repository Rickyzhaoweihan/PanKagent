"""Release-scoped terminology and assay capabilities; never executable Cypher."""
from copy import deepcopy
from difflib import get_close_matches
import hashlib
import json
from pathlib import Path
import re

VERSION = 'pankgraph-semantics-v5-assay-operators'
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
from .donor_categories import DIGEST as DONOR_CATEGORIES_DIGEST
DIGEST = hashlib.sha256(json.dumps([VERSION, RELEASE, PROPERTIES, ALIASES, CAPABILITIES, SOURCE, DONOR_CATEGORIES_DIGEST],sort_keys=True).encode() + Path(__file__).read_bytes() + Path(__file__).with_name('tissue_aliases.py').read_bytes()).hexdigest()


def donor_intent(step):
    typed_donor = any(c.get('entity_type')=='donor' for c in step.get('constraints',[]))
    if typed_donor:
        return True
    relations = set(step.get('relation_types') or [])
    if relations and not relations.intersection({'HAS_DONOR', 'HAS_SAMPLE'}):
        # "Between diabetic and non-diabetic donors" describes the source
        # contrast of a molecular measurement, not a donor-inventory request.
        return False
    return bool(re.search(r'\bdonors?\b|\bHPAP\b',step.get('question',''),re.I) or any(c.get('entity_type')=='donor' for c in step.get('constraints',[])))


def planner_guidance(question):
    if not re.search(r'\bdonors?\b|\bHPAP\b|multiom|scRNA|ATAC', question,re.I): return ''
    return ('\nDonor/sample terminology: stage labels and assay names are resolved by the application against current graph values. '
            'Keep requested stages in ordinary biological wording. donor.t1d_stage belongs only to donor. A recorded T1D stage is not an additional diagnosed-diabetes category restriction; add the latter only when explicitly requested. '
            'Sample_node.data_modality is the assay; tissue uses anatomical_structure -HAS_SAMPLE-> Sample_node. '
            'RNA can be a documented component of multiome; retain exact-only or paired requirements. '
            'Always connect each sample directly to its donor; do not add samples to a donor-only lookup.')


def _assay_values(constraint):
    operator = str(constraint.get('operator', '=')).upper()
    raw = constraint.get('value')
    if operator in {'IN', 'NOT IN'}:
        try:
            values = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return None
        return values if isinstance(values, list) and values and all(isinstance(v, str) for v in values) else None
    return [raw] if isinstance(raw, str) else None


def _canonical_assay(value, available):
    alias = ALIASES.get(re.sub(r'[^a-z0-9]', '', str(value).lower()), value)
    matches = [v for v in available if isinstance(v, str) and v.casefold() == str(alias).casefold()]
    return matches[0] if len(matches) == 1 else value


def _without_negated_assays(question, available):
    """Mask directly negated verified assay names for capability detection only.

    The original wording and typed constraints remain unchanged. Missing or
    ambiguous modality names are not silently assigned a positive capability.
    """
    tokens = list(re.finditer(r'[A-Za-z0-9]+', question))
    words = [token.group().casefold() for token in tokens]
    aliases = {re.sub(r'[^a-z0-9]', '', value.lower()): value for value in available if isinstance(value, str)}
    aliases.update(ALIASES)
    spans = []
    for start in range(len(tokens)):
        candidates = [end for end in range(start + 1, min(len(tokens), start + 7) + 1)
                      if ''.join(words[start:end]) in aliases]
        if not candidates:
            continue
        end = max(candidates)
        prefix = words[max(0, start - 3):start]
        excluded = bool(prefix and (prefix[-1] in {'exclude', 'excluding', 'without', 'except', 'no'}
                        or prefix[-2:] in [['do', 'not'], ['not', 'include']]))
        if excluded and prefix[-1] in {'exclude', 'excluding'} and prefix[-3:-1] == ['do', 'not']:
            excluded = False
        if excluded:
            spans.append((tokens[start].start(), tokens[end - 1].end()))
    chars = list(question)
    for start, end in spans:
        chars[start:end] = ' ' * (end - start)
    return ''.join(chars)


def resolve(step, vocabulary, release):
    out=deepcopy(step)
    # Recovery is derived from this resolution, never inherited from a prior
    # failed preview after a user corrects the entity or scope.
    out.pop('recovery', None)
    if not donor_intent(out): return out
    q=out['question']; lower=q.lower(); constraints=out.setdefault('constraints',[])
    issues=[]; matches=[]; groups=[]
    if release!=RELEASE:
        out['semantic_issues']=['The terminology registry does not match this graph release.']
        out['recovery']={'category':'graph_release_mismatch','title':'The graph service needs attention',
            'message':'The terminology rules and the configured graph release do not match. Your question has been kept; an operator needs to align the service configuration. Changing the biological question will not fix this problem.',
            'retryable':False,'suggestions':[], 'evidence':{'graph_release':release,'registry_release':RELEASE}}
        return out
    def bind(prop, owner, value, operator='=', kind='alias', requested=None):
        # Replace only the same semantic field, preserving unrelated constraints.
        nonlocal constraints
        constraints=[c for c in constraints if not (c.get('property')==prop and c.get('entity_type') in (None,owner))]
        c={'property':prop,'entity_type':owner,'operator':operator,'value':json.dumps(value) if isinstance(value,list) else value}
        constraints.append(c)
        matches.append({'requested':requested or prop,'canonical_binding':deepcopy(c),'match_kind':kind,'registry_version':VERSION,'source':SOURCE if kind=='capability' else 'verified graph categorical values'})
    stage=re.search(r'\bstage\s*[-:]?\s*(\d+|iii|ii|i)\b',q,re.I)
    stage_unspecified = (not stage and bool(re.search(r'\bstage\b',q,re.I))
        and not re.search(r'\b(?:any|all|each)\b.{0,30}\bstage\b|\bby\s+(?:T1D\s+)?stage\b',q,re.I))
    if stage_unspecified:
        from .query_recovery import stage_recovery
        issues.append('A T1D stage was mentioned without a stage number. Please specify the intended stage; all other filters are kept.')
        out['recovery']=stage_recovery(None,vocabulary,release)
    if not stage and not stage_unspecified:
        c=next((c for c in constraints if c.get('property')=='t1d_stage'),None)
        if c:stage=re.search(r'(\d+|iii|ii|i)',str(c['value']),re.I)
    if stage:
        number={'i':'1','ii':'2','iii':'3'}.get(stage.group(1).lower(),stage.group(1))
        candidates=[v for v in vocabulary.get('stages',[]) if re.match(r'^Stage '+re.escape(number)+r':',v)]
        if len(candidates)==1:bind('t1d_stage','donor',candidates[0],requested=stage.group(0))
        else:
            issues.append('Requested T1D stage cannot be uniquely matched to a recorded stage. No substitute stage was selected.')
            from .query_recovery import stage_recovery
            out['recovery'] = stage_recovery(number, vocabulary, release)
    if re.search(r'\bHPAP\b',q,re.I):
        if 'HPAP' in vocabulary.get('sources',[]):bind('data_source','donor','HPAP',kind='exact',requested='HPAP')
        else:issues.append('HPAP donor source is not verified in this release.')
    disease=re.search(r'\bT([12])D\b|\btype\s*([12])\s*diabetes\b',q,re.I)
    stage_only_t1d = bool(stage and disease and (disease.group(1) or disease.group(2))=='1' and not re.search(r'diagnos|clinical diabetes|disease (?:link|category)|diabetes_type',q,re.I))
    if stage_only_t1d:
        constraints=[c for c in constraints if not (c.get('entity_type')=='disease' and c.get('property') in ('name','id') or c.get('entity_type')=='donor' and c.get('property')=='diabetes_type')]
        matches.append({'requested':disease.group(0),'canonical_binding':{'entity_type':'donor','property':'t1d_stage'},'match_kind':'recorded_stage_scope','registry_version':VERSION,'source':'verified stage versus disease-category aggregate inventory','explanation':'T1D stage is recorded donor metadata; it does not add a separate diagnosed-diabetes filter.'})
    if disease and not stage_only_t1d:
        number=disease.group(1) or disease.group(2)
        # Explicit cohort identity; never derive stage from diabetes status.
        constraints=[c for c in constraints if not (c.get('entity_type')=='disease' and c.get('property') in ('name','id'))]
        bind('id','disease','MONDO_0005147' if number=='1' else 'MONDO_0005148',requested=disease.group(0))
    from .tissue_aliases import matched_tissues
    tissues=matched_tissues(q, vocabulary.get('tissues',[]), constraints=constraints)
    if len(tissues)==1:
        constraints=[c for c in constraints if not (c.get('entity_type')=='anatomical_structure' and c.get('property') in ('id','name'))]
        bind('id','anatomical_structure',tissues[0]['id'],kind=tissues[0].get('match_kind','exact'),requested=tissues[0].get('requested_alias',tissues[0]['name']))
    elif len(tissues)>1:
        issues.append('Multiple sample tissues were named. Separate the tissue checks so each sample remains attached to its intended tissue.')
    old_assay=[c for c in constraints if c.get('property')=='data_modality' or c.get('entity_type')=='data_modality']
    available = vocabulary.get('modalities', [])
    negative_assay, positive_assay, unsupported_assay = [], [], []
    for original in old_assay:
        c = deepcopy(original)
        owner = c.get('entity_type')
        operator = str(c.get('operator', '=')).upper()
        values = _assay_values(c)
        if owner not in (None, 'Sample_node', 'data_modality') or c.get('owner_kind') == 'relationship':
            unsupported_assay.append(c)
            issues.append('Unsupported assay property owner; bind data_modality to the linked Sample_node.')
            continue
        if operator not in {'=', 'IN', '!=', '<>', 'NOT IN'} or values is None:
            unsupported_assay.append(c)
            issues.append('Unsupported assay operator or value; preserve its original meaning instead of converting it to a positive assay match.')
            continue
        mapped = [_canonical_assay(value, available) for value in values]
        c.update(entity_type='Sample_node', property='data_modality')
        c.pop('relationship_type', None)
        c.pop('owner_kind', None)
        c['value'] = (json.dumps(mapped) if isinstance(original.get('value'), str) else mapped) if operator in {'IN', 'NOT IN'} else mapped[0]
        matches.append({'requested': deepcopy(original), 'canonical_binding': deepcopy(c), 'match_kind': 'verified_assay_alias',
                        'registry_version': VERSION, 'source': 'verified graph categorical values'})
        if any(value not in available for value in mapped):
            issues.append('Unresolved assay value; no fuzzy substitution or operator change was applied.')
        (negative_assay if operator in {'!=', '<>', 'NOT IN'} else positive_assay).append(c)
        if operator == 'NOT IN':
            issues.append('NOT IN assay predicates are unsupported by the current typed query contract; keep this exclusion explicit rather than substituting positive evidence.')
    constraints = [c for c in constraints if c not in old_assay] + negative_assay + unsupported_assay
    old_assay = positive_assay
    positive_q = _without_negated_assays(q, available)
    assay_text=' '.join([positive_q]+[str(c.get('value','')) for c in old_assay])
    rna=bool(re.search(r'(?<![a-z])(?:sc|sn)?RNA[\s-]*(?:seq)?|transcriptom',assay_text,re.I))
    atac=bool(re.search(r'ATAC|chromatin accessibility',assay_text,re.I))
    paired=bool(re.search(r'multiom|paired|joint',positive_q,re.I))
    exact=bool(re.search(r'\bstandalone\b|\bexact(?:ly)?\b.{0,20}(?:RNA|ATAC)|(?:RNA[\s-]*seq|ATAC[\s-]*seq)\s+only\b|\bonly\s+(?:sc|sn)?(?:RNA|ATAC)|exclude\s+multiom',q,re.I))
    paired = paired and not exact and not bool(re.search(r'\b(?:include|including|also|or)\b.{0,60}multiom',q,re.I))
    if not unsupported_assay and (old_assay or rna or atac or paired):
        if paired:
            groups=[['snMultiomics']]
        elif exact:
            exact_sets = [_assay_values(c) for c in old_assay] if old_assay else [['scATAC-seq' if atac and not rna else 'scRNA-seq']]
            groups = [[_canonical_assay(value, available) for value in values] for values in exact_sets]
        else:
            if rna:groups.append(['scRNA-seq','snMultiomics'])
            if atac:groups.append(['scATAC-seq','snMultiomics'])
            if not groups and old_assay:
                groups = [[_canonical_assay(value, available) for value in _assay_values(c)] for c in old_assay]
        covered = {value for group in groups for value in group}
        for c in old_assay:
            extra = [value for value in _assay_values(c) if value not in covered]
            if extra:
                groups.append(extra)
                covered.update(extra)
        excluded_values = {value for c in negative_assay for value in (_assay_values(c) or [])}
        groups = [[value for value in group if value not in excluded_values] for group in groups]
        if any(not group for group in groups):
            issues.append('The requested positive assay and assay exclusion conflict; do not replace this with an unrestricted sample query.')
            groups = [group for group in groups if group]
        for values in groups:
            for i, value in enumerate(values):
                candidates=[m for m in available if m.casefold()==value.casefold()]
                if len(candidates)==1: values[i]=candidates[0]
            unknown=[v for v in values if v not in available]
            if unknown:
                suggestions=get_close_matches(unknown[0],available,n=3,cutoff=.45)
                issues.append('Unresolved assay '+unknown[0]+'. Suggestions: '+', '.join(suggestions)+'. No fuzzy substitution was applied.')
            c={'property':'data_modality','entity_type':'Sample_node','operator':'IN' if len(values)>1 else '=','value':json.dumps(values) if len(values)>1 else values[0]}
            constraints.append(c)
            matches.append({'requested':'RNA/ATAC assay intent','canonical_binding':c,'match_kind':'capability' if not exact and (rna or atac or paired) and (len(values)>1 or paired) else 'alias','registry_version':VERSION,'source':SOURCE})
    if unsupported_assay:
        constraints.extend(positive_assay)
    if positive_q != q and not negative_assay and not groups:
        issues.append('An assay exclusion was requested without a typed negative modality predicate; do not execute an unrestricted sample query.')
    expanded = bool(not exact and (rna or atac or paired) and groups and any(len(group)>1 or paired for group in groups))
    assay_sources = vocabulary.get('assay_donor_sources', {}).get('snMultiomics', [])
    verified_scope = bool(assay_sources) and set(assay_sources) == {'HPAP'}
    if expanded and verified_scope:
        matches.append({'requested': 'multiome capability scope', 'match_kind':'verified_dataset_scope', 'source':SOURCE, 'registry_version':VERSION, 'explanation':'All donor-linked snMultiomics records in this release are from HPAP; other requested assay records remain unrestricted by cohort.'})
    if expanded and not verified_scope and not re.search(r'\bHPAP\b',q,re.I):
        issues.append('Assay capability mapping is verified for HPAP; specify HPAP or an exact recorded assay before expanding the search.')
    from .donor_categories import resolve_categories
    constraints, categorical_matches = resolve_categories(constraints, vocabulary, PROPERTIES, release)
    matches.extend(categorical_matches)
    out['constraints']=constraints
    out['resolved_constraints']=matches
    out['semantic_registry']={'version':VERSION,'sha256':DIGEST,'graph_release':release,'modality_links_verified':vocabulary.get('modality_links_verified',False)}
    out['semantic_issues']=issues
    out['sample_requirements']={'modality_groups':groups,'paired':paired,'separate_bindings':len(groups)>1,
        'source':SOURCE,'file_availability':'not_verified','excluded_modality_constraints':deepcopy(negative_assay),'capability_scope_verified': bool(expanded and (verified_scope or re.search(r'\bHPAP\b',q,re.I)))}
    if negative_assay and not groups:
        out['semantic_summary'] = 'Match indexed samples with the requested assay exclusions; keep the original operators and donor/tissue filters. Assay exclusions do not exclude donors who also have other assays.'
    if groups:
        out['semantic_summary'] = ('Match only the explicitly requested assay label; do not include related multiome assays.' if exact else
            'Include documented RNA/ATAC components of the recorded assays; show the original assay labels and distinguish indexed samples from downloadable files.' if expanded else
            'Match the recorded assay labels with the requested filters; indexed samples do not verify downloadable files.')
    if groups:
        recorded = [v for group in groups for v in group]
        capability = any(v in ('scRNA-seq','scATAC-seq','snMultiomics') for v in recorded)
        if not capability:
            out['semantic_summary'] = 'Match recorded '+', '.join(recorded)+' assay metadata; indexed samples do not verify file availability or measured functional outcomes.'
            for match in out['resolved_constraints']:
                if match.get('requested') == 'RNA/ATAC assay intent': match['requested'] = 'recorded assay intent'
    from .donor_query_guard import normalize_diagnosis
    return normalize_diagnosis(out)


def generation_guidance(step):
    if not step.get('semantic_registry'):return ''
    notes='\nCanonical bindings above override shorthand stage/assay spellings in the question. A recorded T1D stage does not imply a second disease diagnosis filter: apply only the resolved disease constraint, if present. t1d_stage is a donor property; sample fields: id, data_modality, anatomical_structure (text). No anatomical_structure_id or anatomical_structure_ref. For stage-only questions do not add disease.id or donor.diabetes_type filters, including for stages 1 and 2; those are not necessarily recorded as diagnosed diabetes. Use anatomy -HAS_SAMPLE-> sample and donor -HAS_SAMPLE-> that same sample. Disease -HAS_DONOR-> donor. Return donor/sample nodes and linking evidence; no invented rank or extra sample requirements for donor-only questions.'
    notes+=' Do not filter sample.anatomical_structure: this is descriptive text, not a tissue identifier; constrain the linked anatomy node instead.'
    if not step.get('sample_requirements',{}).get('modality_groups') and not step.get('sample_requirements',{}).get('excluded_modality_constraints') and not any(c.get('entity_type')=='anatomical_structure' for c in step.get('constraints',[])):
        notes+=' DONOR-ONLY lookup: do not MATCH Sample_node, data_modality, anatomical_structure or HAS_SAMPLE. Return all matching donors and their disease link, without any assay restriction.'
    if step.get('sample_requirements',{}).get('separate_bindings'):notes+=' For RNA AND ATAC bind two sample variables linked to the SAME donor and requested tissue; each must satisfy its corresponding modality group. The two variables may identify the same multiome sample.'
    if step.get('sample_requirements',{}).get('excluded_modality_constraints'):
        notes += ' Apply every negative modality predicate to the same sample linked to the returned donor and requested tissue. Never turn an excluded assay into a positive capability lookup or exclude an entire donor unless explicitly requested.'
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
    exclusions=requirement.get('excluded_modality_constraints',[])
    tissue_required=any(c.get('entity_type')=='anatomical_structure' for c in step.get('constraints',[]))
    anatomy={v for v,labels in bindings.items() if 'anatomical_structure' in labels and constraints_at(v,'anatomical_structure')}
    if not groups and not exclusions and not tissue_required and any('HAS_SAMPLE' in kinds for a,b,kinds in paths):
        errors.append('unrequested_sample_join_for_donor_only_lookup')
    if groups or exclusions or tissue_required:
        candidates=[]
        for group in groups or [None]:
            valid=set()
            for sample,labels in bindings.items():
                if 'Sample_node' not in labels:continue
                def excluded_here(c):
                    if predicate(tokens, c, parameters, {sample}):
                        return True
                    return bool(step.get('semantic_registry',{}).get('modality_links_verified') and any(
                        'data_modality' in bindings.get(a,set()) and b==sample and 'HAS_SAMPLE' in kinds
                        and predicate(tokens,{**c,'property':'id'},parameters,{a}) for a,b,kinds in paths))
                if exclusions and not all(excluded_here(c) for c in exclusions):continue
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
