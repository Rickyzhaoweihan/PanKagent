"""Deterministic answer facts from complete retrieved records before excerpts.

These are facts about the executed evidence, not a proof that a plan preserved
user intent or that an indexed result exhausts a source study. No I/O, model,
new graph query or per-donor identifier is used in this summary.
"""
from collections import Counter, defaultdict
from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
import re

from .release_schema import REGISTRY

VERSION = 'full-record-answer-facts-v1'
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
GO_SOURCE = 'https://geneontology.org/docs/guide-go-evidence-codes/'
# Formal names and categories verified against the official guide, 2026-09-09.
_GO_GROUPS = {
    'experimental': {'EXP':'Experiment', 'IDA':'Direct Assay', 'IPI':'Physical Interaction',
                     'IMP':'Mutant Phenotype', 'IGI':'Genetic Interaction', 'IEP':'Expression Pattern'},
    'high-throughput experimental': {'HTP':'High Throughput Experiment', 'HDA':'High Throughput Direct Assay',
                                    'HMP':'High Throughput Mutant Phenotype', 'HGI':'High Throughput Genetic Interaction',
                                    'HEP':'High Throughput Expression Pattern'},
    'phylogenetic': {'IBA':'Biological aspect of Ancestor', 'IBD':'Biological aspect of Descendant',
                    'IKR':'Key Residues', 'IRD':'Rapid Divergence'},
    'computational': {'ISS':'Sequence or structural Similarity', 'ISO':'Sequence Orthology',
                     'ISA':'Sequence Alignment', 'ISM':'Sequence Model', 'IGC':'Genomic Context',
                     'RCA':'Reviewed Computational Analysis'},
    'automatically generated': {'IEA':'Electronic Annotation'},
}
GO_CODES = {code: {'formal_name':'Inferred from ' + label, 'category':category}
            for category, values in _GO_GROUPS.items() for code, label in values.items()}
GO_CODES.update({'TAS': {'formal_name':'Traceable Author Statement','category':'author statement'},
                 'NAS': {'formal_name':'Non-traceable Author Statement','category':'author statement'},
                 'IC': {'formal_name':'Inferred by Curator','category':'curator statement'},
                 'ND': {'formal_name':'No biological Data available','category':'curator statement'}})


