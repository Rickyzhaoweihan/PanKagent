"""Compile completely recognized signal lookups without an inference call.

This is a conservative language recognizer, not a semantic similarity cache.
Every entity must be uniquely grounded and every remaining word must belong to
the supported lookup grammar. Extra modifiers, negation, thresholds, revisions
and unknown wording go to the general grounded planner unchanged.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import re

from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST
from .preplanning_grounding import phrase_tokens

VERSION = 'grounded-signal-plan-patterns-v2-coloc-role-frame'
DIGEST = hashlib.sha256(Path(__file__).read_bytes() + SCHEMA_DIGEST.encode()).hexdigest()

_WORDS = set('''a an the for of in from to with and or does do is are has have
 show find list get what which whether recorded evidence records data available
 all each both separate separately keep genes gene variant variants snp snps
 signal signals association associations associated molecular qtl qtls gwas
 coloc colocalization colocalisation colocalize colocalise colocalizes colocalises
 report alleles allele fine mapping support source sources please it its compare effect direction
'''.split())
_CATEGORIES = {'coloc': 'SIGNAL_COLOC_WITH', 'gwas': 'PART_OF_GWAS_SIGNAL',
               'qtl': 'PART_OF_QTL_SIGNAL'}


def is_no_variant_coloc_role_frame(question):
    """Return whether text has the closed no-variant coloc grammar.

    This is only a latency/admission hint used while the entity catalogue is
    warming.  It never authorizes identities or a query: the ordinary grounded
    recognizer still requires exactly one resolved gene and one disease before
    compiling a plan.  Keeping the regex closed prevents unsupported qualifiers
    from receiving special routing merely because they contain ``coloc``.
    """
    if not isinstance(question, str):
        return False
    gene = r'(?P<gene>[A-Za-z][A-Za-z0-9_.-]{0,63})'
    pattern = (
        r'\s*does\s+(?:the\s+|a\s+)?(?:T1D|type\s+1\s+diabetes)\s+'
        r'(?:associated\s+)?GWAS\s+signal\s+near\s+(?:the\s+)?' + gene
        + r'(?:\s+gene)?\s+colocali[sz]e(?:s)?\s+with\s+'
        r'(?:a|the)\s+(?:molecular\s+)?QTL\s+signal\s+for\s+'
        r'(?:the\s+)?(?P=gene)(?:\s+gene)?\s*[?!.]?\s*'
    )
    return bool(re.fullmatch(pattern, question, re.I))


def _entities(question, grounding, extra_words=()):
    words = list(phrase_tokens(question))
    remaining = list(words)
    entities = {}
    for mention in grounding.get('mentions', []):
        if mention.get('state') != 'resolved' or mention.get('identity_complete') is False:
            # Incidental catalog homonyms do not block, but unknown identity
            # lookups and qualified biological names must not be lost.
            if mention.get('candidates') or mention.get('lookup_complete'):
                return None
            continue
        candidates = mention.get('candidates', [])
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        kind = candidate.get('entity_type')
        if kind not in {'Gene', 'disease', 'variants', 'anatomical_structure'} or not candidate.get('id'):
            return None
        entities.setdefault(kind, {})[candidate['id']] = deepcopy(candidate)
        span = mention.get('normalized_token_span')
        if span and len(span) == 2 and 0 <= span[0] < span[1] <= len(words):
            remaining[span[0]:span[1]] = [''] * (span[1] - span[0])
        else:
            # Explicit rsIDs are verified by a separate parameterized lookup.
            tokens = phrase_tokens(mention.get('requested', ''))
            if not tokens:
                return None
            matched = False
            for start in range(len(words) - len(tokens) + 1):
                if tuple(words[start:start + len(tokens)]) == tuple(tokens):
                    remaining[start:start + len(tokens)] = [''] * len(tokens)
                    matched = True
            if not matched:
                return None
    allowed = _WORDS | set(extra_words)
    if any(word and word not in allowed for word in remaining):
        return None
    return {kind: list(values.values()) for kind, values in entities.items()}


def _coloc_signal_role_frame(question, grounding):
    """Recognize one closed no-variant coloc question without broadening ``near``.

    In this grammar GWAS and QTL name the two recorded signal roles stored on a
    ``SIGNAL_COLOC_WITH`` relationship.  They do not request independent
    disease-wide GWAS or gene-wide QTL inventories.  Generic proximity wording
    remains outside the bounded recognizer because it would need a coordinate
    window and a different graph contract.
    """
    words = list(phrase_tokens(question))
    spans = []
    for mention in grounding.get('mentions', []):
        candidates = mention.get('candidates', [])
        span = mention.get('normalized_token_span')
        if (mention.get('state') != 'resolved' or len(candidates) != 1
                or not isinstance(span, (list, tuple)) or len(span) != 2):
            continue
        candidate = candidates[0]
        kind = candidate.get('entity_type')
        if kind not in {'Gene', 'disease'}:
            continue
        start, end = span
        if not (isinstance(start, int) and isinstance(end, int)
                and 0 <= start < end <= len(words)):
            return False
        spans.append((start, end, '<gene>' if kind == 'Gene' else '<disease>'))
    # Overlap would make the grammatical ownership ambiguous.  Replace spans
    # from right to left so multi-token disease names retain one role marker.
    spans.sort()
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            return False
    for start, end, marker in sorted(spans, reverse=True):
        words[start:end] = [marker]
    normalized = ' '.join(words)
    pattern = (r'does (?:the |a )?<disease> (?:associated )?gwas signal near '
               r'(?:the )?<gene>(?: gene)? colocali[sz]e(?:s)? with '
               r'(?:a|the) (?:molecular )?qtl signal for (?:the )?<gene>(?: gene)?')
    return bool(re.fullmatch(pattern, normalized))


def _identity(candidate):
    return {'property': 'id', 'operator': '=', 'value': candidate['id'],
            'entity_type': candidate['entity_type']}


def _step(kind, identities, question, constraints=()):
    return {'id': kind, 'question': question, 'relation_types': [_CATEGORIES[kind]],
            'depends_on': [], 'constraints': [_identity(item) for item in identities] + list(constraints),
            'complete': True, 'evidence_combination': 'independent'}


def is_verified_local_coloc_step(step):
    """Recognize the closed role-frame step after runtime preparation.

    The marker comes only from this deterministic planner, while the structural
    checks keep a stale or externally supplied marker from authorizing a
    broader query.  This helper deliberately ignores preparation-only fields
    such as resolved identities and retrieval budgets.
    """
    marker = step.get('query_compilation')
    if marker != {'route': 'verified_local_template', 'version': VERSION,
                  'digest': DIGEST}:
        return False
    if (step.get('relation_types') != ['SIGNAL_COLOC_WITH']
            or step.get('depends_on') not in (None, [])
            or step.get('complete', True) is not True
            or step.get('evidence_combination') != 'independent'
            or step.get('path_spec') is not None):
        return False
    constraints = step.get('constraints')
    if not isinstance(constraints, list) or len(constraints) != 2:
        return False
    identities = sorted(
        (constraint.get('entity_type'), constraint.get('property'),
         constraint.get('operator', '='), constraint.get('value'))
        for constraint in constraints if isinstance(constraint, dict)
    )
    return (len(identities) == 2
            and [(kind, prop, operator) for kind, prop, operator, _ in identities]
            == [('Gene', 'id', '='), ('disease', 'id', '=')]
            and all(isinstance(value, str) and value for _, _, _, value in identities))


def compile_signal_plan(question, grounding, history=None):
    """Return a full scoped proposal or None for the normal planner.

    Named-variant signal linkage is three independent queries.  The closed
    no-variant GWAS-near-gene/QTL role frame is one primary coloc query because
    both signal identities are recorded relationship properties.  Neither path
    uses a mandatory triangle join or assumes that a requested variant is a lead.
    """
    if (history or not grounding or grounding.get('status') != 'ready'
            or grounding.get('identity', {}).get('graph_release') != REGISTRY['release']):
        return None
    # Punctuation can encode a modifier even if tokenization drops it.
    if re.search(r'[<>!=]|\b(?:not|except|excluding|without|only|top|first|lead|causal|causality|expression|splicing|exon)\b', question, re.I):
        return None
    coloc_role_frame = _coloc_signal_role_frame(question, grounding)
    entities = _entities(question, grounding, {'near'} if coloc_role_frame else ())
    if not entities:
        return None
    requested = {kind for kind, expression in [('coloc', r'\bcoloc(?:aliz\w*|alis\w*)?\b'),
                 ('gwas', r'\bgwas\b'), ('qtl', r'\bqtls?\b')] if re.search(expression, question, re.I)}
    genes, diseases, variants, tissues = (entities.get(kind, []) for kind in
                                        ('Gene', 'disease', 'variants', 'anatomical_structure'))
    steps = []
    if 'coloc' in requested:
        if len(genes) != 1 or len(diseases) != 1 or len(variants) > 1 or tissues:
            return None
        gene, disease = genes[0], diseases[0]
        coloc_step = _step('coloc', [gene, disease],
            f"Show recorded colocalization evidence between {gene['name']} and {disease['name']}.")
        if coloc_role_frame and not variants:
            coloc_step['query_compilation'] = {
                'route': 'verified_local_template', 'version': VERSION,
                'digest': DIGEST,
            }
        steps.append(coloc_step)
        if variants:
            variant = variants[0]
            steps.extend([
                _step('gwas', [variant, disease], f"Show recorded GWAS signal membership of {variant['id']} for {disease['name']}."),
                _step('qtl', [variant, gene], f"Show recorded molecular QTL signal membership of {variant['id']} for {gene['name']}."),
            ])
        elif requested == {'coloc', 'gwas', 'qtl'} and coloc_role_frame:
            # The exact coloc records already carry both signal identities and
            # lead-variant roles.  A broad QTL inventory would add unrelated
            # signals, while a gene-scoped GWAS lookup has no verified variant.
            pass
        elif requested != {'coloc'}:
            # Generic multiple-evidence scope without a specified signal needs
            # the normal planner to decide independent discovery checks.
            return None
    elif requested == {'qtl'}:
        if diseases or not genes or len(genes) > 3 or len(variants) > 1 or len(tissues) > 1:
            return None
        if len(genes) > 1 and variants:
            # A mentioned variant may belong to just one gene clause. Merely
            # sharing the request does not authorize broadcasting its predicate.
            return None
        tissue_constraints = []
        if tissues:
            if len(genes) > 1:
                from .planning_scope import _mentions, _shared_qtl_tissue
                tokens, mentions, _ = _mentions(question, grounding)
                spans = [span for _, candidate, _, found in mentions
                         if candidate['entity_type'] == 'anatomical_structure'
                         and candidate['id'] == tissues[0]['id'] for span in found]
                starts = [start for _, candidate, _, found in mentions
                          if candidate['entity_type'] == 'Gene' for start, _ in found]
                if not _shared_qtl_tissue(tokens, spans, starts):
                    return None
            tissue = tissues[0]
            choices = REGISTRY['categories'].get('PART_OF_QTL_SIGNAL.tissue_id', [])
            if tissue['id'] not in choices:
                return None
            tissue_constraints.append({'entity_type': None, 'property': 'tissue_id',
                                       'operator': '=', 'value': tissue['id']})
        for index, gene in enumerate(genes):
            suffix = f" in {tissues[0]['name']}" if tissues else ''
            step = _step('qtl', [gene] + variants,
                         f"Show recorded molecular QTL evidence for {gene['name']}" +
                         (f" and {variants[0]['id']}" if variants else '') + suffix + '.', tissue_constraints)
            step['id'] = f'qtl{index + 1}'
            steps.append(step)
    elif requested == {'gwas'}:
        if genes or tissues or len(diseases) != 1 or len(variants) != 1:
            return None
        steps.append(_step('gwas', variants + diseases, question))
    else:
        return None
    return {'interpreted_question': question, 'steps': steps, 'clarification': None,
            'planning_route': {'kind': 'verified_signal_pattern', 'version': VERSION,
                               'digest': DIGEST, 'claude_calls': 0,
                               'rule': 'All request identities and modifiers were recognized; normal scope validation still required.'}}
