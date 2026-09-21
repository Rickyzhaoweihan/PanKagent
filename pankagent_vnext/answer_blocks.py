"""Model-selected, locally rendered facts; no model-written observations.

The model selects relevance/order only. Numbers, entity roles, source context,
comparison direction and citations come from the immutable evidence catalogue.
This validates records, not their underlying experimental or causal truth.
"""
from collections import Counter
import hashlib
import json
import math
import re

from .evidence_identity import validate_ids

VERSION = 'verified-answer-blocks-v1'
SCHEMA = {'type': 'object', 'additionalProperties': False,
          'properties': {'fact_ids': {'type': 'array', 'maxItems': 24,
                                      'items': {'type': 'string'}}},
          'required': ['fact_ids']}
TOOL = {'name': 'select_answer_facts',
        'description': 'Select relevant verified fact IDs. Never supply prose, numbers or new facts.',
        'input_schema': SCHEMA, 'strict': True}
SYSTEM = ('Select the verified facts that directly answer the question, preserving each requested '
          'category and assay distinction. Use select_answer_facts. Mandatory facts are always '
          'included by the application. Do not invent observations or interpret missing evidence '
          'as absence. The application renders the answer from these facts after validation.')

LABELS = {'GENE_DETECTED_IN': 'RNA detection', 'GENE_ENRICHED_IN': 'RNA enrichment',
          'T1D_DEG_IN': 'T1D differential expression', 'GENE_ACTIVITY_SCORE_IN': 'ATAC gene activity',
          'PHYSICAL_INTERACTION': 'Physical interaction', 'GENETIC_INTERACTION': 'Genetic interaction',
          'PART_OF_QTL_SIGNAL': 'Molecular QTL membership', 'PART_OF_GWAS_SIGNAL': 'GWAS membership',
          'SIGNAL_COLOC_WITH': 'Colocalization', 'EFFECTOR_GENE_OF': 'Effector prioritization'}
CONTEXT = ('condition', 'tissue_name', 'tissue_id', 'data_source', 'data_version',
           'credible_set', 'credibleset', 'credible_set_id', 'phenotype', 'experimental_system',
           'experimental_system_type', 'qtl_type', 'type', 'molecular_trait', 'data_source_url', 'throughput', 'pubmed_id', 'publication', 'confidence', 'classification')


