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

VERSION = 'verified-answer-blocks-v7-cohort-integrity'
SCHEMA = {'type': 'object', 'additionalProperties': False,
          'properties': {'fact_ids': {'type': 'array',
                                      'items': {'type': 'string'}}},
          'required': ['fact_ids']}
TOOL = {'name': 'select_answer_facts',
        'description': 'Select at most 24 relevant verified fact IDs. Never supply prose, numbers or new facts.',
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
           'credible_set', 'credibleset', 'credible_set_id', 'phenotype', 'coloc_dataset', 'gwas_signal_id', 'qtl_signal_id', 'gwas_lead_vars', 'qtl_lead_vars',
           'gwas_locus_name', 'qtl_locus_name', 'locus_name', 'lead_status', 'method',
           'effect_allele', 'other_allele', 'non_effect_allele', 'experimental_system',
           'experimental_system_type', 'qtl_type', 'type', 'molecular_trait', 'data_source_url', 'throughput', 'pubmed_id', 'publication', 'confidence', 'classification')


def text(value):
    # Recorded source strings are data, not Markdown/HTML or instructions.
    value = str(value).replace('\n', ' ').replace('\r', ' ')
    return re.sub(r'([\\`*_<>{}\[\]|])', r'\\\1', value)


FIELD_LABELS = {
    'data_source': 'Source', 'data_version': 'Source version',
    'condition': 'Condition', 'tissue_name': 'Tissue', 'tissue_id': 'Tissue ID',
    'pubmed_id': 'PubMed ID', 'data_source_url': 'Source URL',
    'ocr_gene_activity_score_mean': 'mean ATAC gene activity',
    'ocr_gene_activity_score_median': 'median ATAC gene activity',
    'total_cells': 'Total cells', 'n_donors': 'Donors',
    'median_donor_log_cpm': 'Median donor log-CPM',
    'median_donor_cpm': 'Median donor CPM',
    'median_pct_cells_expressing': 'Median percentage of cells expressing',
}


def field_label(key):
    # Only presentation changes; source values and metric distinctions stay intact.
    for prefix, label in [('type_1_diabetes_', 'T1D '), ('non_diabetic_', 'Non-diabetic ')]:
        if str(key).startswith(prefix):
            return label + field_label(str(key)[len(prefix):])
    return text(FIELD_LABELS.get(key, str(key).replace('_', ' ')))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


_COHORT_CLASSIFICATION_LABELS = (
    ('diabetes_type', 'Recorded donor diabetes types', 'recorded_diabetes_type_counts'),
    ('derived_diabetes_status', 'Recorded derived diabetes classifications',
     'recorded_derived_diabetes_status_counts'),
    ('t1d_stage', 'Recorded T1D stages', 'recorded_stage_counts'),
    ('data_source', 'Recorded donor sources', 'recorded_source_counts'),
)


def _cohort_classification_counts(step, ledger):
    """Read anonymous marginals without consulting selected donor examples."""
    from .answer_facts import requested_classification_fields
    requested = requested_classification_fields(step)
    aggregate = step.get('aggregate_cohort_facts') or {}
    summary = (ledger or {}).get('donor_classifications') or {}
    fields = summary.get('fields') or {}
    result = {}
    for field, label, aggregate_key in _COHORT_CLASSIFICATION_LABELS:
        if field not in requested:
            continue
        if isinstance(aggregate.get(aggregate_key), dict) and aggregate[aggregate_key]:
            result[field] = (label, dict(aggregate[aggregate_key]), 0)
            continue
        distribution = fields.get(field) or {}
        counts = Counter()
        for group in distribution.get('groups') or []:
            prop = ((group.get('recorded_fields') or {}).get(field) or {})
            value = prop.get('value') if prop.get('state') == 'recorded' else 'not recorded'
            counts[str(value)] += int(group.get('record_count') or 0)
        if counts:
            result[field] = (label, dict(counts), int(distribution.get('omitted_record_count') or 0))
    return result


