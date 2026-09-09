"""Preserve primary coloc evidence while verifying a requested variant's role.

No queries or model calls live here. Each prepared check uses the ordinary GPU
and validation path. Signal ID conventions are scoped to the reviewed release.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

VERSION = 'coloc-scope-3'
RELEASE = 'PanKgraph_08_04'
RELATIONS = {'SIGNAL_COLOC_WITH', 'PART_OF_GWAS_SIGNAL', 'PART_OF_QTL_SIGNAL'}
MAX_SUMMARY_RECORDS = 25
MAX_SUPPORTING_REFERENCES = 25
# The exact release/owner/ID binding is already verified by the semantic
# registry and the live coloc reference. This is not a vocabulary-wide permit:
# T1D remains an extra unresolved restriction for any other constrained disease.
VERIFIED_IDENTITY_ALIASES = {
    (RELEASE, 'disease', 'MONDO_0005147'): ('T1D', 'type 1 diabetes'),
}
# Reviewed against pankgraph_results/query.py; intentionally no runtime import
# of the results application or its query-executing handlers.
QTL_CONTEXT = {
    't1d_eQTL-inspire_coloc': ('INSPIRE; SusieR', 'UBERON_0000006'),
    't1d_eQTL-gtex_coloc': ('GTEx; SusieR', 'UBERON_0001264'),
    't1d_sQTL-gtex_coloc': ('splicing; GTEx', 'UBERON_0001264'),
    't1d_exonQTL-inspire_coloc': ('exon; INSPIRE', 'UBERON_0000006'),
}
DIGEST = hashlib.sha256(Path(__file__).read_bytes() + b'\n' + json.dumps({'version': VERSION, 'release': RELEASE,
    'qtl_context': QTL_CONTEXT, 'gwas_selected_suffix': '__selected'}, sort_keys=True).encode()).hexdigest()


def _identities(step):
    constraints = step.get('constraints') or []
    if len(constraints) != 3 or step.get('depends_on') or step.get('purpose') == 'context':
        return None
    found = {}
    for item in constraints:
        kind = item.get('entity_type')
        if (kind not in {'Gene', 'variants', 'disease'} or kind in found
                or item.get('operator', '=') != '=' or item.get('property') not in {'id', 'name'}
                or not isinstance(item.get('value'), str) or not item['value'].strip()
                or item.get('owner_kind', 'node') != 'node' or item.get('relationship_type')):
            return None
        found[kind] = item
    return found if len(found) == 3 else None


def _unrepresented_restriction(step):
    """Do not erase a textual restriction missing from the three identities.

    This is a conservative admission guard, not a parser that invents bindings.
    Unknown context after a scope preposition stays on the original question
    for an explicit planning correction; it is never silently discarded.
    """
    question = str(step.get('question') or '')
    # Identity values can contain words such as gene names that are also common
    # metadata words. They already have exact typed bindings and are not filters.
    values = [c.get('value') for c in step.get('constraints', [])]
    for constraint in step.get('constraints', []):
        if constraint.get('property') == 'id' and constraint.get('operator', '=') == '=':
            values.extend(VERIFIED_IDENTITY_ALIASES.get(
                (RELEASE, constraint.get('entity_type'), constraint.get('value')), ()))
    for item in step.get('resolved_entities') or []:
        if item.get('state') == 'resolved':
            values.extend([item.get('id'), item.get('name')])
    for value in sorted({v for v in values if isinstance(v, str) and v}, key=len, reverse=True):
        question = re.sub(r'(?<!\w)' + re.escape(value) + r'(?!\w)', 'ENTITY', question, flags=re.I)
    text = question.casefold()
    if step.get('ranking_contract') or step.get('ranking_issue') or re.search(
            r'\b(?:top\s*\d+|highest|lowest|largest|smallest|strongest|weakest|rank(?:ed|ing)?|sort(?:ed)?|order\s+by)\b', text):
        return 'ranking_restriction'
    if re.search(r'[<>≤≥]|\b(?:above|below|over|under|threshold|cutoff|cut-off|greater\s+than|less\s+than|'
                 r'at\s+least|at\s+most|more\s+than|fewer\s+than|significant(?:ly)?)\b', text):
        return 'statistical_restriction'
    if re.search(r'\b(?:only|exact(?:ly)?|exclusive(?:ly)?|except|exclud(?:e|ed|ing)|without|'
                 r'restrict(?:ed|ing)?|filter(?:ed|ing)?|limit(?:ed|ing)?)\b', text):
        return 'exact_or_exclusion_restriction'
    if re.search(r'\b(?:gtex|inspire|hpap|eqtlgen|ukbb|uk\s+biobank|eqtl|sqtl|exonqtl|pqtl|caqtl|'
                 r'tissues?|pancrea\w*|islets?|liver|blood|brain|muscle|spleen|cell\s+types?)\b', text):
        return 'source_tissue_or_assay_restriction'
    if re.search(r'\bstage\s*[-:]?\s*(?:[1-4]|i{1,3}|iv)\b|\b(?:donors?|patients?|cohort|age|sex|male|female)\b', text):
        return 'cohort_restriction'
    # These dataset-relative phrases do not narrow a query within its pinned
    # graph. Other prepositional contexts are not represented by identity-only
    # constraints, including unrecognized tissue or source names.
    allowed_context = r'(?:ENTITY\b(?=\s*[?,.;!]|\s*$)|(?:pankgraph|the\s+(?:graph|database|current\s+(?:graph|release))|this\s+(?:graph|release)|current\s+(?:graph|release))\b)'
    if re.search(r'\b(?:in|within|from|using|across)\s+(?!' + allowed_context + r')\S', question, re.I):
        return 'unrepresented_context'
    return None


def _blocked(plan, reason):
    if reason == 'unrepresented_restriction':
        message = ('Your question includes a scope restriction that is missing from the planned filters. '
                   'The original question and its restrictions have been kept. They must be represented explicitly before this search can run.')
        suggestion = {'label': 'Keep all my restrictions',
            'instruction': 'Keep the original gene, variant and disease, and every tissue, source, statistical cutoff, ranking or exact-match restriction. Represent each restriction explicitly before searching.'}
    else:
        message = ('This colocalization question needs three separate evidence checks so missing supporting records do not hide a recorded colocalization result. '
                   'Focus on one gene, one variant and one disease to fit the current investigation limit.')
        suggestion = {'label': 'Focus on colocalization',
            'instruction': 'Keep the same gene, variant and disease; focus on their recorded colocalization, GWAS membership and molecular QTL evidence.'}
    plan['coloc_scope_issue'] = reason
    plan['clarification'] = message
    plan['review_ready'] = False
    plan['recovery'] = {'category': 'coloc_scope_needs_clarification',
        'title': 'Keep the colocalization evidence separate', 'message': message,
        'retryable': False, 'suggestions': [suggestion],
        'evidence': {'reason': reason, 'registry_digest': DIGEST}}
    return plan


def normalize_plan(plan: dict, release: str, max_steps: int = 3) -> dict:
    """Split only the narrow, unfiltered three-relation cooccurrence shape.

    Unknown releases/extra biological constraints are left intact for the query
    guard to reject. Original checks and display groups remain in provenance.
    """
    result = copy.deepcopy(plan)
    comparison = result.pop('coloc_comparison_normalization', None)
    if comparison:
        result['steps'] = copy.deepcopy(comparison['original_steps'])
        result.pop('computed_operations', None)
        if comparison.get('original_display_groups') is None:
            result.pop('display_groups', None)
        else:
            result['display_groups'] = copy.deepcopy(comparison['original_display_groups'])
    old = result.pop('coloc_scope_normalization', None)
    if old:
        result['steps'] = copy.deepcopy(old['original_steps'])
        if old.get('original_display_groups') is None:
            result.pop('display_groups', None)
        else:
            result['display_groups'] = copy.deepcopy(old['original_display_groups'])
    if result.pop('coloc_scope_issue', None):
        if (result.get('recovery') or {}).get('category') == 'coloc_scope_needs_clarification':
            message = result['recovery']['message']
            result.pop('recovery', None)
            if result.get('clarification') == message:
                result['clarification'] = None
    if release != RELEASE:
        return result
    original = copy.deepcopy(result.get('steps') or [])
    selected = {step['id']: _identities(step) for step in original
                if set(step.get('relation_types') or []) == RELATIONS
                and step.get('evidence_combination', 'cooccurrence') == 'cooccurrence'
                and step.get('complete', True) is True and _identities(step)}
    if not selected:
        return result
    restrictions = [{'step_id': step['id'], 'kind': _unrepresented_restriction(step)} for step in original
                    if step['id'] in selected and _unrepresented_restriction(step)]
    if restrictions:
        result = _blocked(result, 'unrepresented_restriction')
        result['recovery']['evidence']['restrictions'] = restrictions
        return result
    if len(original) + 2 * len(selected) > max_steps:
        return _blocked(result, 'step_cap')
    used = {step['id'] for step in original}
    mapping = {key: [key, key + '_gwas', key + '_qtl'] for key in selected}
    additions = [part for parts in mapping.values() for part in parts[1:]]
    if len(additions) != len(set(additions)) or used.intersection(additions):
        return _blocked(result, 'step_id_collision')
    original_groups = copy.deepcopy(result.get('display_groups'))
    output = []
    groups = []
    for source in original:
        key = source['id']
        if key not in selected:
            unchanged = copy.deepcopy(source)
            unchanged['depends_on'] = list(dict.fromkeys(part for dependency in unchanged.get('depends_on', [])
                for part in mapping.get(dependency, [dependency])))
            output.append(unchanged)
            continue
        identity = selected[key]
        gene, variant, disease = (identity[k]['value'] for k in ('Gene', 'variants', 'disease'))
        specs = [
            ('primary', key, 'SIGNAL_COLOC_WITH', ['Gene', 'disease'],
             f'Check recorded colocalization for {gene} and {disease}',
             f'Retrieve every recorded Gene to disease SIGNAL_COLOC_WITH relationship for {gene} and {disease}. '
             'Return the recorded GWAS and QTL signal identifiers, lead variants and shared-signal statistics. '
             'Do not require a variant or an additional GWAS/QTL relationship; the requested variant is checked separately.'),
            ('gwas', key + '_gwas', 'PART_OF_GWAS_SIGNAL', ['variants', 'disease'],
             f'Check the GWAS signal containing {variant}',
             f'Retrieve recorded variants to disease PART_OF_GWAS_SIGNAL membership for {variant} and {disease}. '
             'Return exact credible-set identifiers and lead/member status without requiring a gene or QTL join.'),
            ('qtl', key + '_qtl', 'PART_OF_QTL_SIGNAL', ['variants', 'Gene'],
             f'Check molecular QTL evidence for {variant} and {gene}',
             f'Retrieve recorded variants to Gene PART_OF_QTL_SIGNAL evidence for {variant} and {gene}. '
             'Return original credible-set, source, tissue and statistics. Do not require a disease or colocalization join.'),
        ]
        for role, identifier, relation, kinds, title, question in specs:
            step = {k: copy.deepcopy(v) for k, v in source.items() if k in {'category', 'evidence_category', 'requested_category'}}
            step.update(id=identifier, title=title, question=question,
                rationale='Keep recorded colocalization and the requested variant’s supporting evidence separately inspectable.',
                relation_types=[relation], constraints=[copy.deepcopy(identity[k]) for k in kinds],
                complete=True, evidence_combination='independent', purpose='primary', depends_on=[],
                graph_version=release, coloc_scope={'role': role, 'source_step_id': key,
                    'version': VERSION, 'digest': DIGEST, 'requested_variant': copy.deepcopy(identity['variants'])})
            output.append(step)
        groups.append({'source_step_id': key, 'step_ids': dict(zip(('primary', 'gwas', 'qtl'), mapping[key])),
                       'original_constraints': copy.deepcopy(source['constraints'])})
    result['steps'] = output
    for group in result.get('display_groups') or []:
        key = 'step_ids' if 'step_ids' in group else 'steps' if 'steps' in group else None
        if key and isinstance(group[key], list) and all(isinstance(x, str) for x in group[key]):
            group[key] = [part for identifier in group[key] for part in mapping.get(identifier, [identifier])]
    result['coloc_scope_normalization'] = {'version': VERSION, 'digest': DIGEST, 'graph_release': release,
        'original_steps': original, 'original_display_groups': original_groups, 'groups': groups}
    return result


def _identity(step, kind):
    for index, item in enumerate(step.get('constraints') or []):
        if item.get('entity_type') != kind:
            continue
        if item.get('property') == 'id' and isinstance(item.get('value'), str):
            return item['value']
        for resolved in step.get('resolved_entities') or []:
            if resolved.get('constraint_index') == index and resolved.get('state') == 'resolved' and resolved.get('entity_type') == kind:
                return resolved.get('id')
    return None


def _leads(value):
    if isinstance(value, str):
        if len(value) > 4096 or not re.fullmatch(r'\s*rs\d+(?:\s*[,;| ]\s*rs\d+)*\s*', value):
            return set()
        value = re.split(r'\s*[,;| ]\s*', value.strip())
    if not isinstance(value, list) or not value or len(value) > 25:
        return set()
    return set(value) if all(isinstance(x, str) and re.fullmatch(r'rs\d+', x) for x in value) else set()


def _credible_set(signal):
    if not isinstance(signal, str) or not signal or len(signal) > 512:
        return None
    if signal.endswith('__selected'):
        return signal[:-10] if re.fullmatch(r'[A-Za-z0-9_.:+-]+__credibleSet\d+__selected', signal) else None
    return signal


def _ref(edge, step_id):
    return {'step_id': step_id, 'edge_id': edge.get('id'), 'type': edge.get('type'),
            'start_id': edge.get('start_id'), 'end_id': edge.get('end_id'),
            'record_sha256': hashlib.sha256(json.dumps(edge, sort_keys=True, default=str).encode()).hexdigest()}


def _record_enumeration(outcome, kind, start, end):
    if (outcome.get('status') not in {'complete', 'empty'} or outcome.get('truncated') is not False
            or outcome.get('graph_version') != RELEASE
            or outcome.get('retrieval_completeness') in {'partial', 'failed', 'truncated'}):
        return False
    if outcome.get('status') == 'empty':
        # True empty graph evidence is an enumerated zero; a scalar count,
        # including zero, does not enumerate the relationship records.
        return not any(outcome.get(key) for key in ('rows', 'nodes', 'edges'))
    coverage = outcome.get('evidence_coverage') or {}
    if coverage.get('record_membership_enumerated') is False:
        return False
    return any(edge.get('type') == kind and edge.get('start_id') == start and edge.get('end_id') == end
               for edge in outcome.get('edges', []))


def _edges(outcome, kind, start, end):
    if not _record_enumeration(outcome, kind, start, end):
        return []
    return [edge for edge in outcome.get('edges', []) if edge.get('type') == kind
            and edge.get('start_id') == start and edge.get('end_id') == end]


def _verified_pair(step, kinds):
    """Admit already separated, identity-only checks without changing their scope.

    Names and IDs both need a unique release-matched resolver record. Additional
    filters or dependencies remain on the original checks and are not treated
    as an unrestricted signal inventory by this annotation-only path.
    """
    constraints = step.get('constraints') or []
    if (step.get('graph_version') != RELEASE or step.get('complete') is not True
            or step.get('depends_on') or step.get('evidence_combination') != 'independent'
            or step.get('ranking_contract') or step.get('ranking_issue') or len(constraints) != 2):
        return None
    found = {}
    for index, constraint in enumerate(constraints):
        kind = constraint.get('entity_type')
        if (kind not in kinds or kind in found or constraint.get('operator', '=') != '='
                or constraint.get('property') not in {'id', 'name'}
                or constraint.get('owner_kind', 'node') != 'node' or constraint.get('relationship_type')
                or not isinstance(constraint.get('value'), str) or not constraint['value']):
            return None
        resolved = [record for record in step.get('resolved_entities') or []
                    if record.get('constraint_index') == index]
        if len(resolved) != 1:
            return None
        record = resolved[0]
        if (record.get('state') != 'resolved' or record.get('graph_version') != RELEASE
                or record.get('entity_type') != kind or not isinstance(record.get('id'), str)
                or not record['id']):
            return None
        requested = record.get('requested') or {}
        if any(requested.get(key) != constraint.get(key) for key in ('entity_type', 'property', 'value')):
            return None
        if requested.get('operator', '=') != constraint.get('operator', '='):
            return None
        if constraint['property'] == 'id' and constraint['value'] != record['id']:
            return None
        found[kind] = record['id']
    return tuple(found[kind] for kind in kinds) if set(found) == set(kinds) else None


def _separated_scope(plan):
    """Discover exact compatible checks; never join signals by gene proximity.

    These groups only associate the existing evidence containers. Actual signal
    linkage still requires the recorded IDs/source/tissue checks below. Ambiguous
    duplicate containers are intentionally left unannotated.
    """
    steps = plan.get('steps') or []
    identifiers = [step.get('id') for step in steps]
    if (any(not isinstance(identifier, str) or not identifier for identifier in identifiers)
            or len(identifiers) != len(set(identifiers))):
        return None
    roles = {
        'primary': ('SIGNAL_COLOC_WITH', ('Gene', 'disease')),
        'gwas': ('PART_OF_GWAS_SIGNAL', ('variants', 'disease')),
        'qtl': ('PART_OF_QTL_SIGNAL', ('variants', 'Gene')),
    }
    indexed = {role: {} for role in roles}
    for step in steps:
        for role, (relation, kinds) in roles.items():
            if step.get('relation_types') != [relation]:
                continue
            pair = _verified_pair(step, kinds)
            if pair:
                indexed[role].setdefault(pair, []).append(step['id'])
    groups = []
    for (gene, disease), primary_ids in indexed['primary'].items():
        if len(primary_ids) != 1:
            continue
        matching = []
        for (variant, gwas_disease), gwas_ids in indexed['gwas'].items():
            qtl_ids = indexed['qtl'].get((variant, gene), [])
            if gwas_disease != disease or len(gwas_ids) != 1 or len(qtl_ids) != 1:
                continue
            matching.append({'source_step_id': primary_ids[0],
                'step_ids': {'primary': primary_ids[0], 'gwas': gwas_ids[0], 'qtl': qtl_ids[0]}})
        # The existing per-primary evidence slot represents one requested
        # variant. Do not overwrite it with an arbitrary one of several.
        if len(matching) == 1:
            groups.extend(matching)
    if not groups:
        return None
    return {'version': VERSION, 'digest': DIGEST, 'graph_release': RELEASE, 'groups': groups,
            'source_kind': 'already_separated_verified_steps'}


def _comparison_only(question, parents):
    """Closed comparison vocabulary prevents hiding new retrieval modifiers."""
    text = str(question or '')
    if not text or len(text) > 3000:
        return False
    values = set()
    for parent in parents:
        values.add(parent['id'])
        for resolved in parent.get('resolved_entities') or []:
            values.update(value for value in (resolved.get('id'), resolved.get('name'),
                (resolved.get('requested') or {}).get('value')) if isinstance(value, str) and value)
            values.update(VERIFIED_IDENTITY_ALIASES.get(
                (RELEASE, resolved.get('entity_type'), resolved.get('id')), ()))
    for value in sorted(values, key=len, reverse=True):
        text = re.sub(r'(?<!\w)' + re.escape(value) + r'(?!\w)', ' ENTITY ', text, flags=re.I)
    text = re.sub(r'\b(?:PART_OF_GWAS_SIGNAL|PART_OF_QTL_SIGNAL|SIGNAL_COLOC_WITH)\b', 'signal', text, flags=re.I)
    words = re.findall(r'[A-Za-z]+|[^\sA-Za-z(),?:.;/\-]', text.casefold())
    allowed = set(('entity do does are is the these those their recorded retrieved reported '
        'existing available gwas qtl coloc colocalization signal signals identifier identifiers '
        'id ids credible set sets from for of to and or with between across in '
        'match matches matching exactly exact compare comparison whether same '
        'check determine establish using evidence result results step steps').split())
    return (bool(words) and set(words) <= allowed
            and bool(set(words) & {'match', 'matches', 'matching', 'compare', 'comparison', 'same'})
            and bool(set(words) & {'identifier', 'identifiers', 'id', 'ids'})
            and bool(set(words) & {'signal', 'signals'}))


def compile_comparisons(plan: dict, release: str) -> dict:
    """Compile proven terminal comparisons into traceable evidence operations.

    Called only after entity preparation. New biological filters, unverified
    identities, partial parent scopes and dependent retrieval remain executable.
    The existing exact-identifier linker supplies the computed operation result.
    """
    result = copy.deepcopy(plan)
    if (release != RELEASE or result.get('coloc_scope_normalization')
            or result.get('computed_operations') or result.get('coloc_comparison_normalization')):
        return result
    scope = _separated_scope(result)
    if not scope:
        return result
    steps = result.get('steps') or []
    by_id = {step['id']: step for step in steps}
    operations = []
    for step in steps:
        dependencies = step.get('depends_on') or []
        if (set(step.get('relation_types') or []) != RELATIONS
                or len(step.get('relation_types') or []) != 3 or step.get('constraints')
                or step.get('complete') is not True or len(dependencies) != 3
                or len(set(dependencies)) != 3 or step.get('purpose') == 'context'
                or step.get('ranking_contract') or step.get('ranking_issue')
                or step.get('semantic_issues') or step.get('schema_bindings')
                or any(step['id'] in other.get('depends_on', []) for other in steps)):
            continue
        groups = [group for group in scope['groups'] if set(group['step_ids'].values()) == set(dependencies)]
        if len(groups) != 1:
            continue
        parents = [by_id[key] for key in dependencies]
        if any(steps.index(parent) >= steps.index(step) for parent in parents):
            continue
        if not _comparison_only(step.get('question'), parents):
            continue
        operations.append({'id': step['id'], 'operation': 'compare_coloc_signal_identifiers',
            'depends_on': copy.deepcopy(dependencies), 'source_step_id': groups[0]['source_step_id'],
            'step_ids': copy.deepcopy(groups[0]['step_ids']), 'original_step': copy.deepcopy(step),
            'version': VERSION, 'digest': DIGEST, 'graph_release': release,
            'implementation': 'summarize_linkage', 'no_new_retrieval': True})
    if not operations:
        return result
    original_groups = copy.deepcopy(result.get('display_groups'))
    removed = {operation['id'] for operation in operations}
    replacements = {operation['id']: operation['depends_on'] for operation in operations}
    result['steps'] = [step for step in steps if step['id'] not in removed]
    for group in result.get('display_groups') or []:
        key = 'step_ids' if 'step_ids' in group else 'steps' if 'steps' in group else None
        if key and isinstance(group[key], list) and all(isinstance(x, str) for x in group[key]):
            original_refs = group[key]
            group[key] = list(dict.fromkeys(part for identifier in original_refs for part in replacements.get(identifier, [identifier])))
            if removed.intersection(original_refs):
                group['computed_operation_ids'] = [identifier for identifier in original_refs if identifier in removed]
    result['computed_operations'] = operations
    result['coloc_comparison_normalization'] = {'version': VERSION, 'digest': DIGEST,
        'graph_release': release, 'original_steps': copy.deepcopy(steps), 'original_display_groups': original_groups}
    return result


def _computed_outcomes(plan, summary):
    outcomes = []
    for operation in plan.get('computed_operations') or []:
        if operation.get('operation') != 'compare_coloc_signal_identifiers':
            continue
        outcome = {'id': operation.get('id'), 'operation': operation['operation'],
            'depends_on': copy.deepcopy(operation.get('depends_on')), 'status': 'unverified',
            'no_new_retrieval': True}
        if operation.get('digest') == DIGEST and operation.get('graph_release') == RELEASE:
            matches = [group for group in summary.get('groups', []) if group.get('step_ids') == operation.get('step_ids')]
            if len(matches) == 1:
                group = matches[0]
                outcome.update(source_step_id=group['source_step_id'],
                    status='complete' if group.get('status') == 'checked'
                        and all(group.get('record_enumeration', {}).values())
                        and all(state in {'complete', 'empty'} for state in group.get('step_outcomes', {}).values()) else 'blocked',
                    result_reference='coloc_linkage.groups:' + group['source_step_id'])
        outcomes.append(outcome)
    return outcomes


def summarize_linkage(plan: dict, previous: dict) -> dict:
    """Link complete records through exact recorded identifiers, never cooccurrence.

    The original primary graph is untouched even if context is missing. A linked
    result records why the requested variant relates to each coloc record.
    """
    # Do not bypass an explicitly present stale normalization with fresh-looking
    # steps. Only plans that were already separate may use identity discovery.
    meta = (plan.get('coloc_scope_normalization') or {}) if 'coloc_scope_normalization' in plan else (_separated_scope(plan) or {})
    summary = {'version': VERSION, 'digest': DIGEST, 'graph_release': meta.get('graph_release'), 'groups': []}
    if meta.get('source_kind'):
        summary['source_kind'] = meta['source_kind']
    if meta.get('digest') != DIGEST or meta.get('graph_release') != RELEASE:
        summary['status'] = 'not_applicable_or_stale'
        return summary
    by_id = {step['id']: step for step in plan.get('steps', [])}
    for group in meta.get('groups', []):
        ids = group['step_ids']
        primary, gwas, qtl = (by_id.get(ids[k], {}) for k in ('primary', 'gwas', 'qtl'))
        gene = _identity(primary, 'Gene'); disease = _identity(primary, 'disease'); variant = _identity(gwas, 'variants')
        item = {'source_step_id': group['source_step_id'], 'gene_id': gene, 'disease_id': disease,
            'requested_variant_id': variant, 'step_ids': ids,
            'step_outcomes': {role: previous.get(identifier, {}).get('status', 'not_attempted') for role, identifier in ids.items()},
            'records': [], 'primary_record_count': None, 'linked_record_count': None}
        summary['groups'].append(item)
        if not all(isinstance(x, str) and x for x in (gene, disease, variant)) or _identity(gwas, 'disease') != disease or _identity(qtl, 'Gene') != gene or _identity(qtl, 'variants') != variant:
            item['status'] = 'unresolved_identity'
            continue
        item['record_enumeration'] = {
            'primary': _record_enumeration(previous.get(ids['primary'], {}), 'SIGNAL_COLOC_WITH', gene, disease),
            'gwas': _record_enumeration(previous.get(ids['gwas'], {}), 'PART_OF_GWAS_SIGNAL', variant, disease),
            'qtl': _record_enumeration(previous.get(ids['qtl'], {}), 'PART_OF_QTL_SIGNAL', variant, gene)}
        coloc_edges = _edges(previous.get(ids['primary'], {}), 'SIGNAL_COLOC_WITH', gene, disease)
        gwas_edges = _edges(previous.get(ids['gwas'], {}), 'PART_OF_GWAS_SIGNAL', variant, disease)
        qtl_edges = _edges(previous.get(ids['qtl'], {}), 'PART_OF_QTL_SIGNAL', variant, gene)
        if item['record_enumeration']['primary']:
            item['primary_record_count'] = len(coloc_edges)
            item['linked_record_count'] = 0
        primary_outcome = previous.get(ids['primary'], {})
        item['status'] = 'checked' if (primary_outcome.get('status') in {'complete', 'empty'}
            and item['record_enumeration']['primary']) else 'primary_unverified'
        for edge in coloc_edges:
            props = edge.get('properties', {}); reasons = []; support = []
            if variant in _leads(props.get('qtl_lead_vars')):
                reasons.append('recorded_qtl_lead')
            if variant in _leads(props.get('gwas_lead_vars')):
                reasons.append('recorded_gwas_lead')
            signal = _credible_set(props.get('gwas_signal_id'))
            if signal:
                for member in gwas_edges:
                    if member.get('properties', {}).get('credible_set_id') == signal:
                        reasons.append('verified_gwas_credible_set_member')
                        support.append(_ref(member, ids['gwas']))
            context = QTL_CONTEXT.get(props.get('coloc_dataset'))
            qtl_signal = props.get('qtl_signal_id')
            if context and isinstance(qtl_signal, str) and qtl_signal:
                for member in qtl_edges:
                    values = member.get('properties', {})
                    if (values.get('credible_set') == qtl_signal and values.get('data_source') == context[0]
                            and values.get('tissue_id') == context[1]):
                        reasons.append('verified_qtl_credible_set_member')
                        support.append(_ref(member, ids['qtl']))
            reasons = sorted(set(reasons))
            unique_support = {ref['record_sha256']: ref for ref in support}
            support = [unique_support[k] for k in sorted(unique_support)]
            record = {'primary_reference': _ref(edge, ids['primary']),
                'gwas_signal_id': props.get('gwas_signal_id'), 'qtl_signal_id': props.get('qtl_signal_id'),
                'coloc_dataset': props.get('coloc_dataset'), 'pp_h4_abf': props.get('pp_h4_abf'),
                'linked_to_requested_variant': bool(reasons), 'match_kinds': reasons,
                'supporting_references': support[:MAX_SUPPORTING_REFERENCES],
                'omitted_supporting_reference_count': max(0, len(support) - MAX_SUPPORTING_REFERENCES)}
            if len(item['records']) < MAX_SUMMARY_RECORDS:
                item['records'].append(record)
            item['linked_record_count'] += bool(reasons)
        item['summary_omitted_record_count'] = max(0, len(coloc_edges) - len(item['records']))
    if not summary['groups']:
        summary['status'] = 'not_applicable_or_stale'
    elif any(group.get('status') == 'unresolved_identity' for group in summary['groups']):
        summary['status'] = 'unknown'
    elif any(group.get('status') != 'checked' or not all(group.get('record_enumeration', {}).values()) or any(
            previous.get(identifier, {}).get('status') not in {'complete', 'empty'}
            or previous.get(identifier, {}).get('truncated') is not False
            or previous.get(identifier, {}).get('graph_version') != RELEASE
            for identifier in group['step_ids'].values()) for group in summary['groups']):
        summary['status'] = 'partial'
    else:
        summary['status'] = 'checked'
    if plan.get('computed_operations'):
        summary['computed_operations'] = _computed_outcomes(plan, summary)
    return summary
