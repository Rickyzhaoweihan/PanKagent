"""Small purpose-specific request parsers over verified schema identities.

No stored questions, answers, reference queries, or inferred semantic neighbors.
Unsupported modifiers deliberately return None for the general planner. These
drafts still undergo scope, schema, query and post-retrieval model verification.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import re

from .preplanning_grounding import phrase_tokens
from .release_schema import REGISTRY
from .pattern_planning import _identity

VERSION = 'schema-purpose-drafts-v3'
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_COMMON = set('''a an the for of in from to with and or does do is are has have
 show find list get what which whether recorded evidence records data available
 all each both separate separately keep gene genes please it its how many number
 count total current currently now original labels names label name explain
 report sources source me give build short profile overview where types these
 supports support any this that context check include including documented
'''.split())
_SAMPLE = set('''donor donors sample samples cohort cohorts hpap stage t1d
 single cell cells nucleus nuclear rna seq scrnaseq scrna snrna snrnaseq
 components component multiome multiomics snmultiomics assays assay standalone
 only exclude excluding without exact recorded available
'''.split())
_GENE = set('''cell cells type types detected detection expressed expression
 enriched enrichment marker annotations annotation effector physical interaction
 partners partner differential t1d support pathway pathways reactome kegg
 gene insights evidence function genetic
'''.split())


def _separate_assay_counts(question):
    """Recognize an explicit independent-count instruction, not donor overlap."""
    directive = re.search(
        r'\b(?:count\s+donors|(?:report|show)\s+donor\s+counts)\s+separately\s+for\s+each\s+assay\b',
        question, re.I)
    if not directive:
        return None
    suffix = question[directive.end():]
    if not re.fullmatch(r'[\s.;,]*(?:do\s+not\s+require\s+both\s+assays?[\s.;,]*)?', suffix, re.I):
        return None
    request = question[:directive.start()]
    if (not re.search(r'\b(?:compare|count|number|counts)\b', request, re.I)
            or re.search(r'\b(?:not|no|only|exclude|excluding|without|except|both|same|'
                         r'paired|joint|include|including|components?)\b', request, re.I)):
        return None
    return [directive.group(), suffix.strip(' .;,\n\t')]


def _literal_present(question, value):
    tokens, phrase = phrase_tokens(question), phrase_tokens(value)
    return bool(phrase) and any(tuple(tokens[start:start + len(phrase)]) == tuple(phrase)
                               for start in range(len(tokens) - len(phrase) + 1))


def _parse(question, grounding, vocabulary, recorded_literals=()):
    words = list(phrase_tokens(question))
    remaining = list(words)
    found = {}
    for mention in grounding.get('mentions', []):
        if mention.get('state') != 'resolved' or len(mention.get('candidates', [])) != 1:
            if mention.get('state') in {'ambiguous', 'qualified', 'not_found'}:
                return None
            continue
        candidate = mention['candidates'][0]
        if mention.get('identity_complete') is False:
            return None
        if candidate.get('entity_type') not in {'Gene', 'anatomical_structure', 'disease'}:
            return None
        found.setdefault(candidate['entity_type'], {})[candidate['id']] = candidate
        span = mention.get('normalized_token_span')
        if not span or len(span) != 2:
            return None
        remaining[span[0]:span[1]] = [''] * (span[1] - span[0])
    # Admit complete recorded assay/source phrases, not an expanded bag of
    # component words which could accidentally recognize an unknown assay.
    for literal in recorded_literals:
        tokens = phrase_tokens(literal)
        if not tokens:
            continue
        for start in range(len(words) - len(tokens) + 1):
            if tuple(words[start:start + len(tokens)]) == tuple(tokens):
                remaining[start:start + len(tokens)] = [''] * len(tokens)
    # Stage numbers are authorized only by an explicit stage-number phrase.
    for index, word in enumerate(words[:-1]):
        if word == 'stage' and re.fullmatch(r'\d+|i|ii|iii', words[index + 1]):
            remaining[index + 1] = ''
    for index, word in enumerate(words):
        if re.fullmatch(r'stage(?:\d+|iii|ii|i)', word):
            remaining[index] = ''
    if any(word and word not in _COMMON | vocabulary for word in remaining):
        return None
    return {kind: list(values.values()) for kind, values in found.items()}


def compile_schema_draft(question, grounding, history=None):
    if (history or not grounding or grounding.get('status') != 'ready'
            or grounding.get('identity', {}).get('graph_release') != REGISTRY['release']
            or re.search(r'[<>!=]|\b(?:instead|replace|except|most|top|first|before|after|between|paired|joint)\b', question, re.I)):
        return None
    samples = bool(re.search(r'\bdonors?\b|\bsamples?\b', question, re.I))
    separate_counts = _separate_assay_counts(question) if samples else None
    vocabulary = grounding.get('sample_terminology') if samples else None
    literals = []
    if samples:
        if not vocabulary or not vocabulary.get('inventory_complete'):
            return None
        from .semantic_registry import ALIASES
        recorded = vocabulary.get('modalities', [])
        literals = list(recorded) + list(vocabulary.get('sources', []))
        literals += [alias for alias, canonical in ALIASES.items() if canonical in recorded]
        if separate_counts:
            literals += separate_counts
    words = _SAMPLE | {'compare', 'against', 'counts'} if separate_counts else _SAMPLE if samples else _GENE
    entities = _parse(question, grounding, words, literals)
    if not entities:
        return None
    genes, tissues, diseases = (entities.get(kind, []) for kind in ('Gene', 'anatomical_structure', 'disease'))
    if samples:
        if genes or len(tissues) > 1 or len(diseases) > 1:
            return None
        sources = [value for value in vocabulary.get('sources', []) if _literal_present(question, value)]
        if len(sources) > 1:
            # Independent cohorts cannot be collapsed into one overwritten
            # scalar source predicate by the single-scope draft.
            return None
        if re.search(r'\b(?:exclude|excluding|without)\s+(?:all\s+|any\s+)?donors?\b'
                     r'|\bdonors?\s+(?:who\s+)?(?:without|with\s+no|do\s+not\s+have)\b'
                     r'|\b(?:who|that)\b[^.;!?]{0,50}\b(?:without|no|never|not)\b', question, re.I):
            # A donor anti-existence request cannot become a per-sample !=.
            return None
        if (re.search(r'\b(?:including|include)\b.*\b(?:components?|multiome)\b', question, re.I)
                and re.search(r'\bHPAP\b', question, re.I)
                and not re.search(r'\bHPAP\s+(?:donors?|cohort)\b', question, re.I)):
            # Dataset-qualified capability expansion is not necessarily a
            # restriction on every standalone assay; let the general planner
            # separate these roles instead of narrowing the whole cohort.
            return None
        from .semantic_registry import _mentioned_assays, _without_negated_assays
        has_samples = bool(re.search(r'\bsamples?\b|\bassays?\b', question, re.I))
        named_assays = _mentioned_assays(question, vocabulary.get('modalities', []))
        if separate_counts:
            if len(named_assays) != 2 or len(tissues) != 1 or len(sources) != 1:
                return None
            has_samples = True
        if not has_samples and named_assays:
            positive = _mentioned_assays(_without_negated_assays(question, vocabulary['modalities']),
                                         vocabulary['modalities'])
            if (len(positive) != 1
                    or re.search(r'\bonly\b', question, re.I) and not re.search(r'\bstandalone\b', question, re.I)):
                # Multiple assay roles and donor-only exclusivity need a plan
                # with explicit ownership. A single recorded positive assay
                # supplies a concrete sample witness, even without that noun.
                return None
            has_samples = True
        kinds = ['HAS_SAMPLE'] if has_samples else ['HAS_DONOR']
        step = {'id': 'samples' if has_samples else 'donors', 'question': question,
                'relation_types': kinds, 'constraints': [_identity(item) for item in tissues + diseases],
                'depends_on': [], 'complete': True, 'evidence_combination': 'independent',
                'semantic_request': {'source': 'user_request', 'question': question, 'revision_instruction': ''}}
        from .semantic_registry import resolve
        step = resolve(step, vocabulary, REGISTRY['release'])
        if step.get('semantic_issues') or step.get('recovery'):
            return None
        positive_assays = _mentioned_assays(_without_negated_assays(question, vocabulary['modalities']),
                                           vocabulary['modalities'])
        if (not separate_counts and step.get('semantic_registry', {}).get('donor_required')
                and (len(positive_assays) > 1 or step.get('sample_requirements', {}).get('separate_bindings'))):
            # For donor questions, two assay names may mean a union, two
            # independent counts or an intersection on the same donor. Even
            # an explicit OR remains on the general path until its clause
            # ownership is separately proved; never choose a union here.
            return None
        steps = [step]
        if separate_counts:
            # The explicit independence instruction permits replacing only the
            # multi-assay binding with one exact label per check. Every other
            # verified scalar constraint, including stage and cohort, survives.
            shared = [deepcopy(c) for c in step['constraints'] if c.get('property') != 'data_modality']
            steps = []
            for index, assay in enumerate(named_assays, 1):
                exact = {'entity_type': 'Sample_node', 'property': 'data_modality',
                         'operator': '=', 'value': assay}
                check = {'id': 'assay' + str(index),
                    'question': f'Count donors with samples labeled with the {assay} assay.',
                    'relation_types': ['HAS_SAMPLE'], 'constraints': deepcopy(shared) + [exact],
                    'depends_on': [], 'complete': True, 'evidence_combination': 'independent',
                    'semantic_request': {'source': 'user_request', 'question': question, 'revision_instruction': ''},
                    'schema_draft_compilation': {'operation': 'independent_exact_assay_count',
                        'requested': question, 'shared_constraints': deepcopy(shared),
                        'assay_constraint': deepcopy(exact), 'instruction': separate_counts[0]}}
                check = resolve(check, vocabulary, REGISTRY['release'])
                if (check.get('semantic_issues') or check.get('recovery')
                        or check.get('sample_requirements', {}).get('modality_groups') != [[assay]]
                        or check.get('sample_requirements', {}).get('separate_bindings')):
                    return None
                steps.append(check)
    else:
        # Single anchored gene with independent evidence categories. Per-gene
        # mixed category roles and relational partner discovery remain general.
        if len(genes) != 1 or len(diseases) > 1 or len(tissues) > 1:
            return None
        if re.search(r'\b(?:not|no|only|exclude|without|specific|specifically|exclusive|between)\b', question, re.I):
            return None
        mapping = [
            ('GENE_DETECTED_IN', r'\b(?:detected|detection|expressed|expression)\b', 'detection'),
            ('GENE_ENRICHED_IN', r'\b(?:enriched|enrichment)\b', 'gene enrichment'),
            ('MARKER_GENE_OF', r'\bmarker\b', 'marker-cell annotations'),
            ('EFFECTOR_GENE_OF', r'\beffector\b', 'effector-gene support'),
            ('PHYSICAL_INTERACTION', r'\bphysical interaction\b', 'physical interaction partners'),
            ('FUNCTION_ANNOTATION', r'\b(?:pathways?|reactome|kegg)\b', 'pathway annotations'),
        ]
        if re.search(r'\bdifferential\b|\bgenetic interaction\b', question, re.I):
            return None
        selected = [(kind, wording) for kind, expression, wording in mapping if re.search(expression, question, re.I)]
        if not selected:
            return None
        if len(selected) > 1 and (diseases or re.search(r'\b(?:where|both)\b', question, re.I)):
            # Disease/condition ownership and same-cell conjunction cannot be
            # inferred from a bag of category names. General planning retains
            # these per-clause roles instead of dropping or broadcasting them.
            return None
        # A tissue and several category roles need explicit per-role scoping.
        if tissues and len(selected) > 1:
            return None
        steps = []
        for index, (kind, wording) in enumerate(selected, 1):
            anchors = [genes[0]]
            if diseases:
                if kind == 'EFFECTOR_GENE_OF':
                    anchors += diseases
                elif len(selected) == 1:
                    return None
            if tissues:
                if kind not in {'GENE_DETECTED_IN', 'GENE_ENRICHED_IN', 'MARKER_GENE_OF'}:
                    return None
                anchors += tissues
            text = f"Show all recorded {wording} for {genes[0]['name']}"
            if tissues:
                text += f" in {tissues[0]['name']}"
            if diseases and kind == 'EFFECTOR_GENE_OF':
                text += f" for {diseases[0]['name']}"
            if kind == 'FUNCTION_ANNOTATION':
                sources = [source for source in ('Reactome', 'KEGG') if re.search(r'\b'+source+r'\b', question, re.I)]
                if sources:
                    text += ' from ' + ' and '.join(sources)
            steps.append({'id': 's'+str(index), 'question': text+'.', 'relation_types': [kind],
                          'constraints': [_identity(item) for item in anchors], 'depends_on': [],
                          'complete': True, 'evidence_combination': 'independent'})
    return {'interpreted_question': question, 'steps': deepcopy(steps), 'clarification': None,
            'planning_route': {'kind': 'verified_schema_pattern', 'version': VERSION, 'digest': DIGEST,
                'claude_calls': 0, 'rule': 'Purpose parser retained all recognized request tokens; schema, query and post-retrieval scope checks remain mandatory.'}}