def catalogue(evidence):
    steps = list(evidence.values()) if isinstance(evidence, dict) else list(evidence)
    ids = validate_ids(steps)
    facts = []
    identities = set()

    def add(eid, kind, prose, mandatory=False):
        identity = hashlib.sha256((eid + kind + prose).encode()).hexdigest()[:16]
        if (eid, identity) in identities:
            return next(f for f in facts if f['id'] == eid + ':' + identity)
        identities.add((eid, identity))
        facts.append({'id': eid + ':' + identity, 'evidence_id': eid,
                      'kind': kind, 'text': prose, 'mandatory': mandatory})
        return facts[-1]

    for step, eid in zip(steps, ids):
        title = text(step.get('title') or step.get('question') or 'Requested check')
        nodes, edges, rows = (step.get(k) or [] for k in ('nodes', 'edges', 'rows'))
        from .answer_facts import (requested_classification_fields,
                                   sanitize_unrequested_classifications)
        rows = sanitize_unrequested_classifications(
            rows, requested_classification_fields(step))
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
        from .answer_facts import build_answer_facts, cohort_integrity_issues
        ledger = step.get('answer_facts')
        if not isinstance(ledger, dict):
            ledger = build_answer_facts(step)
        classifications = _cohort_classification_counts(step, ledger)

        def add_classifications():
            for _field, (label, counts, omitted) in classifications.items():
                detail = '; '.join(text(value) + ': ' + str(count) + ' donors'
                                   for value, count in sorted(counts.items()))
                if omitted:
                    detail += '; ' + str(omitted) + ' donors in omitted recorded-value groups'
                add(eid, 'cohort_classification', label + ': ' + detail + '.', True)
            if classifications:
                add(eid, 'cohort_classification', 'Diabetes type, derived diabetes status and T1D stage are '
                    'separate recorded metadata; stage is not a diagnosis or an ND/healthy classification.', True)

        integrity_issues = cohort_integrity_issues(step, ledger) if isinstance(ledger, dict) else []
        if integrity_issues:
            fields = ', '.join(sorted({field_label(issue['field']) for issue in integrity_issues}))
            add(eid, 'cohort_integrity', 'Cohort integrity check failed: retrieved donor classifications do not '
                'satisfy the requested cohort filter for ' + fields + '. These records cannot be labeled as the '
                'requested cohort or used to answer its count.', True)
            add_classifications()
            continue
        execution = step.get('retrieval_execution') or {}
        aggregate = step.get('aggregate_record_counts')
        if aggregate:
            sample_text=(str(aggregate['samples'])+' assay/sample records retained' if aggregate.get('samples') is not None else 'assay/sample record count unavailable because sample records were not retained')
            add(eid, 'cohort', f"{title}: {aggregate['donors']} unique donors and {sample_text}. "
                + ('These are retrieved-record totals.' if aggregate.get('complete') else 'Retrieval is incomplete; these are retained-record counts only.')
                + ' Recorded stage is distinct from diagnosis; assay records are not donors.', True)
            cohort = step.get('aggregate_cohort_facts') or {}
            add_classifications()
            for assay,counts in sorted(cohort.get('assays',{}).items()):
                add(eid,'cohort',text(assay) + ': ' + str(counts['sample_count']) + ' assay/sample records linked to '
                    + str(counts['donor_count']) + ' unique donors. File availability has not been verified.',True)
            continue
        if not (nodes or edges or rows or step.get('functional_metadata')):
            verified_empty = (bool(step.get('queries')) and execution.get('completed') is True
                              and execution.get('cursor_exhausted') is True and not step.get('truncated'))
            add(eid, 'scope', f'{title}: ' + ('the executed query returned no matching records in its recorded scope.'
                if verified_empty else 'no records are available; exhaustive query execution is not verified.'), True)
            continue
        if step.get('dependency_inputs'):
            add(eid, 'input_scope', 'GWAS membership was checked for verified lead variants recorded by the preceding gene-scoped colocalization check; this is not a disease-wide GWAS search or exhaustive gene-locus fine mapping.', True)
        suffix = (' Coverage is incomplete; these are the retained matches, not an exhaustive list.'
                  if step.get('truncated') or step.get('status') == 'partial' else '')
        if step.get('functional_metadata'):
            add(eid, 'scope', f'{title}: aggregate functional measurements are available for the recorded cohort.' + suffix, True)
        else:
            categories = list(dict.fromkeys(LABELS.get(e.get('type'), str(e.get('type', 'Recorded evidence')).replace('_', ' ').lower()) for e in edges))
            heading = ', '.join(categories) if categories else 'Retrieved evidence'
            counts = [f"{len(edges)} relationship record{'s' if len(edges) != 1 else ''}"]
            if nodes:
                counts.append(f"{len(nodes)} {'entity' if len(nodes) == 1 else 'entities'}")
            if rows:
                counts.append(f"{len(rows)} result row{'s' if len(rows) != 1 else ''}")
            add(eid, 'scope', text(heading) + ': ' + ', '.join(counts) + ' retrieved.' + suffix, True)
        add_classifications()
        selection=(step.get('requested_scope') or {}).get('retrieval_selection') or {}
        if selection.get('mode')=='annotation_overview' and selection.get('ordering')=='stable_identifiers':
            add(eid,'annotation_selection','Annotation examples use stable-identifier ordering, not a ranking by biological importance or immune specificity. No ontology-depth or information-content ranking was computed.',True)
        index = {n['id']: n for n in nodes if isinstance(n, dict) and 'id' in n}

        def identity(identifier):
            node = index.get(identifier, {})
            props = node.get('properties') or {}
            name = props.get('name') or props.get('hgnc_symbol') or identifier
            roles = ', '.join(text(str(x).replace('_', ' ')) for x in node.get('labels', [])) or 'entity'
            return f'{text(name)} ({roles})'

        # Distributions use all records; examples never determine totals.
        from .answer_facts import source_classifications
        distribution = source_classifications(edges)
        if any(k != 'not recorded' for k in distribution):
            add(eid, 'source_classification', 'Recorded source classifications: ' +
                '; '.join(f'{text(k)}: {v} records' for k, v in sorted(distribution.items())) +
                '. These are source labels, not independent proof of causality.', True)
        interactions = [e for e in edges if e.get('type') == 'PHYSICAL_INTERACTION']
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
                details = '; '.join(f'{field_label(k)}: {text(v.get("value"))}' for k, v in fields.items())
                add(eid, 'interaction_method', f"Recorded interaction assay group: {group['record_count']} relationship records; {details}.")
        for edge_index, edge in enumerate(edges):
            props = edge.get('properties') or {}
            relation = edge.get('type', 'recorded relationship')
            details = [f'{field_label(k)}: {text(v)}' for k, v in props.items()
                       if finite(v) and not k.endswith('_id')][:12]
            details += [f'{field_label(k)}: {text(props[k])}' for k in CONTEXT if props.get(k) is not None]
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
                        add(eid, 'comparison', f'{identity(edge.get("end_id"))}: recorded {field_label(metric)} '
                            f'in T1D is {value}, {direction} non-diabetic samples ({nd}). This descriptive comparison '
                            'does not establish statistical significance or causality.')
        if len(edges) > 40:
            add(eid, 'context_omission', 'Detailed numerical comparisons are limited to 40 retained relationship records in this answer; all retrieved relationships remain in the result. This answer limit does not mean evidence is absent.', True)
        if any(e.get('type') == 'SIGNAL_COLOC_WITH' for e in edges):
            from .answer_facts import build_answer_facts
            roles = (build_answer_facts(step) or {}).get('signal_roles') or {}
            for role in roles.get('records', []):
                if role.get('relation') != 'SIGNAL_COLOC_WITH' or not role.get('typed_endpoints_verified'):
                    continue
                gwas, qtl = role.get('gwas_lead_variant_ids'), role.get('qtl_lead_variant_ids')
                if gwas is not None and qtl is not None:
                    shared = role.get('shared_recorded_lead_variant_ids')
                    add(eid, 'signal_roles', 'Recorded GWAS lead variants: ' + ', '.join(map(text,gwas))
                        + '; QTL lead variants: ' + ', '.join(map(text,qtl))
                        + '; shared recorded leads: ' + (', '.join(map(text,shared)) if shared else 'none')
                        + '. Lead identity is separate from credible-set membership; different leads do not invalidate recorded colocalization.', True)
            add(eid, 'limitation', 'Colocalization is recorded statistical evidence of a shared association signal under its source model; it is not proof of a causal gene or mechanism.', True)
        if any(e.get('type') == 'PART_OF_GWAS_SIGNAL' for e in edges):
            add(eid, 'limitation', 'GWAS association and fine-mapping posterior probabilities provide statistical support and variant prioritization, not proof of causality. Lead status is reported only when explicitly recorded; credible-set membership alone does not establish it.', True)
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
            specificity = re.search(r'\b(?:restricted|exclusive|exclusively|specific|specificity|only)\b',
                                    str(step.get('question') or step.get('title') or ''), re.I)
            if specificity:
                add(eid, 'takeaway', 'The available RNA detection evidence cannot establish cell-type exclusivity. '
                    'Missing detection records do not show that expression was measured and found absent in other cell types.', True)
            add(eid, 'assay', 'RNA detection describes expression within the recorded condition; it is not a T1D-versus-control comparison.'
                + ('' if specificity else ' It does not establish cell-type specificity.'), True)
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
                values = [f'{field_label(k)}: {text(v)}' for k, v in row.items()
                          if isinstance(v, (str, int, float)) and len(str(v)) <= 240]
                if values:
                    add(eid, 'row', 'Recorded result: ' + '; '.join(values) + '.')
    from .signal_comparison import membership_facts
    for comparison in membership_facts(steps, ids):
        members = lambda values: ', '.join(map(text, values)) if values else 'not verified in retrieved records'
        fact = add(comparison['evidence_id'], 'signal_membership',
            'Exact recorded signal linkage: GWAS credible set ' + text(comparison['gwas_signal_id'])
            + ' contains retrieved members ' + members(comparison['gwas_members'])
            + '; QTL credible set ' + text(comparison['qtl_signal_id'])
            + ' contains retrieved members ' + members(comparison['qtl_members'])
            + ' under the recorded source/tissue mapping. These are separate memberships; missing support does not establish absence.', True)
        fact['supporting_evidence_ids'] = sorted(set(fact.get('supporting_evidence_ids', [])) | set(comparison['support']))
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
    # Combine identical prose only; keep every supporting citation and never
    # merge different values, scopes or execution outcomes.
    grouped = {}
    for fact in facts:
        if fact['id'] not in chosen:
            continue
        entry = grouped.setdefault((fact['kind'], fact['text']), [])
        for eid in [fact['evidence_id'], *fact.get('supporting_evidence_ids', [])]:
            if eid not in entry:
                entry.append(eid)
    if not grouped:
        raise ValueError('empty_answer_blocks')
    lead, observations, context = [], [], []
    for (kind, prose), citations in grouped.items():
        line = prose + ' ' + ' '.join('[' + eid + ']' for eid in citations)
        target = lead if kind == 'takeaway' else context if kind in {
            'assay', 'limitation', 'annotation_selection', 'context_omission', 'input_scope'
        } else observations
        target.append(line)
    # Short answers stay short; longer evidence lists get two useful signposts.
    if len(grouped) > 5 and observations and context:
        parts = lead + ['**Recorded evidence**', '\n\n'.join(observations),
                        '**Interpretation and limits**', '\n\n'.join(context)]
    else:
        parts = lead + observations + context
    return '\n\n'.join(parts)


def fallback(facts):
    # Include a bounded representative record per step, in addition to all scope
    # facts. No additional provider request is needed after invalid model output.
    chosen, seen = [], set()
    for fact in facts:
        if not fact['mandatory'] and fact['evidence_id'] not in seen:
            chosen.append(fact['id']); seen.add(fact['evidence_id'])
    return render({'fact_ids': chosen[:24]}, facts)
