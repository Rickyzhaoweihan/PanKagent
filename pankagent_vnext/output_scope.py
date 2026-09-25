"""Aggregate answer presentation over public evidence; no privacy redaction."""
from copy import deepcopy
import re

VERSION = 'public-evidence-v6-aggregate-presentation'
COHORT_TYPES = {'donor', 'Sample_node'}


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
    """Copy public records and add authoritative aggregates for count answers.

    Aggregate-only is an answer presentation preference, not a restriction on
    public graph evidence, identifiers, clinical fields, API output or downloads.
    """
    def clean(obj):
        if isinstance(obj, list):
            return [clean(x) for x in obj]
        if not isinstance(obj, dict):
            return obj
        result = {key: clean(item) for key, item in obj.items()}
        nodes = [n for n in obj.get('nodes', []) if isinstance(n, dict)] if isinstance(obj.get('nodes'), list) else []
        if any(set(n.get('labels') or []) & COHORT_TYPES for n in nodes):
            from .semantic_registry import donor_summary
            summary = donor_summary(obj)
            if summary:
                result['donor_summary'] = clean(summary)
            from collections import Counter
            donors = {n.get('id'):n for n in nodes if 'donor' in n.get('labels', [])}
            samples = {n.get('id'):n for n in nodes if 'Sample_node' in n.get('labels', [])}
            diabetes_types = Counter(str((n.get('properties') or {}).get('diabetes_type') or 'not recorded')
                                     for n in donors.values())
            derived_statuses = Counter(str((n.get('properties') or {}).get('derived_diabetes_status') or 'not recorded')
                                       for n in donors.values())
            stages = Counter(str((n.get('properties') or {}).get('t1d_stage') or 'not recorded') for n in donors.values())
            sources = Counter(str((n.get('properties') or {}).get('data_source') or 'not recorded') for n in donors.values())
            assays = {}
            sample_donors = {}
            for edge in obj.get('edges', []):
                if not isinstance(edge, dict): continue
                if edge.get('type') == 'HAS_SAMPLE' and edge.get('start_id') in donors:
                    sample_donors.setdefault(edge.get('end_id'), set()).add(edge['start_id'])
            for sample_id, sample in samples.items():
                modality = str((sample.get('properties') or {}).get('data_modality') or 'not recorded')
                group = assays.setdefault(modality, {'sample_ids':set(), 'donor_ids':set()})
                group['sample_ids'].add(sample_id)
                group['donor_ids'].update(sample_donors.get(sample_id, ()))
            classifications = {
                'recorded_diabetes_type_counts':dict(diabetes_types),
                'recorded_derived_diabetes_status_counts':dict(derived_statuses),
                'recorded_stage_counts':dict(stages),
                'recorded_source_counts':dict(sources),
            }
            result['aggregate_cohort_facts'] = clean({
                **classifications, 'assays':{name:{'sample_count':len(group['sample_ids']),
                    'donor_count':len(group['donor_ids'])} for name,group in assays.items()},
                'count_scope':'all retrieved donor nodes; diabetes type, derived status and stage remain separate',
                'file_availability':'not_verified'})
            result['aggregate_record_counts'] = {
                'donors': len({n.get('id') for n in nodes if 'donor' in n.get('labels', [])}),
                'samples': len(samples) if samples else None,
                'count_scope': 'retrieved records', 'complete': obj.get('status') == 'complete' and obj.get('truncated') is False}
        return result
    result = clean(value)
    if isinstance(result, dict): result['output_scope'] = {'mode': 'aggregate_only', 'version': VERSION}
    return result
