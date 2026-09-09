"""Separate indexed QTL/GWAS records from source-reported variant counts.

No count here proves a credible set's complete membership. A complete graph
query can enumerate every indexed signal record while the source fine-mapping
table contains many more variants. Preserve source counts and their exact
record context; never sum them or infer a single-variant set from one edge.
"""
from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path

from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST

VERSION = 'indexed-signal-membership-v1'
DIGEST = hashlib.sha256(Path(__file__).read_bytes() + SCHEMA_DIGEST.encode()).hexdigest()
_SPECS = {
    'PART_OF_QTL_SIGNAL': {
        'target': 'Gene', 'count': 'n_snp',
        'identity': ('credible_set', 'data_source', 'data_version', 'tissue_id'),
        'optional_context': ('credibleset', 'tissue_name'),
    },
    'PART_OF_GWAS_SIGNAL': {
        'target': 'disease', 'count': 'credible_set_size',
        'identity': ('credible_set_id', 'data_source', 'data_version', 'method'),
        'optional_context': ('locus_name',),
    },
}


def _count(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value >= 0 and value.is_integer() else None
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 16:
        return int(value)
    return None


def _literal(value):
    """Only bounded scalar source metadata enters this extra model summary."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and len(value) <= 512:
        return value
    return None


def summarize_signal_membership(item: Mapping, *, max_records: int = 20) -> dict | None:
    """Summarize full retrieved records before excerpt selection, without I/O.

    Exact contextual metadata is supplied per record, not treated as proof of
    a globally unique biological signal. Missing identity fields stay unknown.
    Reported source counts are never added across records or across contexts.
    """
    if item.get('graph_version') != REGISTRY['release']:
        return None
    edges = [edge for edge in item.get('edges') or [] if isinstance(edge, Mapping) and edge.get('type') in _SPECS]
    if not edges:
        return None
    max_records = max(0, min(40, max_records))
    nodes = {str(node['id']): node for node in item.get('nodes') or [] if isinstance(node, Mapping) and 'id' in node}
    records, seen = [], set()
    for edge in edges:
        kind = edge['type']; spec = _SPECS[kind]
        props = edge.get('properties') or {}
        if not isinstance(props, Mapping):
            props = {}
        fingerprint = hashlib.sha256(json.dumps(dict(edge), sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        start, end = edge.get('start_id'), edge.get('end_id')
        typed = (isinstance(start, str) and isinstance(end, str)
                 and 'variants' in (nodes.get(start, {}).get('labels') or [])
                 and spec['target'] in (nodes.get(end, {}).get('labels') or []))
        metadata = {key: _literal(props.get(key)) for key in spec['identity'] + spec['optional_context']}
        complete_context = typed and all(isinstance(metadata[key], str) and metadata[key] for key in spec['identity'])
        count_property = spec['count']
        raw_count = props.get(count_property)
        record = {'edge_id': edge.get('id'), 'record_sha256': fingerprint, 'relation': kind,
            'indexed_variant_id': start if typed else None, 'target_id': end,
            'typed_endpoints_verified': bool(typed), 'recorded_signal_context': metadata,
            'recorded_context_state': 'fields_present' if complete_context else 'incomplete_or_unverified',
            'globally_unique_signal_identity_verified': False,
            'source_count_property': count_property, 'source_reported_count_raw': _literal(raw_count),
            'source_reported_count': _count(raw_count),
            'source_count_state': 'recorded' if _count(raw_count) is not None else 'missing_or_invalid',
            'complete_credible_set_membership_verified': False}
        if kind == 'PART_OF_GWAS_SIGNAL':
            record['recorded_lead_status'] = _literal(props.get('lead_status'))
        records.append(record)
    by_relation = {}
    for kind in sorted({record['relation'] for record in records}):
        selected = [record for record in records if record['relation'] == kind]
        by_relation[kind] = {'retrieved_record_count': len(selected),
            'unique_indexed_variant_ids': len({record['indexed_variant_id'] for record in selected})
                if all(record['typed_endpoints_verified'] for record in selected) else None,
            'source_counts_are_per_record_not_summed': True}
    return {'version': VERSION, 'digest': DIGEST, 'graph_release': item['graph_version'],
        'count_scope': 'all_retrieved_records_before_excerpt_selection',
        'input_record_count': len(edges), 'retrieved_record_count': len(records),
        'duplicate_input_records': len(edges) - len(records), 'by_relation': by_relation,
        'records': records[:max_records], 'summary_omitted_record_count': max(0, len(records) - max_records),
        'complete_credible_set_membership_verified': False,
        'interpretation': ('Retrieved records and unique indexed variants describe the graph result. '
            'The source-reported n_snp or credible_set_size describes a separate recorded variant count. '
            'One returned edge does not mean a single-variant credible set, even when the graph search is complete. '
            'Do not infer complete credible-set membership, sum counts across records, assign a source count to another '
            'gene/tissue/signal, or infer lead status from the number of retrieved records. '
            'Report source counts as recorded; if their denominator or context is unavailable, keep that uncertainty explicit.')}
