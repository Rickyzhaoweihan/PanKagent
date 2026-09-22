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

VERSION = 'full-record-answer-facts-v7-linked-coloc-records'
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


DONOR_CLASSIFICATION_FIELDS = (
    'diabetes_type', 'derived_diabetes_status', 't1d_stage', 'data_source')

# Public summaries use presentation-oriented names that do not necessarily
# preserve the underlying graph property spelling.  Keep the mapping explicit:
# a renamed key must not turn a protected donor classification into an
# unclassified value at an outbound boundary.  ``recorded_source`` is
# deliberately absent because it is also used for non-donor relationship
# provenance; the donor aggregate alias is the narrower ``*_counts`` key.
DONOR_CLASSIFICATION_KEY_ALIASES = {
    'recorded_diabetes_type': 'diabetes_type',
    'recorded_diabetes_types': 'diabetes_type',
    'recorded_diabetes_type_counts': 'diabetes_type',
    'recorded_derived_diabetes_status': 'derived_diabetes_status',
    'recorded_derived_diabetes_statuses': 'derived_diabetes_status',
    'recorded_derived_diabetes_status_counts': 'derived_diabetes_status',
    'recorded_stage': 't1d_stage',
    'recorded_stages': 't1d_stage',
    'recorded_stage_counts': 't1d_stage',
    'recorded_source_counts': 'data_source',
}


def _classification_stage_intent(question):
    """Recognize requests for recorded T1D-stage marginals.

    ``T1D`` often qualifies the stage field rather than independently asking
    for the donor's diabetes-type field.  Keep those two intents separate so a
    stage-only request does not disclose an unrelated classification.
    """
    stage_number = r'(?:\d+|I{1,3})'
    return any(re.search(pattern, question, re.I) for pattern in (
        rf'\bstages?\s*[-:]?\s*{stage_number}\b',
        r'\bT1D(?:M)?[ _-]+stages?\b',
        r'\b(?:by|across)\s+(?:T1D(?:M)?[ _-]+)?stages?\b',
        r'\bstages?\s+(?:distribution|breakdown)\b',
        r'\b(?:distribution|breakdown)\b[^.!?;]{0,80}\b(?:by|across|of)\s+'
        r'(?:T1D(?:M)?[ _-]+)?stages?\b',
    ))


def _mask_stage_qualified_t1d(question):
    """Mask only T1D tokens that are syntactically part of a stage phrase."""
    masked = re.sub(r'\bT1D(?:M)?(?=[ _-]+stages?\b)', ' ', question, flags=re.I)
    return re.sub(
        r'(\bstages?(?:\s*[-:]?\s*(?:\d+|I{1,3}))?\s*(?:[-:/]\s*)?)'
        r'T1D(?:M)?\b',
        lambda match: match.group(1) + ' ', masked, flags=re.I)


def requested_classification_fields(item):
    """Return classification marginals needed by the immutable request.

    Full marginals remain available to the private integrity check. Public
    answers expose only fields the user used to define the cohort (or named
    explicitly), so aggregate-only output does not reveal unrelated clinical
    attributes of a small retrieved group.
    """
    scope = item.get('requested_scope') or {}
    fields = {constraint.get('property') for constraint in scope.get('constraints') or []
              if constraint.get('entity_type') == 'donor'
              and constraint.get('property') in DONOR_CLASSIFICATION_FIELDS}
    bindings = item.get('request_filter_bindings') or []
    fields.update(constraint.get('property') for index, constraint in enumerate(
        item.get('constraints') or [])
        if constraint.get('entity_type') == 'donor'
        and constraint.get('property') in DONOR_CLASSIFICATION_FIELDS
        and any(binding.get('constraint_index') == index
                and binding.get('canonical_binding') == constraint
                and binding.get('source') == 'immutable_user_request'
                for binding in bindings))
    semantic_request = item.get('semantic_request') or {}
    trusted_question = (semantic_request.get('question')
                        if semantic_request.get('source') == 'user_request' else None)
    question = str(scope.get('original_question') or trusted_question or item.get('question')
                   or item.get('title') or '')
    from .semantic_registry import control_cohort_polarity, scope_intent_text
    question = scope_intent_text(question)
    type_intent_question = _mask_stage_qualified_t1d(question)
    diagnosed_t1d = re.search(
        r'\b(?:diagnos(?:is|es|ed|tic|tics)(?:\s+(?:with|as|of))?\s+T[12]D(?:M)?'
        r'|T[12]D(?:M)?\s+diagnos(?:is|es|ed|tic|tics))\b', question, re.I)
    if (control_cohort_polarity(question)['positive']
            or re.search(r'\b(?:diabetes[ _-]+type|T[12]D(?:M)?|type\s*(?:1|2|I|II)\s+diabetes)\b',
                         type_intent_question, re.I)
            or diagnosed_t1d):
        fields.add('diabetes_type')
    if re.search(r'\bderived(?:[ _-]+diabetes)?[ _-]+(?:status|classification)\b',
                 question, re.I):
        fields.add('derived_diabetes_status')
    if _classification_stage_intent(question):
        fields.add('t1d_stage')
    if re.search(r'\b(?:data|dataset|donor|cohort)[ _-]+source\b', question, re.I):
        fields.add('data_source')
    return fields


