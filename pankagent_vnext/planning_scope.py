"""Conservative raw-grounding to structured-plan scope checks.

This is not a second language model, entity resolver, or proof that every natural
language modifier has been compiled. It catches loss of uniquely verified named
anchors and direct tissue scopes. It never adds a filter or changes the question.
Novel/ambiguous wording remains with the planner and the existing scope guards.
"""
import hashlib
import json
from pathlib import Path
import re

from .preplanning_grounding import phrase_tokens
from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST
from .semantic_registry import ALIASES as ASSAY_ALIASES

VERSION = 'grounded-requested-scope-v4'
DIGEST = hashlib.sha256(Path(__file__).read_bytes() + SCHEMA_DIGEST.encode()).hexdigest()
_GENETIC = {'SIGNAL_COLOC_WITH', 'PART_OF_QTL_SIGNAL', 'PART_OF_GWAS_SIGNAL'}
_TISSUE_PATHS = {'PART_OF_QTL_SIGNAL', 'HAS_SAMPLE'}
_INCIDENTAL = {'example', 'examples', 'previous', 'previously', 'unrelated'}


def _kind(value):
    aliases = {'cell_type': 'anatomical_structure', 'tissue': 'anatomical_structure', 'cell': 'anatomical_structure'}
    if value in aliases:
        return aliases[value]
    matches = [label for label in REGISTRY['nodes'] if isinstance(value, str) and label.casefold() == value.casefold()]
    return matches[0] if len(matches) == 1 else value


def _values(constraint):
    operator = str(constraint.get('operator', '=')).upper()
    value = constraint.get('value')
    if operator == 'IN':
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return []
        return value if isinstance(value, list) else []
    return [value] if operator == '=' else []


def _same(value, forms):
    return isinstance(value, str) and phrase_tokens(value) in forms


def _replacement_spans(words, mentions):
    """Recognize only explicit replacements between uniquely grounded peers.

    These instructions authorize changing the named scope without a history
    lookup. Incidental mentions and ambiguous alternatives never do. Only the
    replaced occurrence is excluded; another positive occurrence is retained.
    """
    excluded, targets = set(), set()
    for _, old, _, old_spans in mentions:
        for _, new, _, new_spans in mentions:
            if old['entity_type'] != new['entity_type'] or old['id'] == new['id']:
                continue
            for start, end in old_spans:
                for new_start, new_end in new_spans:
                    before_old = words[max(0, start - 3):start]
                    before_new = words[max(0, new_start - 3):new_start]
                    replace = (before_old and before_old[-1] == 'replace'
                               and words[end:new_start] == ('with',)
                               and not set(before_old[:-1]) & {'not', 'never'})
                    use = (before_new and before_new[-1] == 'use'
                           and words[new_end:start] in {('instead', 'of'), ('rather', 'than')}
                           and not set(before_new[:-1]) & {'not', 'never'})
                    if replace or use:
                        excluded.add((old['id'], start, end))
                        targets.add((new['id'], new_start, new_end))
    return excluded, targets


def _mentions(question, grounding):
    words = phrase_tokens(question)
    output = []
    for mention in grounding.get('mentions', []):
        candidates = mention.get('candidates', [])
        if mention.get('state') != 'resolved' or mention.get('identity_complete') is False or len(candidates) != 1:
            continue
        candidate = candidates[0]
        if candidate.get('entity_type') not in {'Gene', 'variants', 'disease', 'anatomical_structure'} or not candidate.get('id'):
            continue
        requested = phrase_tokens(mention.get('requested', ''))
        if not requested:
            continue
        spans = [(i, i + len(requested)) for i in range(len(words) - len(requested) + 1)
                 if words[i:i + len(requested)] == requested]
        # Grounding from history or a different request cannot authorize a
        # new constraint. Only mentions actually present in this raw input count.
        spans = [(start, end) for start, end in spans if not set(words[max(0, start - 4):start]) & _INCIDENTAL]
        if not spans:
            continue
        forms = {phrase_tokens(value) for value in (mention['requested'], candidate.get('id'), candidate.get('name')) if value}
        output.append((mention, candidate, forms, spans))
    excluded, targets = _replacement_spans(words, output)
    output = [(mention, candidate, forms,
               [span for span in spans if (candidate['id'], *span) not in excluded])
              for mention, candidate, forms, spans in output]
    return words, [item for item in output if item[3]], targets