def text(value):
    # Recorded source strings are data, not Markdown/HTML or instructions.
    value = str(value).replace('\n', ' ').replace('\r', ' ')
    return re.sub(r'([\\`*_<>{}\[\]|])', r'\\\1', value[:240])


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def catalogue(evidence):
    steps = list(evidence.values()) if isinstance(evidence, dict) else list(evidence)
    ids = validate_ids(steps)
    facts = []
    identities = set()

    def add(eid, kind, prose, mandatory=False):
        identity = hashlib.sha256((eid + kind + prose).encode()).hexdigest()[:16]
        if (eid, identity) in identities:
            return
        identities.add((eid, identity))
        facts.append({'id': eid + ':' + identity, 'evidence_id': eid,
                      'kind': kind, 'text': prose, 'mandatory': mandatory})

    for step, eid in zip(steps, ids):
        title = text(step.get('title') or step.get('question') or 'Requested check')
        nodes, edges, rows = (step.get(k) or [] for k in ('nodes', 'edges', 'rows'))
        reasons = [str(r) for v in step.get('validation', []) for r in v.get('reasons', [])]
        skipped = step.get('execution_status') == 'skipped_empty_dependency' or any(
            r.startswith('empty_dependency:') for r in reasons)
        if skipped:
            add(eid, 'scope', f'{title}: this check was not executed because its required input was empty. '
                'It does not establish an independent zero-match search.', True)
            continue
        if step.get('status') in {'failed', 'blocked', 'unavailable', 'interrupted'}:
            add(eid, 'scope', f'{title}: evidence could not be retrieved; this does not establish absence.', True)
            continue
        execution = step.get('retrieval_execution') or {}
        aggregate = step.get('aggregate_record_counts')
        if aggregate:
            add(eid, 'cohort', f"{title}: {aggregate['donors']} unique donors and {aggregate['samples']} assay/sample records retained. "
                + ('These are retrieved-record totals.' if aggregate.get('complete') else 'Retrieval is incomplete; these are retained-record counts only.')
                + ' Recorded stage is distinct from diagnosis; assay records are not donors.', True)
            continue
        if not (nodes or edges or rows):
            verified_empty = (bool(step.get('queries')) and execution.get('completed') is True
                              and execution.get('cursor_exhausted') is True and not step.get('truncated'))
            add(eid, 'scope', f'{title}: ' + ('the executed query returned no matching records in its recorded scope.'
                if verified_empty else 'no records are available; exhaustive query execution is not verified.'), True)
            continue
        if step.get('dependency_inputs'):
            add(eid, 'input_scope', 'GWAS membership was checked for verified lead variants recorded by the preceding gene-scoped colocalization check; this is not a disease-wide GWAS search or exhaustive gene-locus fine mapping.', True)
        suffix = (' Coverage is incomplete; these are the retained matches, not an exhaustive list.'
                  if step.get('truncated') or step.get('status') == 'partial' else '')
        add(eid, 'scope', f'{title}: {len(edges)} relationship records, {len(nodes)} entities and '
            f'{len(rows)} result rows retained.' + suffix, True)
        index = {n['id']: n for n in nodes if isinstance(n, dict) and 'id' in n}

        def identity(identifier):
            node = index.get(identifier, {})
            props = node.get('properties') or {}
            name = props.get('name') or props.get('hgnc_symbol') or identifier
            roles = ', '.join(text(x) for x in node.get('labels', [])) or 'entity'
            return f'{text(name)} ({roles})'

        # Distributions use all records; examples never determine totals.
        from .answer_facts import source_classifications
        distribution = source_classifications(edges)
        if any(k != 'not recorded' for k in distribution):
            add(eid, 'source_classification', 'Recorded source classifications: ' +
                '; '.join(f'{text(k)}: {v} records' for k, v in sorted(distribution.items())) +
                '. These are source labels, not independent proof of causality.', True)
        interactions = [e for e in edges if e.get('type') in {'PHYSICAL_INTERACTION', 'GENETIC_INTERACTION'}]
        if interactions:
            from .answer_facts import build_answer_facts
            full = build_answer_facts(step) or {}
            from .evidence_context import _interaction_totals
            totals = _interaction_totals(step, edges, index, step.get('evidence_coverage') or {})
            if totals.get('unique_partner_genes') is not None:
                add(eid, 'partners', f"Physical interactions: {totals['focal_interaction_records']} relationship records involving {totals['unique_partner_genes']} unique partner genes; the focal gene is excluded from the partner count.", True)
            methods = full.get('physical_interaction_methods') or {}
            for group in methods.get('groups', []):
                fields = group['recorded_fields']
                details = '; '.join(f'{text(k)}={text(v.get("value"))}' for k, v in fields.items())
                add(eid, 'interaction_method', f"Recorded interaction assay group: {group['record_count']} relationship records; {details}.")
        for edge_index, edge in enumerate(edges):
            props = edge.get('properties') or {}
            relation = edge.get('type', 'recorded relationship')
            details = [f'{text(k)}={text(v)}' for k, v in props.items()
                       if finite(v) and not k.endswith('_id')][:12]
            details += [f'{text(k)}={text(props[k])}' for k in CONTEXT if props.get(k) is not None]
            if edge_index < 6:
                add(eid, 'relationship', f"{identity(edge.get('start_id'))} → {identity(edge.get('end_id'))}: "
                f"{text(LABELS.get(relation, relation))}. " + '; '.join(details) + '.', edge_index == 0)
            for key, value in props.items():
                if edge_index >= 40:
                    break
                if key.startswith('type_1_diabetes_') and finite(value):
                    metric = key[len('type_1_diabetes_'):]
                    nd = props.get('non_diabetic_' + metric)
                    if finite(nd):
                        direction = 'higher than' if value > nd else 'lower than' if value < nd else 'equal to'
                        add(eid, 'comparison', f'{identity(edge.get("end_id"))}: recorded {text(metric)} '
                            f'in T1D is {value}, {direction} ND ({nd}). This descriptive comparison '
                            'does not establish statistical significance or causality.')
        if len(edges) > 40:
            add(eid, 'context_omission', 'Detailed numerical comparisons are limited to 40 retained relationship records in this answer; all retrieved relationships remain in the result. This answer limit does not mean evidence is absent.', True)
        if any(e.get('type') == 'T1D_DEG_IN' for e in edges):
            add(eid, 'limitation', 'The recorded differential-expression result does not by itself '
                'establish cell specificity, a source-method significance threshold, or exclusion of technical artifacts.', True)
        if any(e.get('type') == 'PART_OF_QTL_SIGNAL' for e in edges):
            add(eid, 'limitation', 'Splicing QTLs concern splice/isoform usage; expression QTLs concern '
                'total expression. A generic molecular-QTL membership does not establish either subtype '
                'without recorded subtype evidence. Different credible-set IDs identify different recorded sets; signal linkage requires separate verified evidence.', True)
        if any(e.get('type') == 'GENE_ACTIVITY_SCORE_IN' for e in edges):
            add(eid, 'assay', 'ATAC gene activity is accessibility-derived, not RNA expression. Mean and median retain their recorded definitions.', True)
        if any(e.get('type') == 'GENE_DETECTED_IN' for e in edges):
            add(eid, 'assay', 'RNA detection within the recorded condition does not establish a T1D-versus-control differential-expression result or cell-type specificity.', True)
        if any(e.get('type') == 'GENE_ENRICHED_IN' for e in edges):
            add(eid, 'assay', 'Enrichment retains its source-analysis comparison population; returned cell types do not redefine a recorded one-versus-rest comparison.', True)
        metadata = step.get('functional_metadata')
        if metadata:
            n = metadata.get('contributing_donors')
            add(eid, 'contributors', f"Selected donor inventory: {metadata.get('unique_donors')}; "
                f"donors contributing finite values: {n if n is not None else 'unknown'}. "
                f"Measurement: {text(metadata.get('y_label'))}. Counts are unique donors, not selected series.", True)
            counts = metadata.get('contributing_donors_by_timepoint') or []
            known = [n for n in counts if isinstance(n, int)]
            if known and len(known) == len(counts):
                add(eid, 'contributors', f'Contributors per timepoint: {min(known)}–{max(known)} '
                    f'across {len(counts)} recorded timepoints.', True)
            points = metadata.get('trace_points') or []
            usable = [p for p in points if finite(p.get('mean_response'))]
            if usable:
                peak = max(usable, key=lambda p:p['mean_response'])
                low = min(usable, key=lambda p:p['mean_response'])
                add(eid, 'trace', f"Full mean trace: {len(points)} timepoints, {len(points)-len(usable)} missing means; recorded range {low['mean_response']}–{peak['mean_response']} {text(metadata.get('y_label'))}; highest recorded mean at {peak['time_minutes']} minutes.", True)
                facts[-1]['trace_points'] = points
            add(eid, 'trace_context', 'Applied filters: ' + text(json.dumps(metadata.get('filters', {}), sort_keys=True)) + '. Recorded stimulus intervals: ' + json.dumps(metadata.get('stimuli', []), ensure_ascii=False) + '. The cohort mean cannot establish individual variability or causality.', True)
        for row in rows[:4]:
            if isinstance(row, dict):
                values = [f'{text(k)}={text(v)}' for k, v in row.items()
                          if isinstance(v, (str, int, float)) and len(str(v)) <= 240]
                if values:
                    add(eid, 'row', 'Recorded result: ' + '; '.join(values) + '.')
    return facts


def render(selection, facts):
    if not isinstance(selection, dict) or set(selection) != {'fact_ids'}:
        raise ValueError('invalid_answer_blocks')
    selected = selection['fact_ids']
    index = {f['id']: f for f in facts}
    if (not isinstance(selected, list) or len(selected) > 24
            or any(not isinstance(x, str) or x not in index for x in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('invalid_answer_fact_reference')
    # Every requested category and every mandatory limitation survives selection.
    chosen = set(selected) | {f['id'] for f in facts if f['mandatory']}
    output = [f"{fact['text']} [{fact['evidence_id']}]" for fact in facts if fact['id'] in chosen]
    if not output:
        raise ValueError('empty_answer_blocks')
    return '\n\n'.join(output)


def fallback(facts):
    # Include a bounded representative record per step, in addition to all scope
    # facts. No additional provider request is needed after invalid model output.
    chosen, seen = [], set()
    for fact in facts:
        if not fact['mandatory'] and fact['evidence_id'] not in seen:
            chosen.append(fact['id']); seen.add(fact['evidence_id'])
    return render({'fact_ids': chosen[:24]}, facts)