def _literal(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and len(value) <= 512:
        return value
    return None


def _property(value):
    if value is None or value == '':
        return {'value':value, 'state':'not_recorded'}
    converted = _literal(value)
    return {'value':converted, 'state':'recorded' if converted is not None else 'unsupported_value'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def _properties(record):
    value = record.get('properties') or {}
    return value if isinstance(value, Mapping) else {}


def _distribution(records, fields, cap):
    groups = Counter()
    values = {}
    for record in records:
        props = _properties(record)
        row = {field:_property(props.get(field)) for field in fields}
        key = _digest(row)
        values[key] = row
        groups[key] += 1
    ordered = sorted(groups, key=lambda key:(-groups[key], key))
    return {'groups':[{'recorded_fields':values[key], 'record_count':groups[key]} for key in ordered[:cap]],
            'full_record_count':len(records), 'full_group_count':len(groups),
            'omitted_group_count':max(0, len(groups)-cap),
            'omitted_record_count':sum(groups[key] for key in ordered[cap:])}


def _index(nodes):
    grouped = defaultdict(list)
    for node in nodes:
        if isinstance(node, Mapping) and isinstance(node.get('id'), str):
            grouped[node['id']].append(node)
    # A conflicting duplicate cannot supply a trustworthy categorical value.
    return {identifier: records[0] for identifier, records in grouped.items()
            if len({_digest(record) for record in records}) == 1}, sum(
                len({_digest(record) for record in records}) > 1 for records in grouped.values())


def _sample_facts(item, nodes, edges, complete, cap):
    samples = {key:node for key,node in nodes.items() if 'Sample_node' in (node.get('labels') or [])}
    donors = {key for key,node in nodes.items() if 'donor' in (node.get('labels') or [])}
    requested = set((item.get('requested_scope') or {}).get('relation_types') or [])
    has_samples = 'HAS_SAMPLE' in requested or any(edge.get('type') == 'HAS_SAMPLE' for edge in edges)
    if not samples and not has_samples:
        return None
    links = defaultdict(set)
    donor_link_records = 0
    for edge in edges:
        if edge.get('type') != 'HAS_SAMPLE':
            continue
        start, end = edge.get('start_id'), edge.get('end_id')
        if start in donors and end in samples:
            links[start].add(end)
            donor_link_records += 1
    assay_groups = defaultdict(set)
    assays = {}
    for identifier, sample in samples.items():
        assay = _property(_properties(sample).get('data_modality'))
        key = _digest(assay); assays[key] = assay; assay_groups[key].add(identifier)
    ordered = sorted(assay_groups, key=lambda key:(-len(assay_groups[key]), key))
    histogram = Counter(len(values) for values in links.values())
    linked_samples = set().union(*links.values()) if links else set()
    return {
        'unique_retrieved_samples':len(samples),
        'by_recorded_assay':[{'assay':assays[key], 'unique_samples':len(assay_groups[key]),
            'unique_linked_donors':sum(bool(sample_ids & assay_groups[key]) for sample_ids in links.values())}
            for key in ordered[:cap]],
        'omitted_assay_group_count':max(0,len(ordered)-cap),
        'sample_source_counts':_distribution(list(samples.values()), ('data_source',), cap),
        'donor_sample_distribution':{
            'count_scope':'all_retrieved_typed_donor_to_sample_links',
            'complete_for_executed_scope':complete,
            'unique_linked_donors':len(links), 'unique_linked_samples':len(linked_samples),
            'donor_sample_link_records':donor_link_records,
            'distinct_donor_sample_pairs':sum(len(values) for values in links.values()),
            'histogram':[{'matching_samples_per_donor':count,'donors':histogram[count]} for count in sorted(histogram)],
            'donors_without_a_returned_sample_link':len(donors-set(links)),
            'samples_without_a_returned_donor_link':len(set(samples)-linked_samples),
            'minimum_matching_samples_per_linked_donor':min(histogram) if histogram else None,
            'maximum_matching_samples_per_linked_donor':max(histogram) if histogram else None,
            'interpretation':'Histogram counts every retrieved, typed donor-to-sample link before examples are selected. '
                'A shortened excerpt does not make this computed distribution unavailable. These are matching retrieved '
                'samples, not all possible samples from each donor; missing links do not prove a donor has no samples.'},
        'individual_donor_examples_included':False,
        'file_download_availability_verified':False,
        'interpretation':'Assay counts use original recorded labels. Donor cohort source and sample source are separate '
            'metadata owners. Do not label all samples with an assay shown only in selected examples, and do not '
            'infer RNA components from unrecorded protocol knowledge.'}


def _lead_ids(raw):
    if isinstance(raw, str) and len(raw) <= 4096 and re.fullmatch(r'\s*rs\d+(?:\s*[,;| ]\s*rs\d+)*\s*', raw):
        return sorted(set(re.split(r'\s*[,;| ]\s*', raw.strip())))
    if isinstance(raw, list) and 0 < len(raw) <= 25 and all(isinstance(value,str) and re.fullmatch(r'rs\d+',value) for value in raw):
        return sorted(set(raw))
    return None


def _signal_roles(edges, nodes, cap):
    selected = [edge for edge in edges if edge.get('type') in {'SIGNAL_COLOC_WITH','PART_OF_GWAS_SIGNAL','PART_OF_QTL_SIGNAL'}]
    if not selected:
        return None
    records = []
    for edge in selected:
        kind = edge['type']; props = _properties(edge)
        row = {'record_sha256':_digest(dict(edge)), 'relation':kind,
               'source_id':edge.get('start_id'), 'target_id':edge.get('end_id'),
               'recorded_source':_property(props.get('data_source')),
               'recorded_source_version':_property(props.get('data_version'))}
        row['typed_endpoints_verified'] = (
            ('Gene' in (nodes.get(edge.get('start_id'),{}).get('labels') or []) and 'disease' in (nodes.get(edge.get('end_id'),{}).get('labels') or []))
            if kind == 'SIGNAL_COLOC_WITH' else
            ('variants' in (nodes.get(edge.get('start_id'),{}).get('labels') or []) and
             ('disease' if kind == 'PART_OF_GWAS_SIGNAL' else 'Gene') in (nodes.get(edge.get('end_id'),{}).get('labels') or [])))
        if kind == 'SIGNAL_COLOC_WITH':
            gwas, qtl = _lead_ids(props.get('gwas_lead_vars')), _lead_ids(props.get('qtl_lead_vars'))
            row.update(recorded_coloc_dataset=_property(props.get('coloc_dataset')),
                recorded_gwas_signal_id=_property(props.get('gwas_signal_id')),
                recorded_qtl_signal_id=_property(props.get('qtl_signal_id')),
                gwas_lead_variant_ids=gwas, qtl_lead_variant_ids=qtl,
                shared_recorded_lead_variant_ids=sorted(set(gwas)&set(qtl)) if gwas is not None and qtl is not None else None,
                same_complete_lead_set=(gwas == qtl) if gwas is not None and qtl is not None else None,
                raw_gwas_lead_vars=props.get('gwas_lead_vars') if gwas is not None else _literal(props.get('gwas_lead_vars')),
                raw_qtl_lead_vars=props.get('qtl_lead_vars') if qtl is not None else _literal(props.get('qtl_lead_vars')))
        else:
            typed = 'variants' in (nodes.get(edge.get('start_id'),{}).get('labels') or [])
            row['indexed_variant_id'] = edge.get('start_id') if typed else None
            raw = props.get('lead_status')
            row['recorded_lead_status'] = _property(raw)
            row['lead_role'] = ('recorded_lead' if raw == 'lead' and row['typed_endpoints_verified'] else 'recorded_nonlead' if raw == 'nonlead' and row['typed_endpoints_verified'] else 'not_established')
            row['interpretation'] = 'Membership is not lead status. Do not infer lead role from one indexed record, PIP or rank.'
        records.append(row)
    return {'records':records[:cap], 'full_record_count':len(records), 'omitted_record_count':max(0,len(records)-cap),
            'interpretation':'GWAS lead, QTL lead and credible-set membership are separate roles. A shared coloc association '
                'does not mean the same lead variant. Source and dataset labels belong to each record; do not invent '
                'GTEx-style or other source qualifiers. Keep primary coloc evidence separate from exact variant linkage.'}


def _go_facts(edges, nodes, cap):
    records = [edge for edge in edges if edge.get('type') == 'ASSOCIATED_WITH_GO']
    if not records:
        return None
    codes = Counter(); groups = defaultdict(list)
    for edge in records:
        props = _properties(edge)
        raw = props.get('go_evidence_code')
        code = raw if isinstance(raw,str) and len(raw) <= 32 else None
        codes[code] += 1
        target = nodes.get(edge.get('end_id'),{})
        domain = _literal(_properties(target).get('go_domain')) if 'GO_term' in (target.get('labels') or []) else None
        groups[(edge.get('start_id'),domain)].append(edge)
    typed_terms = all('GO_term' in (nodes.get(edge.get('end_id'),{}).get('labels') or []) for edge in records)
    ordered_codes = sorted(codes.items(),key=lambda pair:str(pair[0]))
    return {'full_record_count':len(records),
        'unique_recorded_term_ids':len({edge.get('end_id') for edge in records}) if typed_terms else None,
        'term_endpoint_types_verified':typed_terms,
        'by_gene_and_domain':[{'gene_id':key[0],'recorded_go_domain':key[1], 'annotation_records':len(values),
                               'unique_terms':len({edge.get('end_id') for edge in values})}
                              for key,values in sorted(groups.items(),key=lambda pair:str(pair[0]))[:cap]],
        'omitted_gene_domain_groups':max(0,len(groups)-cap),
        'recorded_codes':[{'code':code,'annotation_records':count, **GO_CODES.get(code, {'formal_name':None,'category':'unverified'})}
                          for code,count in ordered_codes[:cap]],
        'omitted_code_groups':max(0,len(ordered_codes)-cap),
        'omitted_code_records':sum(count for _,count in ordered_codes[cap:]),
        'definition_source':GO_SOURCE,'definition_verified':'2026-09-09',
        'interpretation':'Use formal names only for recorded codes. Distinct codes or annotation records are not '
            'statistically independent evidence. TAS is an author statement, not a direct assay; absence of a direct '
            'assay code does not establish an absence of experiments. Do not improvise unknown code expansions.'}


def build_answer_facts(item, *, coverage=None, max_groups=30, max_records=20):
    """Build bounded aggregates from the full step, preserving raw evidence.

    The ledger certifies counts over retrieved records only. Scope correctness
    remains the responsibility of the validated plan/query contract. Failed,
    truncated and unknown-scope results cannot gain a complete-evidence claim.
    """
    if not isinstance(item,Mapping) or item.get('graph_version') != REGISTRY['release']:
        return None
    edges = [edge for edge in item.get('edges') or [] if isinstance(edge,Mapping)]
    nodes, conflicts = _index(item.get('nodes') or [])
    if not edges and not nodes and item.get('status') not in {'complete','empty'}:
        return None
    max_groups = max(1,min(40,max_groups)); max_records = max(1,min(40,max_records))
    query_scope = (coverage or {}).get('query_scope') or {}
    complete = bool(item.get('status') in {'complete','empty'} and item.get('truncated') is False and not conflicts
        and query_scope.get('complete_for_requested_scope') is True
        and query_scope.get('constraints') == (item.get('requested_scope') or {}).get('constraints'))
    by_relation = defaultdict(list)
    for edge in edges:
        by_relation[str(edge.get('type') or 'unknown')].append(edge)
    result = {'version':VERSION,'digest':DIGEST,'graph_release':item['graph_version'],
        'count_scope':'all_retrieved_records_before_excerpt_selection',
        'complete_for_executed_scope':complete, 'user_scope_correctness_verified_by_this_ledger':False,
        'conflicting_node_identities':conflicts,
        'relationship_source_counts':{kind:_distribution(values, ('data_source','data_version'),max_groups)
                                      for kind,values in sorted(by_relation.items())},
        'interpretation':'These facts are computed from full retrieved records, not selected examples. They do not '
            'prove that an incorrect plan preserves the user request. Only the existing coverage contract can '
            'support a complete executed-scope claim. Do not invent source, method, causation, ambient RNA or '
            'aggregation explanations absent from the recorded evidence.'}
    sample = _sample_facts(item,nodes,edges,complete,max_groups)
    if sample is not None:
        result['sample_counts'] = sample
    if by_relation.get('PHYSICAL_INTERACTION'):
        result['physical_interaction_methods'] = _distribution(by_relation['PHYSICAL_INTERACTION'],
            ('experimental_system','experimental_system_type','throughput','data_source'),max_groups)
        result['physical_interaction_methods']['interpretation'] = ('Each group retains its recorded assay, throughput '
            'and source. Mixed groups cannot be summarized as all high throughput or as a single assay. An omitted '
            'group is still part of the full total; unknown throughput cannot be classified from the assay name.')
    signals = _signal_roles(edges,nodes,max_records)
    if signals is not None:
        result['signal_roles'] = signals
    go = _go_facts(edges,nodes,max_groups)
    if go is not None:
        result['go_annotations'] = go
    return result