def sanitize_unrequested_classifications(value, allowed=frozenset()):
    """Remove donor-classification values from any outbound nested payload.

    Query rows are not guaranteed to preserve an owning node label.  Treat a
    key whose normalized suffix is one of the protected fields as donor
    classification data unless the immutable request explicitly needs it.
    The full private evidence remains unchanged for integrity checks.
    """
    allowed = set(allowed)
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r'[^a-z0-9]+', '_', str(key).casefold()).strip('_')
            protected = DONOR_CLASSIFICATION_KEY_ALIASES.get(normalized)
            if protected is None:
                protected = next((field for field in DONOR_CLASSIFICATION_FIELDS
                                  if normalized == field
                                  or normalized.endswith('_' + field)), None)
            if protected and protected not in allowed:
                continue
            result[key] = sanitize_unrequested_classifications(item, allowed)
        return result
    if isinstance(value, list):
        return [sanitize_unrequested_classifications(item, allowed) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_unrequested_classifications(item, allowed) for item in value)
    return value


def minimize_answer_facts_for_request(item, facts):
    """Copy an answer ledger with unrequested donor marginals removed.

    Local integrity checks run on the full ledger before this outbound/public
    projection. The minimized copy is safe for synthesis context and display.
    """
    if not isinstance(facts, Mapping):
        return facts
    result = dict(facts)
    summary = facts.get('donor_classifications')
    if not isinstance(summary, Mapping):
        return result
    allowed = requested_classification_fields(item)
    visible_fields = {field:value for field, value in (summary.get('fields') or {}).items()
                      if field in allowed}
    if visible_fields:
        result['donor_classifications'] = {
            **summary, 'fields':visible_fields,
            'interpretation':'Only donor classification fields used by the requested cohort are included in answer context.'}
    else:
        result.pop('donor_classifications', None)
    return result


def _donor_classifications(records, cap):
    """Return anonymous marginals over every unambiguous donor record.

    These are deliberately four separate distributions. In particular,
    ``t1d_stage`` is never used as a substitute for either diabetes field.
    """
    return {
        'counting_unit':'unique_retrieved_donor_nodes',
        'unique_retrieved_donors':len(records),
        'fields':{field:_distribution(records, (field,), cap)
                  for field in DONOR_CLASSIFICATION_FIELDS},
        'individual_donor_identifiers_included':False,
        'interpretation':'Recorded diabetes type, derived diabetes status and T1D stage are separate metadata. '
            'A recorded stage must not be relabeled as ND/healthy, diabetes type or a diagnosis. '
            'Source is provenance, not a clinical classification.'}


def _field_values(summary, field):
    distribution = ((summary or {}).get('fields') or {}).get(field) or {}
    values = set()
    for group in distribution.get('groups') or []:
        prop = ((group.get('recorded_fields') or {}).get(field) or {})
        value = prop.get('value') if prop.get('state') == 'recorded' else None
        values.add(value)
    return values


