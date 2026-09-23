"""Versioned aggregate-only boundary. Historical unmarked runs are unchanged."""
from copy import deepcopy
import re

VERSION = 'aggregate-output-v5-donor-classifications'
PRIVATE_TYPES = {'donor', 'Sample_node'}


def aggregate_only(question):
    return bool(re.search(r'^\s*(?:(?:please|can\s+you|could\s+you|would\s+you)\s+){0,3}'
                          r'(?:how\s+many|count)\b|'
                          r'\b(?:total\s+)?number\s+of\b|'
                          r'\b(?:donor|sample|record|cohort)s?\s+counts?\b|'
                          r'\baggregate(?:s|[- ]only)?\b|\bcounts? only\b|'
                          r'\b(?:without|no|do not (?:show|list|include|print))\b[^.]{0,60}'
                          r'\b(?:donor|sample)\s*(?:ids?|identifiers?)\b', question, re.I))


def enabled(run):
    return (run.get('output_scope') or (run.get('plan') or {}).get('output_scope') or {}).get('mode') == 'aggregate_only'


def project(value, *, context=None):
    """Remove record-level channels on a copy; retain computed aggregate facts.

    This is applied before synthesis, at public snapshots and SSE replay. Results
    consumes the same public snapshot, so its graphs/tables/downloads inherit it.
    Raw canonical evidence stays private for query and count validation.
    """
    from .answer_facts import requested_classification_fields, minimize_answer_facts_for_request
    classification_keys = {
        'diabetes_type':'recorded_diabetes_type_counts',
        'derived_diabetes_status':'recorded_derived_diabetes_status_counts',
        't1d_stage':'recorded_stage_counts',
        'data_source':'recorded_source_counts',
    }
    hidden = set()
    def private(obj):
        return isinstance(obj, dict) and bool(set(obj.get('labels') or []) & PRIVATE_TYPES)
    def collect(obj):
        if isinstance(obj, list):
            for item in obj: collect(item)
        elif isinstance(obj, dict):
            if private(obj):
                for key in ('id', 'name'):
                    for source in (obj, obj.get('properties') or {}):
                        if source.get(key) is not None: hidden.add(str(source[key]))
            for key, item in obj.items():
                if key in {'donor_id', 'sample_id'} and item is not None: hidden.add(str(item))
                collect(item)
    collect(value)
    if context is not None: collect(context)
    def clean(obj, visible_classifications=None, evidence_record=None):
        if isinstance(obj, list):
            return [clean(x, visible_classifications, evidence_record) for x in obj if not private(x)]
        if isinstance(obj, str):
            if obj in hidden: return '[individual identifier withheld]'
            for identifier in sorted(hidden, key=len, reverse=True):
                if len(identifier) >= 4:
                    obj = re.sub(r'(?<!\w)' + re.escape(identifier) + r'(?!\w)', '[individual identifier withheld]', obj)
            return obj
        if not isinstance(obj, dict): return obj
        if any(key in obj for key in ('nodes', 'edges', 'answer_facts',
                                      'aggregate_cohort_facts', 'step_id', 'evidence_id')):
            evidence_record = obj
            visible_classifications = requested_classification_fields(obj)
        visible_classifications = visible_classifications or set()
        result = {}
        for key, item in obj.items():
            if key == 'answer_facts' and isinstance(item, dict):
                result[key] = clean(minimize_answer_facts_for_request(
                    evidence_record or obj, item), visible_classifications, evidence_record)
                continue
            if key == 'aggregate_cohort_facts' and isinstance(item, dict):
                item = {name:field_value for name, field_value in item.items()
                        if name not in classification_keys.values()
                        or name in {classification_keys[field] for field in visible_classifications}}
            if key == 'rows' and isinstance(item, list) and item and all(isinstance(row, dict) and {'time_minutes','mean_response'} <= set(row) <= {'time_minutes','mean_response','unit'} for row in item):
                result[key] = clean(item, visible_classifications, evidence_record)
                continue
            if key in {'queries', 'validation', 'generator_attempts', 'resolved_entities',
                       'resolved_constraints', 'rows', 'series', 'donor_id', 'sample_id',
                       'donor_ids', 'sample_ids', 'parameters', 'candidate_cypher', 'cypher'}:
                continue
            if key == 'edges' and isinstance(item, list):
                item = [e for e in item if not isinstance(e, dict) or
                        (str(e.get('start_id')) not in hidden and str(e.get('end_id')) not in hidden)]
            result[key] = clean(item, visible_classifications, evidence_record)
        if isinstance(obj.get('nodes'), list) and any(private(n) for n in obj['nodes']):
            from .semantic_registry import donor_summary
            summary = donor_summary(obj)
            if summary:
                # Aggregate-only output keeps totals, never reconstructed
                # per-donor rows (even after identifiers are redacted).
                summary = {key:item for key,item in summary.items() if key != 'rows'}
                result['donor_summary'] = clean(summary, visible_classifications, evidence_record)
            from collections import Counter
            donors = {n.get('id'):n for n in obj['nodes'] if 'donor' in n.get('labels', [])}
            samples = {n.get('id'):n for n in obj['nodes'] if 'Sample_node' in n.get('labels', [])}
            diabetes_types = Counter(str((n.get('properties') or {}).get('diabetes_type') or 'not recorded')
                                     for n in donors.values())
            derived_statuses = Counter(str((n.get('properties') or {}).get('derived_diabetes_status') or 'not recorded')
                                       for n in donors.values())
            stages = Counter(str((n.get('properties') or {}).get('t1d_stage') or 'not recorded') for n in donors.values())
            sources = Counter(str((n.get('properties') or {}).get('data_source') or 'not recorded') for n in donors.values())
            assays = {}
            for sample_id, sample in samples.items():
                modality = str((sample.get('properties') or {}).get('data_modality') or 'not recorded')
                group = assays.setdefault(modality, {'sample_ids':set(), 'donor_ids':set()})
                group['sample_ids'].add(sample_id)
                for edge in obj.get('edges', []):
                    if edge.get('type') == 'HAS_SAMPLE' and edge.get('end_id') == sample_id and edge.get('start_id') in donors:
                        group['donor_ids'].add(edge['start_id'])
            classifications = {
                'recorded_diabetes_type_counts':dict(diabetes_types),
                'recorded_derived_diabetes_status_counts':dict(derived_statuses),
                'recorded_stage_counts':dict(stages),
                'recorded_source_counts':dict(sources),
            }
            classifications = {key:value for key,value in classifications.items()
                               if key in {classification_keys[field]
                                          for field in visible_classifications}}
            result['aggregate_cohort_facts'] = clean({
                **classifications, 'assays':{name:{'sample_count':len(group['sample_ids']),
                    'donor_count':len(group['donor_ids'])} for name,group in assays.items()},
                'count_scope':'all retrieved donor nodes; diabetes type, derived status and stage remain separate',
                'file_availability':'not_verified'}, visible_classifications, evidence_record)
            result['aggregate_record_counts'] = {
                'donors': len({n.get('id') for n in obj['nodes'] if 'donor' in n.get('labels', [])}),
                'samples': len(samples) if samples else None,
                'count_scope': 'retrieved records', 'complete': obj.get('status') == 'complete' and obj.get('truncated') is False}
        return result
    result = clean(deepcopy(value))
    if isinstance(result, dict): result['output_scope'] = {'mode': 'aggregate_only', 'version': VERSION}
    return result
