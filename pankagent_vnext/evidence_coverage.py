"""Recorded retrieval coverage, independent of source comparisons and display.

No model, database, or schema discovery is performed here. A current validated
execution may certify its requested scope. Only a narrow, inspected query shape
certifies all matching cell types; unknown and historical shapes stay unknown.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

VERSION = 'evidence-coverage-v1'
CELL_MEASUREMENTS = frozenset({
    'GENE_ENRICHED_IN', 'GENE_DETECTED_IN', 'MARKER_GENE_OF',
    'T1D_DEG_IN', 'GENE_ACTIVITY_SCORE_IN',
})
_NAMES = {
    'GENE_ENRICHED_IN': 'cell-type enrichment',
    'GENE_DETECTED_IN': 'gene-expression detection',
    'MARKER_GENE_OF': 'marker annotation',
    'T1D_DEG_IN': 'T1D differential-expression',
    'SIGNAL_COLOC_WITH': 'colocalization',
    'PART_OF_QTL_SIGNAL': 'molecular QTL',
    'PART_OF_GWAS_SIGNAL': 'GWAS',
}


def _cell_identity_property(property_name):
    """Recognize endpoint identity families without confusing cell statistics.

    The release contains several relationship-side cell labels. Future aliases
    with cell/target/subtype identity names must also stay conservative, while
    fields such as mean_pct_cells_expressing remain measurement filters.
    """
    if not isinstance(property_name, str):
        return False
    name = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', property_name).lower()
    tokens = set(re.split(r'[^a-z0-9]+', name))
    compact = ''.join(re.split(r'[^a-z0-9]+', name))
    if name in {'end_id', 'target_id', 'target_record_id', 'target_node_id'}:
        return True
    if 'celltype' in compact or 'cellsubtype' in compact:
        return True
    identities = {'id','ids','name','names','label','labels','type','types',
                  'subtype','subtypes','ontology','ontologies','identifier','identifiers'}
    return bool(tokens & {'cell','cells','target','subtype','subtypes'}
                and tokens & identities)


def _cell_query_scope(step, query, graph_version):
    """Conservative positive proof for a single unrestricted cell endpoint.

    Broader query syntax remains usable; it simply cannot receive this extra
    all-cell-types assertion. A valid inner join is not a complete independent
    evidence search, and an absent typed cell constraint is not sufficient.
    """
    relations = set(step.get('relation_types') or [])
    if not relations & CELL_MEASUREMENTS:
        return 'not_applicable'
    constraints = step.get('constraints') or []
    if any(c.get('entity_type') in {'anatomical_structure', 'CellType', 'CELL_TYPE'}
           for c in constraints if isinstance(c, Mapping)):
        return 'restricted'
    if any(c.get('relationship_type') in relations and _cell_identity_property(c.get('property'))
           for c in constraints if isinstance(c, Mapping)):
        return 'restricted'
    if (len(relations) != 1 or not constraints or step.get('depends_on')
            or not isinstance(query, str) or not query):
        return 'unknown'
    if any(not isinstance(c, Mapping) or not (
        c.get('entity_type') == 'Gene' and c.get('property') in {'id', 'name'}
        or c.get('relationship_type') in relations
            and not _cell_identity_property(c.get('property'))
    ) for c in constraints):
        return 'unknown'
    # Reuse the application's lexer and verified binding implementation. Import
    # lazily because the graph adapter invokes this only after validation.
    from .graph import tokenize, _pattern_bindings, _predicate_owner
    try:
        tokens = tokenize(query)
        words = [t.value.upper() for t in tokens if t.kind == 'WORD']
        if (words.count('MATCH') != 1 or words.count('RETURN') != 1
                or set(words) & {'OPTIONAL', 'UNION', 'UNWIND', 'EXISTS', 'CALL',
                                 'LIMIT', 'SKIP', 'CASE', 'FILTER', 'REDUCE'}
                or any(t.value == '..' for t in tokens)):
            return 'unknown'
        nodes, paths = _pattern_bindings(tokens, graph_release=graph_version)
        if len(nodes) != 2 or len(paths) != 1 or paths[0][2] != relations:
            return 'unknown'
        source, target, _ = paths[0]
        if 'Gene' not in nodes.get(source, ()) or 'anatomical_structure' not in nodes.get(target, ()):
            return 'unknown'
        # Any use of the cell variable in a predicate or its property map narrows
        # the returned set. WITH/RETURN projections don't change analysis scope.
        clause = ''
        projected = False
        for i, token in enumerate(tokens):
            if token.kind == 'WORD' and token.value.upper() in {'MATCH', 'WHERE', 'WITH', 'RETURN', 'ORDER'}:
                clause = token.value.upper()
                if clause in {'WITH', 'RETURN'}:
                    projected = True
                elif projected and clause in {'MATCH', 'WHERE'}:
                    return 'unknown'
            if clause == 'WHERE' and token.value == target:
                return 'restricted'
            if clause == 'MATCH' and token.value == '{' and _predicate_owner(tokens, i) == target:
                return 'restricted'
            property_access = (i >= 1 and tokens[i-1].value == '.' or
                               i >= 1 and i+1 < len(tokens) and tokens[i-1].value in {'{', ','}
                               and tokens[i+1].value == ':')
            if (clause in {'MATCH', 'WHERE'} and property_access
                    and _cell_identity_property(token.value)):
                return 'restricted'
        return 'all_matching'
    except (TypeError, ValueError, KeyError, IndexError):
        return 'unknown'


def source_comparisons(result):
    """Summarize recorded comparisons before individual edges are excerpted."""
    counts = Counter()
    for edge in result.get('edges') or []:
        if not isinstance(edge, Mapping) or edge.get('type') != 'GENE_ENRICHED_IN':
            continue
        properties = edge.get('properties') or {}
        comparison = properties.get('comparison')
        if not isinstance(comparison, str):
            continue
        key = re.sub(r'[\s_-]+', '', comparison).lower()
        if key not in {'onevsrest', 'oneversusrest'}:
            continue
        # Conditions are observations; never substitute an assumed source cohort.
        condition = properties.get('condition')
        counts[(edge['type'], str(condition) if condition is not None else None)] += 1
    return [{'relationship_type': kind, 'comparison': 'one_vs_rest',
             'condition': condition, 'record_count': count,
             'comparator': 'remaining_cell_types_in_source_analysis',
             'meaning': 'The source analysis compared each target cell type with the remaining cell types profiled in that analysis. Returned cells do not redefine this comparison.',
             'does_not_establish': ['exclusive_expression', 'annotated_marker_relationship']} 
            for (kind, condition), count in sorted(counts.items(), key=lambda pair: str(pair[0]))]


def _query_digest(query, parameters):
    return hashlib.sha256(json.dumps({'query': query, 'parameters': parameters or {}},
                                     sort_keys=True, default=str).encode()).hexdigest()


def build_evidence_coverage(step, result, *, graph_version, query=None,
                            parameters=None, validation_verified=False):
    """Record current accepted execution truth; never infer a fresh verification."""
    status = result.get('status', 'unknown')
    verified = bool(validation_verified and query and graph_version)
    projection = {'representation': 'unknown', 'record_membership_enumerated': False, 'missing_relations': []}
    if verified:
        from .graph import tokenize
        from .scientific_projection import projection_contract
        projection = projection_contract(tokenize(query), step, parameters)
    exhaustive = bool(verified and not projection['missing_relations'] and status in {'complete', 'empty'}
                      and result.get('truncated') is False and step.get('complete', True))
    cell_scope = _cell_query_scope(step, query, graph_version) if verified else 'unknown'
    return {
        'version': VERSION,
        'graph_release': graph_version,
        'query_scope': {
            'verification': 'validated_execution' if verified else 'unknown',
            'complete_for_requested_scope': exhaustive,
            'cell_type_scope': cell_scope,
            'relations': list(step.get('relation_types') or []),
            'constraints': deepcopy(step.get('constraints') or []),
            'depends_on': list(step.get('depends_on') or []),
            **({'path_spec': deepcopy(step['path_spec'])}
               if step.get('path_spec') is not None else {}),
        },
        'result': {'state': status,
                   'nodes': len(result.get('nodes') or []),
                   'edges': len(result.get('edges') or []),
                   'rows': len(result.get('rows') or []),
                   'retrieval_truncated': bool(result.get('truncated'))},
        'result_representation': projection['representation'],
        'record_membership_enumerated': projection['record_membership_enumerated'],
        'source_comparisons': source_comparisons(result),
        'absence_statement': ('No additional matching records exist in the checked PanKgraph release under these entities, evidence categories and filters.'
                              if exhaustive else 'Do not infer database-wide absence from this result.'),
        'source_comparison_is_not_query_scope': True,
        'model_context_and_graph_display_do_not_change_coverage': True,
        'query_sha256': _query_digest(query, parameters) if verified else None,
    }


def coverage_for_answer(result):
    """Current immutable coverage or explicitly unknown legacy coverage.

    Source comparison interpretation is still available for historical records;
    their query completion is never retroactively asserted from status alone.
    """
    coverage = result.get('evidence_coverage')
    if isinstance(coverage, Mapping) and coverage.get('version') == VERSION:
        recorded = coverage.get('result') or {}
        queries = result.get('queries') or []
        accepted = queries[-1] if isinstance(queries, list) and queries and isinstance(queries[-1], Mapping) else {}
        current_query = accepted.get('cypher')
        if (current_query and coverage.get('query_sha256') == _query_digest(current_query, accepted.get('parameters'))
                and coverage.get('graph_release') == result.get('graph_version')
                and recorded.get('state') == result.get('status')
                and recorded.get('retrieval_truncated') == bool(result.get('truncated'))
                and all(recorded.get(k) == len(result.get(k) or []) for k in ('nodes', 'edges', 'rows'))):
            return deepcopy(coverage)
    return build_evidence_coverage({}, result, graph_version=result.get('graph_version'),
                                   validation_verified=False)


def complete_empty_message(evidence):
    """Plain-language zero-match conclusion only from current complete searches."""
    steps = list(evidence.values()) if isinstance(evidence, Mapping) else list(evidence)
    primary = [s for s in steps if s.get('purpose') != 'context'] or steps
    if not primary or any(any(s.get(k) for k in ('nodes', 'edges', 'rows')) for s in primary):
        return None
    coverages = [coverage_for_answer(s) for s in primary]
    if not all(c['query_scope']['complete_for_requested_scope'] for c in coverages):
        return None
    releases = sorted({str(c['graph_release']) for c in coverages})
    if len(releases) != 1:
        return None
    relations = sorted({r for c in coverages for r in c['query_scope']['relations']})
    categories = [_NAMES[r] for r in relations if r in _NAMES]
    category = ' or '.join(categories) + ' ' if categories and len(categories) == len(relations) else ''
    primary_ids = {id(s) for s in primary}
    citations = ', '.join('[G' + str(index + 1) + ']' for index, s in enumerate(steps) if id(s) in primary_ids)
    return (f'PanKgraph contains no matching {category}records for the requested entities and filters '
            f'in {releases[0]} {citations}. The search completed for this scope. '
            'This is an absence of recorded evidence in this release, not proof that the biological relationship cannot occur.')


DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