def cohort_integrity_issues(item, facts=None):
    """Pure fail-closed check between requested and retrieved donor cohorts.

    The function never derives diabetes type from stage. It verifies explicit
    donor categorical constraints and one high-confidence ND/healthy-control
    wording pattern against anonymous full-record marginals.
    """
    facts = facts if isinstance(facts, Mapping) else build_answer_facts(item)
    summary = (facts or {}).get('donor_classifications') or {}
    scope = item.get('requested_scope') or {}
    expected = defaultdict(set)
    excluded = defaultdict(set)
    for constraint in scope.get('constraints') or []:
        if (constraint.get('entity_type') != 'donor'
                or constraint.get('property') not in DONOR_CLASSIFICATION_FIELDS):
            continue
        operator = str(constraint.get('operator') or '=')
        raw = constraint.get('value')
        values = raw if operator == 'IN' and isinstance(raw, list) else None
        if operator == 'IN' and isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            values = parsed if isinstance(parsed, list) else None
        if values is None:
            values = [raw]
        if operator in {'=', 'IN'}:
            expected[constraint['property']].update(str(value).casefold() for value in values
                                                    if isinstance(value, str))
        elif operator in {'!=', '<>', 'NOT IN'}:
            excluded[constraint['property']].update(str(value).casefold() for value in values
                                                    if isinstance(value, str))
    semantic_request = item.get('semantic_request') or {}
    trusted_question = (semantic_request.get('question')
                        if semantic_request.get('source') == 'user_request' else None)
    question = str(scope.get('original_question') or trusted_question
                   or item.get('question') or item.get('title') or '')
    clinical_intent = scope.get('clinical_intent') or {}
    from .semantic_registry import control_cohort_polarity, scope_intent_text
    question = scope_intent_text(question)
    fallback_polarity = control_cohort_polarity(question)
    def control_like(value):
        normalized = re.sub(r'[^a-z0-9]+', ' ', value.casefold()).strip()
        return (normalized in {'nd', 'healthy', 'healthy control', 'non diabetic',
                               'control without diabetes', 'without diabetes'}
                or 'control without diabetes' in normalized)
    # The immutable original wording remains authoritative even if corrupted
    # or stale structured metadata says otherwise.
    explicit_control = (fallback_polarity['positive']
                        or clinical_intent.get('control_cohort') is True
                        and not fallback_polarity['negative'])
    scope_issues = []
    if (fallback_polarity['positive']
            and clinical_intent
            and clinical_intent.get('control_cohort') is not True):
        scope_issues.append({
            'field':'diabetes_type',
            'expected':['raw ND/healthy control intent'],
            'recorded':['contradictory structured clinical intent'],
            'reason':'structured_clinical_intent_conflicts_with_original_question'})
    if fallback_polarity['negative'] and (
            clinical_intent.get('control_cohort') is True
            or any(control_like(value) for value in expected.get('diabetes_type', set()))):
        scope_issues.append({
            'field':'diabetes_type',
            'expected':['raw exclusion of ND/healthy control cohort'],
            'recorded':['prepared positive control scope'],
            'reason':'structured_clinical_intent_conflicts_with_original_question'})
    negative_control = (fallback_polarity['negative']
                        or clinical_intent.get('excluded_control_cohort') is True)
    if negative_control:
        bound_exclusions = excluded.get('diabetes_type', set())
        valid_exclusions = {value for value in bound_exclusions if control_like(value)}
        if not bound_exclusions or valid_exclusions != bound_exclusions:
            scope_issues.append({
                'field':'diabetes_type',
                'expected':['runtime-resolved exclusion of ND/healthy control category'],
                'recorded':sorted(bound_exclusions) if bound_exclusions else ['no prepared control exclusion'],
                'reason':'requested_control_exclusion_not_bound'})
        excluded['diabetes_type'] = valid_exclusions or {'control without diabetes'}
    if explicit_control:
        bound = expected.get('diabetes_type', set())
        valid_bound = {value for value in bound if control_like(value)}
        trusted_scope = ('original_question' in scope
                         or clinical_intent.get('control_cohort') is True)
        if trusted_scope and (not bound or valid_bound != bound):
            scope_issues.append({'field':'diabetes_type',
                'expected':['runtime-resolved ND/healthy control category'],
                'recorded':sorted(bound) if bound else ['no prepared control constraint'],
                'reason':'requested_control_scope_not_bound'})
        # The trusted raw control intent is authoritative. A contradictory
        # prepared category must never broaden the accepted set to include T1D.
        expected['diabetes_type'] = valid_bound or {'control without diabetes'}
    if not summary.get('unique_retrieved_donors'):
        if not expected and not excluded:
            return scope_issues
        # A true empty graph result has no mismatching records. Scalar rows or
        # returned samples without donor classifications cannot substantiate a
        # clinical cohort count and therefore fail closed before synthesis.
        if item.get('status') == 'empty' and not item.get('rows') and not item.get('nodes'):
            return scope_issues
        unavailable = [{'field':field, 'expected':sorted(wanted),
                        'recorded':['donor classifications not returned'],
                        'reason':'retrieved_donor_classification_unavailable_for_scope_verification'}
                       for field, wanted in {**expected, **excluded}.items()]
        return scope_issues + unavailable
    issues = list(scope_issues)
    for field, wanted in expected.items():
        actual = _field_values(summary, field)
        distribution = ((summary.get('fields') or {}).get(field) or {})
        actual_text = {str(value).casefold() for value in actual if isinstance(value, str)}
        has_unrecorded = None in actual
        if (has_unrecorded or not actual_text or not actual_text.issubset(wanted)
                or distribution.get('omitted_record_count')):
            issues.append({'field':field, 'expected':sorted(wanted),
                           'recorded':sorted(str(value) if value is not None else 'not recorded'
                                             for value in actual),
                           'reason':'retrieved_donor_classification_outside_requested_scope'})
    for field, blocked in excluded.items():
        actual = _field_values(summary, field)
        distribution = ((summary.get('fields') or {}).get(field) or {})
        actual_text = {str(value).casefold() for value in actual if isinstance(value, str)}
        control_violation = (field == 'diabetes_type' and negative_control
                             and any(control_like(value) for value in actual_text))
        if (None in actual or not actual_text or actual_text.intersection(blocked)
                or control_violation or distribution.get('omitted_record_count')):
            issues.append({'field':field, 'expected':['exclude ' + value for value in sorted(blocked)],
                           'recorded':sorted(str(value) if value is not None else 'not recorded'
                                             for value in actual),
                           'reason':'retrieved_donor_classification_inside_excluded_scope'})
    return issues


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
    if not samples and item.get('rows'):
        # COUNT, AVG and other scalar projections can successfully answer a
        # query without returning a single sample identity. Neither a positive
        # scalar nor scalar zero enumerates sample-to-donor links or assays.
        # Do not guess a denominator from an alias or treat absent nodes as 0.
        return {
            'enumeration_state':'sample_identities_not_returned',
            'row_results_available':True,
            'complete_sample_enumeration_verified':False,
            'individual_donor_examples_included':False,
            'file_download_availability_verified':False,
            'interpretation':'The query returned row results without typed sample identities. '
                'Use the validated row values with their recorded query meaning, including a scalar zero. '
                'Do not infer zero samples from the absence of sample nodes. Counts by assay, unique '
                'linked donors and per-donor sample distributions cannot be computed from these rows.'}
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
        'enumeration_state':'retrieved_sample_identities',
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