def _compatible(kind, relation):
    if kind == 'variants' and relation == 'SIGNAL_COLOC_WITH':
        # Pre-compilation coloc plans can carry variant identity before the
        # existing role-aware normalizer splits three independent checks.
        return True
    return any(kind in path['source'] + path['target'] for path in REGISTRY['relations'].get(relation, {}).get('paths', []))


def _identity_present(step, candidate, forms):
    kind = candidate['entity_type']
    for constraint in step.get('constraints', []):
        prop = str(constraint.get('property', '')).split('.')[-1]
        if prop in {'id', 'name'} and _kind(constraint.get('entity_type')) == kind:
            if any(_same(value, forms) for value in _values(constraint)):
                return True
    return False


def _direct_tissue(words, spans):
    for start, end in spans:
        before = list(words[max(0, start - 4):start])
        while before and before[-1] in {'the', 'human', 'a', 'an'}:
            before.pop()
        if before and before[-1] in {'in', 'within', 'from', 'across'}:
            return True
        after = words[end:end + 5]
        if after and (re.fullmatch(r'(?:e|s|exon)?qtls?', after[0]) or after[0] in {'sample', 'samples'}
                      or set(after) & {'sample', 'samples'}):
            return True
    return False


def _tissue_present(step, candidate, forms, relation):
    if _identity_present(step, candidate, forms):
        return True  # The typed compiler verifies anatomy -> QTL tissue_id.
    if relation != 'PART_OF_QTL_SIGNAL':
        return False
    for constraint in step.get('constraints', []):
        if constraint.get('entity_type') or constraint.get('relationship_type') not in (None, relation):
            continue
        prop = constraint.get('property')
        if prop not in {'tissue', 'tissue_id', 'tissue_name'}:
            continue
        if any(_same(value, forms) for value in _values(constraint)):
            return True
    return False


def _shared_qtl_tissue(words, spans, gene_starts):
    """Recognize a tissue on the common QTL phrase before the named genes.

    QTL-family abbreviations are equivalent linguistic roles. A tissue after
    one named gene remains local to that gene; other independent checks do not
    inherit it. Other tissue-bearing phrases (samples, pathways) cannot lend
    their scope to a later QTL phrase.
    """
    if not gene_starts:
        return False
    first_gene = min(gene_starts)
    qtl = r"(?:e|s|exon)?qtls?"
    for start, end in spans:
        if end > first_gene:
            continue
        if end < len(words) and re.fullmatch(qtl, words[end]):
            return True
        prefix = " ".join(words[max(0, start - 7):start])
        if re.search(r"\b" + qtl + r"(?: (?:evidence|signals?|data|associations?|results?|records?)){0,2} (?:in|within|from|across)(?: (?:the|human)){0,2}$", prefix):
            return True
    return False


def _explicit_unrestricted_disease(question, step):
    """Admit a universal disease scope only for its stated evidence role."""
    roles = {
        'PART_OF_GWAS_SIGNAL': r'\bgwas\b',
        'SIGNAL_COLOC_WITH': r'\b(?:coloc|colocalization)\b',
        'EFFECTOR_GENE_OF': r'\beffector\b',
    }
    relations = step.get('relation_types') or []
    if len(relations) != 1 or relations[0] not in roles:
        return False
    pattern = re.compile(r'\b(?:(?:across|for|in)\s+)?(?:all|any)\s+(?:recorded\s+)?(?:diseases|traits)\b|\b(?:without|no)\s+(?:a\s+)?disease\s+(?:filter|restriction)\b', re.I)
    def matches(text):
        for match in pattern.finditer(str(text or '')):
            prefix = phrase_tokens(str(text)[:match.start()])[-4:]
            if not set(prefix) & {'not', 'never', 'exclude', 'excluding'}:
                yield match
    if not list(matches(step.get('question'))):
        return False
    # Nearest explicitly named evidence role in the same short clause binds
    # this universal phrase. A GWAS instruction cannot broaden coloc as well.
    for clause in re.split(r'[.;!?\n]', str(question or '')):
        role_mentions = [(key, match) for key, regex in roles.items() for match in re.finditer(regex, clause, re.I)]
        for match in matches(clause):
            distances = [(min(abs(role.end() - match.start()), abs(role.start() - match.end())), key)
                         for key, role in role_mentions]
            if not distances:
                continue
            distance = min(item[0] for item in distances)
            nearest = {key for dist, key in distances if dist == distance}
            if distance <= 80 and nearest == {relations[0]}:
                return True
    return False


