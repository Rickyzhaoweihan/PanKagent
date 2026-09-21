"""Check verified request bindings and narrowly scoped entity replacements.

These checks produce repair feedback; they never rewrite a proposal or turn a
failed search into absence. Ordinary free-form revisions remain with planning.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .preplanning_grounding import phrase_tokens, explicit_non_go_annotation_scope
from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST

VERSION = 'grounded-plan-requirements-v2'
DIGEST = hashlib.sha256(Path(__file__).read_bytes() + SCHEMA_DIGEST.encode()).hexdigest()


def _values(constraint):
    value = constraint.get('value')
    if str(constraint.get('operator', '=')).upper() in {'IN', 'NOT IN'}:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return [value]
        return value if isinstance(value, list) else [value]
    return [value]


def _records(grounding, parent):
    records = {}
    def add(kind, identifier, *forms):
        if kind and identifier:
            record = records.setdefault((kind, identifier), set())
            record.update(phrase_tokens(v) for v in (identifier, *forms) if isinstance(v, str))
    for mention in grounding.get('mentions', []):
        if mention.get('state') == 'resolved' and mention.get('identity_complete') is not False and len(mention.get('candidates', [])) == 1:
            value = mention['candidates'][0]
            add(value.get('entity_type'), value.get('id'), value.get('name'), mention.get('requested'))
    for step in parent.get('steps', []):
        for value in step.get('resolved_entities', []):
            if value.get('state') == 'resolved':
                add(value.get('entity_type'), value.get('id'), value.get('name'))
        for binding in step.get('schema_bindings', []):
            tissue = binding.get('resolved_tissue') or {}
            canonical = binding.get('canonical_binding') or {}
            if (binding.get('kind') == 'verified_qtl_tissue_property'
                    and binding.get('graph_version') == REGISTRY['release']
                    and canonical.get('relationship_type') == 'PART_OF_QTL_SIGNAL'
                    and canonical.get('property') == 'tissue_id'
                    and canonical.get('value') == tissue.get('id')
                    and 'anatomical_structure' in tissue.get('labels', [])):
                add('anatomical_structure', tissue.get('id'), tissue.get('name'))
    return records


def _identity(value, kind, records):
    forms = phrase_tokens(value) if isinstance(value, str) else ()
    matches = [identifier for (label, identifier), aliases in records.items() if label == kind and forms in aliases]
    return matches[0] if len(matches) == 1 else None


def _replacement(instruction, records):
    # Only a full replacement instruction with an optional preservation clause
    # authorizes this strict unchanged-scope check. Adding/removing other scope
    # makes this an ordinary revision instead.
    text = str(instruction or '').strip().rstrip('.!?')
    text = re.sub(r'^please\s+', '', text, flags=re.I)
    parts = re.split(r'(?:[,;]\s*|\s+)(?:and\s+)?(?:keep(?:ing)?|retain(?:ing)?|preserv(?:e|ing))\b', text, maxsplit=1, flags=re.I)
    if len(parts) > 1 and re.search(r'\b(?:add(?:ing)?|remov(?:e|ing)|chang(?:e|ing)|replac(?:e|ing)|exclud(?:e|ing)|includ(?:e|ing)|broaden(?:ing)?|narrow(?:ing)?|instead)\b', parts[1], re.I):
        return None
    text = parts[0].strip()
    match = re.fullmatch(r'replace\s+(.+?)\s+with\s+(.+)', text, re.I)
    if match:
        old, new = match.groups()
    else:
        match = re.fullmatch(r'use\s+(.+?)\s+instead\s+of\s+(.+)', text, re.I)
        if not match:
            return None
        new, old = match.groups()
    def resolve(text):
        forms = phrase_tokens(text)
        choices = []
        for (kind, identifier), aliases in records.items():
            if forms in aliases or (forms and forms[0] in {'gene', 'variant', 'snp', 'tissue', 'disease'} and forms[1:] in aliases):
                choices.append((kind, identifier))
        return choices[0] if len(choices) == 1 else None
    old, new = resolve(old), resolve(new)
    return (old, new) if old and new and old != new and old[0] == new[0] else None


def _constraint_key(constraint, records, replacement=None):
    owner = constraint.get('entity_type') or constraint.get('relationship_type') or ''
    prop = constraint.get('property')
    operator = str(constraint.get('operator', '=')).upper()
    operator = '!=' if operator == '<>' else operator
    values = _values(constraint)
    if len(values) == 1 and operator in {'IN', 'NOT IN'}:
        operator = '=' if operator == 'IN' else '!='
    identity_kind = owner if prop in {'id', 'name'} else None
    if owner == 'PART_OF_QTL_SIGNAL' and prop in {'tissue_id', 'tissue_name'}:
        identity_kind = 'anatomical_structure'
    identifiers = [_identity(value, identity_kind, records) for value in values] if identity_kind else []
    if identifiers and all(identifier is not None for identifier in identifiers):
        prop = 'tissue_identity' if owner == 'PART_OF_QTL_SIGNAL' else 'identity'
        if replacement:
            old, new = replacement
            identifiers = [new[1] if (identity_kind, identifier) == old else identifier for identifier in identifiers]
        values = identifiers
    serialized = [json.dumps(value, sort_keys=True, ensure_ascii=False) for value in values]
    return owner, prop, operator, tuple(sorted(serialized)) if operator in {'IN', 'NOT IN'} else tuple(serialized)


def _replacement_issue(grounding, plan, history):
    revision = next((item.get('revision_context') for item in reversed(history or [])
                     if isinstance(item, dict) and isinstance(item.get('revision_context'), dict)), None)
    if not revision:
        return None
    parent = revision.get('parent_plan') or {}
    records = _records(grounding, parent)
    replacement = _replacement(revision.get('instruction'), records)
    if not replacement:
        return None
    def generated_helper(step):
        proof = step.get('application_generated_context') or {}
        return (proof.get('kind') == 'related_context_step' and proof.get('version') == 1
                and proof.get('source_step_id') == step.get('context_for')
                and step.get('purpose') == 'context' and step.get('complete') is False)
    old_steps = [step for step in parent.get('steps', []) if not generated_helper(step)]
    new_steps = plan.get('steps', [])
    if len(old_steps) != len(new_steps):
        return 'revision_checks_not_preserved:identity_replacement_only'
    used, mapping = set(), {}
    for old in old_steps:
        expected = Counter(_constraint_key(c, records, replacement) for c in old.get('constraints', []))
        candidates = [new for new in new_steps if new.get('id') not in used
                      and set(new.get('relation_types', [])) == set(old.get('relation_types', []))]
        candidates.sort(key=lambda new: new.get('id') != old.get('id'))
        match = next((new for new in candidates if Counter(_constraint_key(c, records) for c in new.get('constraints', [])) == expected
                      and new.get('complete', True) == old.get('complete', True)
                      and new.get('evidence_combination', 'independent') == old.get('evidence_combination', 'independent')
                      and (new.get('purpose') or 'primary') == (old.get('purpose') or 'primary')
                      and bool(new.get('context_for') or new.get('context_kind')) == bool(old.get('context_for') or old.get('context_kind'))), None)
        if match is None:
            actual = Counter(_constraint_key(c, records) for c in candidates[0].get('constraints', [])) if candidates else Counter()
            missing = expected - actual
            if missing:
                owner, prop, _, _ = next(iter(missing))
                return f'revision_constraint_not_preserved:{old.get("id", "step")}:{owner}.{prop}'
            return f'revision_category_or_completeness_changed:{old.get("id", "step")}'
        used.add(match.get('id'))
        mapping[old.get('id')] = match.get('id')
    by_id = {step.get('id'): step for step in new_steps}
    for old in old_steps:
        if {mapping.get(value, value) for value in old.get('depends_on', [])} != set(by_id[mapping[old.get('id')]].get('depends_on', [])):
            return f'revision_dependencies_not_preserved:{old.get("id", "step")}'
    return None


def _domain_requirements(question, grounding, domains, records):
    """Bind domains to explicit gene/evidence clauses, never nearest genes.

    A single shared evidence phrase may own several genes. Two complete
    evidence clauses instead keep their own domains, including an unrestricted
    GO clause. Interleaved terms without a clear clause/shared phrase fail with
    repair feedback rather than silently crossing or extending filters.
    """
    gene_forms = {identifier: forms for (kind, identifier), forms in records.items() if kind == 'Gene'}
    domain_forms = {value: {phrase_tokens(value)} for value in domains}
    for mention in grounding.get('mentions', []):
        role = mention.get('context_role') or {}
        binding = role.get('canonical_binding') or {}
        value = binding.get('value')
        if value in domain_forms and role.get('kind') == 'ontology_domain' and mention.get('requested'):
            domain_forms[value].add(phrase_tokens(mention['requested']))

    def spans(words, forms):
        return [(i, i + len(form)) for form in forms if form
                for i in range(len(words) - len(form) + 1) if words[i:i + len(form)] == form]

    def inspect(text):
        words = phrase_tokens(text)
        genes = {key: found for key, forms in gene_forms.items() if (found := spans(words, forms))}
        values = {key: found for key, forms in domain_forms.items() if (found := spans(words, forms))}
        go = bool(values or 'go' in words or ('gene', 'ontology') in zip(words, words[1:]))
        evidence = go or bool(set(words) & {
            'qtl', 'eqtl', 'sqtl', 'gwas', 'coloc', 'colocalization', 'pathway', 'pathways',
            'reactome', 'kegg', 'marker', 'markers', 'expression', 'detected', 'enriched',
            'donor', 'donors', 'sample', 'samples'})
        return genes, values, go, evidence

    def split(text):
        for separator in re.finditer(r'[;.!?\n]|,|\b(?:and|while|whereas)\b', text, re.I):
            left, right = text[:separator.start()], text[separator.end():]
            lg, _, _, le = inspect(left)
            rg, _, _, revidence = inspect(right)
            if lg and le and rg and revidence:
                return split(left) + split(right)
        return [text]

    expected = {}
    for clause in split(str(question)):
        genes, values, go, _ = inspect(clause)
        if not go or not genes:
            continue
        if values and len(genes) > 1:
            gene_spans = [span for found in genes.values() for span in found]
            domain_spans = [span for found in values.values() for span in found]
            shared_prefix = max(end for _, end in domain_spans) <= min(start for start, _ in gene_spans)
            shared_suffix = max(end for _, end in gene_spans) <= min(start for start, _ in domain_spans)
            if not shared_prefix and not shared_suffix:
                return {}, 'ambiguous_requested_category_scope:GO_term.go_domain:separate_each_gene_scope'
        wanted = frozenset(values)
        for identifier in genes:
            if identifier in expected and expected[identifier] != wanted:
                return {}, 'ambiguous_requested_category_scope:GO_term.go_domain:repeated_gene_scope'
            expected[identifier] = wanted
    return expected, None


def _domain_issue(question, grounding, plan, domains, history):
    go_steps = [step for step in plan.get('steps', []) if 'ASSOCIATED_WITH_GO' in step.get('relation_types', [])
                and step.get('purpose') != 'context' and not step.get('context_for') and not step.get('context_kind')]
    revision = next((item.get('revision_context') for item in reversed(history or [])
                     if isinstance(item, dict) and isinstance(item.get('revision_context'), dict)), {})
    records = _records(grounding, revision.get('parent_plan') or {})
    # Replacement-only revisions retain the exact per-step parent filters in
    # _replacement_issue; the old and new names here are not two query roles.
    if _replacement(revision.get('instruction'), records):
        return None
    expected, issue = _domain_requirements(question, grounding, domains, records)
    if issue:
        return issue
    if not go_steps:
        return 'missing_requested_category:ASSOCIATED_WITH_GO'
    if not expected:
        # Unresolved identities are handled elsewhere. A verified domain on a
        # one-scope request must still survive until resolution succeeds.
        expected = {None: frozenset(domains)}
    observed = {identifier: set() for identifier in expected}
    checked = set()
    for step in go_steps:
        gene_predicates, domain_predicates, excluded_domains = [], [], set()
        has_domain_filter = False
        for constraint in step.get('constraints', []):
            if constraint.get('entity_type') == 'Gene' and constraint.get('property') in {'id', 'name'} and str(constraint.get('operator', '=')).upper() in {'=', 'IN'}:
                gene_predicates.append({identifier for value in _values(constraint)
                                        if (identifier := _identity(value, 'Gene', records))})
            if constraint.get('entity_type') == 'GO_term' and constraint.get('property') == 'go_domain':
                has_domain_filter = True
                if str(constraint.get('operator', '=')).upper() in {'=', 'IN'}:
                    domain_predicates.append({value for value in _values(constraint) if isinstance(value, str)})
                elif str(constraint.get('operator', '=')).upper() in {'!=', '<>', 'NOT IN'}:
                    excluded_domains.update(value for value in _values(constraint) if isinstance(value, str))
        identifiers = set.intersection(*gene_predicates) if gene_predicates else set()
        values = set.intersection(*domain_predicates) if domain_predicates else set()
        values -= excluded_domains
        if None in expected:
            identifiers = {None}
        if not identifiers or any(identifier not in expected for identifier in identifiers):
            return f'missing_requested_category_scope:{step.get("id", "step")}:Gene'
        for identifier in identifiers:
            wanted = expected[identifier]
            if ((wanted and (not values or not values <= wanted))
                    or (not wanted and has_domain_filter)):
                detail = '|'.join(sorted(wanted)) or 'unrestricted'
                return f'missing_requested_category_binding:{step.get("id", "step")}:GO_term.go_domain:{detail}'
            observed[identifier].update(values)
            checked.add(identifier)
    for identifier, wanted in expected.items():
        if identifier not in checked or observed[identifier] != wanted:
            detail = '|'.join(sorted(wanted - observed[identifier])) or 'unrestricted'
            return f'missing_requested_category_binding:GO_term.go_domain:{identifier or "scope"}:{detail}'
    return None


def requirements_issue(question, grounding, plan, history=None):
    if (not isinstance(grounding, dict) or grounding.get('status') != 'ready'
            or grounding.get('identity', {}).get('graph_release') != REGISTRY['release']):
        return None
    steps = plan.get('steps', [])
    if not steps and plan.get('clarification'):
        return None
    domains = set()
    recorded = grounding.get('schema', {}).get('categories', {}).get('GO_term.go_domain', [])
    for mention in grounding.get('mentions', []):
        role = mention.get('context_role') or {}
        binding = role.get('canonical_binding') or {}
        if (role.get('kind') == 'ontology_domain' and role.get('resolution_state') == 'resolved'
                and binding.get('entity_type') == 'GO_term' and binding.get('property') == 'go_domain'
                and binding.get('value') in recorded):
            domains.add(binding['value'])
    if domains:
        issue = _domain_issue(question, grounding, plan, domains, history)
        if issue:
            return issue
    if explicit_non_go_annotation_scope(question) and any('ASSOCIATED_WITH_GO' in step.get('relation_types', []) for step in steps):
        return 'unrequested_evidence_category:ASSOCIATED_WITH_GO:explicit_pathway_or_marker_annotation_scope'
    source_issue = _pathway_source_issue(question, grounding, plan)
    if source_issue:
        return source_issue
    return _replacement_issue(grounding, plan, history)


def _pathway_sources(question, grounding):
    """Return explicit per-gene resource roles, backed by recorded categories."""
    available = grounding.get('schema', {}).get('categories', {}).get('FUNCTION_ANNOTATION.data_source', [])
    # Resource names must also be typed endpoints in this release. A label alone
    # never establishes the spelling/value of a data_source property.
    sources = {value for value in available if isinstance(value, str)
               and value.casefold() in {'reactome', 'kegg'}
               and any(value.casefold() in path['target'] for path in REGISTRY['relations']['FUNCTION_ANNOTATION']['paths'])}
    words = phrase_tokens(question)
    requested_names = set(words) & {'reactome', 'kegg'}
    if not requested_names:
        return {}, None
    if not requested_names <= {value.casefold() for value in sources}:
        return {}, 'requested_source_inventory_unavailable:FUNCTION_ANNOTATION.data_source'
    records = _records(grounding, {})
    gene_forms = {identifier: forms for (kind, identifier), forms in records.items() if kind == 'Gene'}

    def inspect(text):
        tokens = phrase_tokens(text)
        genes = {identifier for identifier, forms in gene_forms.items()
                 if any(tokens[i:i + len(form)] == form for form in forms if form
                        for i in range(len(tokens) - len(form) + 1))}
        named = {value for value in sources if value.casefold() in tokens}
        evidence = bool(named or set(tokens) & {'go', 'pathway', 'pathways', 'qtl', 'eqtl', 'sqtl', 'marker', 'markers'})
        return genes, named, evidence

    def clauses(text):
        for separator in re.finditer(r'[;.!?\n]|,|\b(?:and|while|whereas)\b', text, re.I):
            left, right = text[:separator.start()], text[separator.end():]
            lg, _, le = inspect(left)
            rg, _, revidence = inspect(right)
            if lg and le and rg and revidence:
                return clauses(left) + clauses(right)
        return [text]

    expected = {}
    for clause in clauses(str(question)):
        genes, named, _ = inspect(clause)
        if not named:
            continue
        if re.search(r'\b(?:not|except|excluding|without|other than)\b', clause, re.I):
            return {}, 'ambiguous_requested_source_scope:preserve_explicit_source_exclusion'
        if not genes:
            continue  # Unresolved identities cannot authorize a new predicate.
        for gene in genes:
            if gene in expected and expected[gene] != frozenset(named):
                return {}, 'ambiguous_requested_source_scope:separate_each_gene_source'
            expected[gene] = frozenset(named)
    return expected, None


def _step_gene_ids(step, grounding):
    records = _records(grounding, {})
    predicates = [{identifier for value in _values(c)
                   if (identifier := _identity(value, 'Gene', records))}
                  for c in step.get('constraints', []) if c.get('entity_type') == 'Gene'
                  and c.get('property') in {'id', 'name'} and c.get('operator', '=') in {'=', 'IN'}]
    return set.intersection(*predicates) if predicates else set()


def _source_predicates(step):
    return [c for c in step.get('constraints', []) if c.get('property') == 'data_source'
            and (not c.get('entity_type') and c.get('relationship_type') in {None, 'FUNCTION_ANNOTATION'}
                 or c.get('entity_type') in {'reactome', 'kegg', 'ontology'})]


def _pathway_source_issue(question, grounding, plan):
    expected, issue = _pathway_sources(question, grounding)
    if issue or not expected:
        return issue
    observed = {gene: set() for gene in expected}
    for step in plan.get('steps', []):
        if 'FUNCTION_ANNOTATION' not in step.get('relation_types', []):
            continue
        ids = _step_gene_ids(step, grounding) & expected.keys()
        if not ids:
            continue
        predicates = _source_predicates(step)
        positive = [set(_values(c)) for c in predicates if c.get('operator', '=') in {'=', 'IN'}]
        actual = set.intersection(*positive) if positive else set()
        excluded = {value for c in predicates if c.get('operator') in {'!=', '<>', 'NOT IN'} for value in _values(c)}
        actual -= excluded
        for gene in ids:
            if not actual or not actual <= expected[gene]:
                return f'missing_requested_source_scope:{step.get("id", "step")}:FUNCTION_ANNOTATION.data_source:{"|".join(sorted(expected[gene]))}'
            observed[gene].update(actual)
    if any(observed[gene] != wanted for gene, wanted in expected.items()):
        return 'missing_requested_source_scope:FUNCTION_ANNOTATION.data_source'
    return None


def compile_requested_scope(question, grounding, plan):
    """Fill only explicit, uniquely grounded scopes before admission.

    Original predicates and exact raw wording remain in a compile audit. This
    can remove an unrequested alternative from a positive IN predicate; it
    never removes a negative constraint or unrelated scientific filter.
    """
    result = deepcopy(plan)
    if (not isinstance(grounding, dict) or grounding.get('status') != 'ready'
            or grounding.get('identity', {}).get('graph_release') != REGISTRY['release']
            or result.get('clarification')):
        return result, None

    def bind(step, predicate, existing, proof):
        if any(c.get('operator', '=') not in {'=', 'IN'} for c in existing):
            return 'conflicting_requested_scope:' + predicate['property']
        if len(existing) == 1 and existing[0] == predicate:
            return None
        original = deepcopy(existing)
        step['constraints'] = [c for c in step.get('constraints', []) if c not in existing] + [predicate]
        step.setdefault('requested_scope_compilation', []).append({
            'version': VERSION, 'graph_release': REGISTRY['release'], 'schema_digest': SCHEMA_DIGEST,
            'raw_question': question, 'original_predicates': original,
            'canonical_binding': deepcopy(predicate), 'proof': proof})
        return None

    sources, issue = _pathway_sources(question, grounding)
    if issue:
        return result, issue
    for step in result.get('steps', []):
        if 'FUNCTION_ANNOTATION' not in step.get('relation_types', []):
            continue
        ids = _step_gene_ids(step, grounding) & sources.keys()
        if not ids:
            continue
        scopes = {sources[gene] for gene in ids}
        if len(scopes) != 1 or step.get('relation_types') != ['FUNCTION_ANNOTATION']:
            return result, 'ambiguous_requested_source_scope:separate_annotation_checks'
        wanted = sorted(scopes.pop())
        predicate = {'property': 'data_source', 'entity_type': None, 'relationship_type': 'FUNCTION_ANNOTATION',
                     'owner_kind': 'relationship', 'operator': '=' if len(wanted) == 1 else 'IN',
                     'value': wanted[0] if len(wanted) == 1 else json.dumps(wanted)}
        issue = bind(step, predicate, _source_predicates(step),
                     {'kind': 'explicit_recorded_pathway_source', 'gene_ids': sorted(ids), 'recorded_values': wanted})
        if issue:
            return result, issue

    from .planning_scope import _mentions, _direct_tissue, _shared_qtl_tissue
    words, mentions, _ = _mentions(question, grounding)
    # A uniquely grounded disease applies to every compatible requested check,
    # unless the user explicitly gives that evidence role unrestricted scope.
    # Donor stage/diagnosis paths are deliberately excluded.
    from .planning_scope import _compatible, _explicit_unrestricted_disease, _identity_present
    diseases = {candidate['id']:(candidate, forms) for _,candidate,forms,_ in mentions
                if candidate['entity_type'] == 'disease'}
    if len(diseases) == 1:
        candidate, forms = next(iter(diseases.values()))
        for step in result.get('steps', []):
            relations = step.get('relation_types', [])
            if (not any(_compatible('disease', r) for r in relations)
                    or set(relations) & {'HAS_DONOR','HAS_SAMPLE'}
                    or _explicit_unrestricted_disease(question, step)):
                continue
            existing = [c for c in step.get('constraints', []) if c.get('entity_type') == 'disease']
            if existing:
                if not _identity_present(step, candidate, forms):
                    return result, 'conflicting_requested_scope:disease'
                continue
            predicate = {'property':'id', 'entity_type':'disease', 'owner_kind':'node',
                         'operator':'=', 'value':candidate['id']}
            issue = bind(step, predicate, [], {'kind':'explicit_unique_disease', 'resolved_disease':deepcopy(candidate)})
            if issue:
                return result, issue
    tissues = [(candidate, spans) for _, candidate, _, spans in mentions
               if candidate['entity_type'] == 'anatomical_structure' and _direct_tissue(words, spans)]
    unique = {candidate['id']: (candidate, spans) for candidate, spans in tissues}
    gene_starts = [start for _, c, _, spans in mentions if c['entity_type'] == 'Gene' for start, _ in spans]
    gene_ids = {c['id'] for _, c, _, _ in mentions if c['entity_type'] == 'Gene'}
    if len(unique) != 1 or not gene_ids or not re.search(r'\b(?:e|s|exon)?qtls?\b', question, re.I):
        return result, None
    candidate, spans = next(iter(unique.values()))
    if (candidate['id'] not in REGISTRY['categories'].get('PART_OF_QTL_SIGNAL.tissue_id', [])
            or len(gene_ids) > 1 and not _shared_qtl_tissue(words, spans, gene_starts)
            or re.search(r'\b(?:not|except|excluding|without|other than)\b', question, re.I)):
        return result, None
    for step in result.get('steps', []):
        if (step.get('relation_types') != ['PART_OF_QTL_SIGNAL'] or step.get('depends_on')
                or not _step_gene_ids(step, grounding) & gene_ids):
            continue
        existing = [c for c in step.get('constraints', [])
                    if (not c.get('entity_type') and c.get('property') in {'tissue', 'tissue_id', 'tissue_name'}
                        and c.get('relationship_type') in {None, 'PART_OF_QTL_SIGNAL'})
                    or (c.get('entity_type') == 'anatomical_structure' and c.get('property') in {'id', 'name'})]
        predicate = {'property': 'tissue_id', 'entity_type': None, 'relationship_type': 'PART_OF_QTL_SIGNAL',
                     'owner_kind': 'relationship', 'operator': '=', 'value': candidate['id']}
        issue = bind(step, predicate, existing, {'kind': 'explicit_unique_qtl_tissue', 'resolved_tissue': deepcopy(candidate)})
        if issue:
            return result, issue
    return result, None