def _signal_roles(edges, nodes, cap, coloc_summary=None):
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
    coloc_records = [row for row in records if row['relation'] == 'SIGNAL_COLOC_WITH']

    def recorded_values(field):
        values = []
        for row in coloc_records:
            recorded = row.get(field)
            value = recorded.get('value') if isinstance(recorded, Mapping) else None
            if recorded and recorded.get('state') == 'recorded' and isinstance(value, str) and value:
                values.append(value)
        return values

    gwas_signals = recorded_values('recorded_gwas_signal_id')
    qtl_signals = recorded_values('recorded_qtl_signal_id')
    pairs = [(row['recorded_gwas_signal_id']['value'], row['recorded_qtl_signal_id']['value'])
             for row in coloc_records
             if all(isinstance(row.get(field), Mapping)
                    and row[field].get('state') == 'recorded'
                    and isinstance(row[field].get('value'), str) and row[field]['value']
                    for field in ('recorded_gwas_signal_id', 'recorded_qtl_signal_id'))]
    coloc_counts = {
        'counting_unit': 'retrieved_SIGNAL_COLOC_WITH_relationship_records',
        'record_count': len(coloc_records),
        'gwas_signal_reference_count': len(gwas_signals),
        'distinct_recorded_gwas_signal_count': len(set(gwas_signals)),
        'recorded_gwas_signal_ids': sorted(set(gwas_signals)),
        'unresolved_gwas_signal_reference_count': len(coloc_records) - len(gwas_signals),
        'qtl_signal_reference_count': len(qtl_signals),
        'distinct_recorded_qtl_signal_count': len(set(qtl_signals)),
        'recorded_qtl_signal_ids': sorted(set(qtl_signals)),
        'unresolved_qtl_signal_reference_count': len(coloc_records) - len(qtl_signals),
        'distinct_recorded_signal_pair_count': len(set(pairs)),
        'unresolved_signal_pair_reference_count': len(coloc_records) - len(pairs),
        'interpretation': ('Signal references are counted once per retrieved colocalization relationship. '
            'Distinct signal counts deduplicate repeated identifiers across those records; they do not count '
            'separately indexed QTL or GWAS membership edges, and they do not establish complete credible-set membership.'),
    }
    if isinstance(coloc_summary, Mapping):
        derived_records = [record for record in coloc_summary.get('records') or []
                           if isinstance(record, Mapping)]
        records = ([row for row in records if row['relation'] != 'SIGNAL_COLOC_WITH']
                   + derived_records)
        coloc_counts = coloc_summary.get('counts') or coloc_counts
    result = {'records':records[:cap], 'full_record_count':len(records), 'omitted_record_count':max(0,len(records)-cap),
            'interpretation':'GWAS lead, QTL lead and credible-set membership are separate roles. A shared coloc association '
                'does not mean the same lead variant. Source and dataset labels belong to each record; do not invent '
                'GTEx-style or other source qualifiers. Keep primary coloc evidence separate from exact variant linkage.'}
    if coloc_records:
        result['coloc_signal_counts'] = coloc_counts
    if isinstance(coloc_summary, Mapping):
        result['colocalization_record_version'] = coloc_summary.get('version')
        result['colocalization_record_completeness'] = coloc_summary.get('completeness')
        result['colocalization_record_derivation'] = coloc_summary.get('derivation')
    return result


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
    result['source_classifications'] = {'counting_unit':'relationship records', 'distribution':source_classifications(edges), 'interpretation':'Source labels are not independent proof of causality.'}
    donors = [node for node in nodes.values() if 'donor' in (node.get('labels') or [])]
    if donors:
        result['donor_classifications'] = _donor_classifications(donors, max_groups)
    sample = _sample_facts(item,nodes,edges,complete,max_groups)
    if sample is not None:
        result['sample_counts'] = sample
    if by_relation.get('PHYSICAL_INTERACTION'):
        result['physical_interaction_methods'] = _distribution(by_relation['PHYSICAL_INTERACTION'],
            ('experimental_system','experimental_system_type','throughput','data_source'),max_groups)
        result['physical_interaction_methods']['interpretation'] = ('Each group retains its recorded assay, throughput '
            'and source. Mixed groups cannot be summarized as all high throughput or as a single assay. An omitted '
            'group is still part of the full total; unknown throughput cannot be classified from the assay name.')
    from .coloc_records import derive_colocalization_records
    coloc_summary = derive_colocalization_records(item)
    signals = _signal_roles(edges, nodes, max_records, coloc_summary)
    if signals is not None:
        result['signal_roles'] = signals
    go = _go_facts(edges,nodes,max_groups)
    if go is not None:
        result['go_annotations'] = go
    return result


def source_classifications(edges):
    """Count recorded labels, including the source's structured summary field.

    A label in arbitrary prose or a model answer is not a source classification.
    Conflicting labels remain unknown rather than choosing the strongest one.
    """
    counts = Counter()
    for edge in edges:
        props = _properties(edge)
        label = props.get('classification')
        if label is None and edge.get('type') == 'EFFECTOR_GENE_OF':
            try:
                records = json.loads(props.get('evidence', 'null'))
            except (ValueError, TypeError):
                records = None
            labels = set()
            for record in records if isinstance(records, list) else []:
                summary = record.get('summary') if isinstance(record, dict) else None
                if isinstance(summary, str):
                    match = re.fullmatch(r'.+ classified as (Possible|Moderate|Strong|Causal) \(overall score [0-9.]+\)', summary)
                    if match: labels.add(match.group(1))
            if len(labels) == 1: label = labels.pop()
        counts[str(label) if label is not None else 'not recorded'] += 1
    return dict(sorted(counts.items()))