def _assay_scope_issue(question, grounding, plan):
    """Keep exact or excluded assay records scoped to the original request.

    Only recorded modality values and registry aliases are interpreted here.
    This is an admission check, never an alternative assay search or a donor
    subtraction operation. It also checks context steps: the word 'context'
    cannot authorize a query for an explicitly excluded assay.
    """
    vocabulary = grounding.get('sample_terminology', {}).get('modalities')
    if not isinstance(vocabulary, list):
        return None
    recorded = {value for value in vocabulary if isinstance(value, str)}
    key = lambda value: re.sub(r'[^a-z0-9]', '', str(value).lower())
    aliases = {key(value): value for value in recorded}
    aliases.update({alias: value for alias, value in ASSAY_ALIASES.items() if value in recorded})
    words = phrase_tokens(question)
    positive, excluded, exact_assays = set(), set(), set()
    for start in range(len(words)):
        # Longest verified phrase prevents a shorter alias stealing a compound.
        matches = [(end, aliases.get(''.join(words[start:end])))
                   for end in range(start + 1, min(len(words), start + 7) + 1)]
        matches = [(end, value) for end, value in matches if value]
        if not matches:
            continue
        end, assay = max(matches)
        prefix = words[max(0, start - 3):start]
        direct_exclusion = bool(prefix and (prefix[-1] in {'exclude', 'excluding', 'without', 'except', 'no'}
                                or prefix[-2:] in {('do', 'not'), ('not', 'include')}))
        # 'Do not exclude X' is positive, not a negative assay constraint.
        if direct_exclusion and prefix[-1] in {'exclude', 'excluding'} and prefix[-3:-1] == ('do', 'not'):
            direct_exclusion = False
        (excluded if direct_exclusion else positive).add(assay)
        if set(prefix[-2:]) & {'standalone', 'exact', 'exactly', 'only'} or 'only' in words[end:end + 2]:
            exact_assays.add(assay)
    exact = bool(exact_assays - excluded)
    allowed = positive - excluded
    if not excluded and not (exact and allowed):
        return None
    interpreted = str(plan.get('interpreted_question', ''))
    donor_exclusion = r'\bexclud(?:e|ing)\s+donors?\s+(?:who|whose|with|having)\b'
    if re.search(donor_exclusion, str(question), re.I):
        # An explicitly requested donor anti-join can need positive membership
        # evidence for the excluded assay. Its operators/joins are validated
        # downstream; this assay-record guard does not reinterpret that scope.
        return None
    if excluded and re.search(donor_exclusion, interpreted, re.I):
        return ('changed_requested_scope:assay_exclusion_is_not_donor_exclusion: '
                'Exclude the named assay records, not donors who also have other assays. '
                'Keep the original donor and tissue filters; do not add a positive excluded-assay check.')
    for step in plan.get('steps', []):
        if not isinstance(step, dict) or 'HAS_SAMPLE' not in step.get('relation_types', []):
            continue
        values = set()
        for constraint in step.get('constraints', []):
            owner, prop = _kind(constraint.get('entity_type')), str(constraint.get('property', '')).split('.')[-1]
            if not (owner == 'Sample_node' and prop == 'data_modality'
                    or owner == 'data_modality' and prop in {'id', 'name'}):
                continue
            values.update(aliases.get(key(value), str(value)) for value in _values(constraint))
        forbidden = values & excluded
        if forbidden or (exact and allowed and values - allowed):
            return ('unrequested_assay_scope:' + str(step.get('id', 'step')) + ': '
                    'Preserve the original exact assay and exclusions in every sample check, including context checks. '
                    'Do not query excluded or additional assay capabilities to obtain positive evidence. '
                    'An exact empty result is valid; exclude assay records, not donors who also have other assays.')
        if exact and allowed and not values:
            return ('missing_requested_scope:exact_assay:' + str(step.get('id', 'step')) + ': '
                    'Bind the exact requested recorded assay on this sample check; do not leave its modality unrestricted.')
    return None


def scope_issue(question, grounding, plan):
    """Return one precise missing-scope reason for the existing bounded repair."""
    if not isinstance(grounding, dict) or grounding.get('status') != 'ready' or not isinstance(plan, dict):
        return None
    release = grounding.get('identity', {}).get('graph_release') or grounding.get('schema', {}).get('graph_release')
    if release != REGISTRY['release'] or plan.get('clarification') or plan.get('answer_mode'):
        return None
    assay_issue = _assay_scope_issue(question, grounding, plan)
    if assay_issue:
        return assay_issue
    steps = [step for step in plan.get('steps', []) if isinstance(step, dict) and step.get('purpose') != 'context']
    if not steps:
        return None  # The separate structural validator handles empty plans.
    words, mentions, replacement_targets = _mentions(question, grounding)
    genes = {candidate['id'] for _, candidate, _, _ in mentions if candidate['entity_type'] == 'Gene'}
    diseases = {candidate['id'] for _, candidate, _, _ in mentions if candidate['entity_type'] == 'disease'}
    tissues = {candidate['id'] for _, candidate, _, spans in mentions
               if candidate['entity_type'] == 'anatomical_structure' and
               (_direct_tissue(words, spans) or any((candidate['id'], *span) in replacement_targets for span in spans))}
    for mention, candidate, forms, spans in mentions:
        kind = candidate['entity_type']
        if kind == 'anatomical_structure':
            if not (_direct_tissue(words, spans) or any((candidate['id'], *span) in replacement_targets for span in spans)):
                continue
            required = {r for step in steps for r in step.get('relation_types', []) if r in _TISSUE_PATHS}
            gene_starts = [start for _, anchor, _, anchor_spans in mentions
                           if anchor['entity_type'] == 'Gene' for start, _ in anchor_spans]
            # A leading "pancreatic QTL for X and Y" scopes both genes.
            # A tissue attached to one named gene in a longer multi-gene
            # instruction must not be imposed on an unrelated second check.
            shared_tissue = len(genes) <= 1 or _shared_qtl_tissue(words, spans, gene_starts)
            for relation in sorted(required):
                compatible_tissues = [step for step in steps if relation in step.get('relation_types', [])]
                direct = [step for step in compatible_tissues if not step.get('depends_on')]
                if (len(tissues) == 1 and shared_tissue and any(not _tissue_present(step, candidate, forms, relation) for step in direct)
                        or not any(_tissue_present(step, candidate, forms, relation) for step in compatible_tissues)):
                    return 'missing_requested_scope:tissue:' + str(mention['requested']) + ':' + relation
            continue
        compatible = [step for step in steps if any(_compatible(kind, relation)
                      and not (kind == 'disease' and relation in {'HAS_DONOR', 'HAS_SAMPLE'})
                      for relation in step.get('relation_types', []))]
        # T1D differential-expression context is encoded by the edge category,
        # not a disease-node predicate. Donor cohorts have their own stage and
        # clinical-status guards; do not require a disease node for those paths.
        if kind == 'disease' and not compatible:
            continue
        if kind == 'disease' and len(diseases) == 1:
            direct = [step for step in compatible if not step.get('depends_on')]
            for step in direct:
                if not _identity_present(step, candidate, forms) and not _explicit_unrestricted_disease(question, step):
                    return 'missing_requested_scope:disease:' + str(mention['requested']) + ':' + str(step.get('id', 'step'))
            if compatible and not any(_identity_present(step, candidate, forms) or _explicit_unrestricted_disease(question, step) for step in compatible):
                return 'missing_requested_scope:disease:' + str(mention['requested'])
            continue
        if kind == 'Gene' and len(genes) == 1:
            direct = [step for step in compatible if not step.get('depends_on')]
            for step in direct:
                if not _identity_present(step, candidate, forms):
                    return 'missing_requested_scope:Gene:' + str(mention['requested']) + ':' + str(step.get('id', 'step'))
            if compatible and not any(_identity_present(step, candidate, forms) for step in compatible):
                return 'missing_requested_scope:Gene:' + str(mention['requested'])
        elif compatible and not any(_identity_present(step, candidate, forms) for step in compatible):
            return 'missing_requested_scope:' + kind + ':' + str(mention['requested'])
        # Gene/variant questions must not be replaced by a different family
        # merely because an unrelated independently planned step is executable.
        if kind in {'Gene', 'variants'} and not compatible:
            return 'missing_requested_scope:' + kind + ':' + str(mention['requested'])
    return None
