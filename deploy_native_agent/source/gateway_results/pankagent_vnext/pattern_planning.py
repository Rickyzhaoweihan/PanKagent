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

VERSION = 'grounded-signal-plan-patterns-v1'
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


def _entities(question, grounding):
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
    if any(word and word not in _WORDS for word in remaining):
        return None
    return {kind: list(values.values()) for kind, values in entities.items()}


def _identity(candidate):
    return {'property': 'id', 'operator': '=', 'value': candidate['id'],
            'entity_type': candidate['entity_type']}


def _step(kind, identities, question, constraints=()):
    return {'id': kind, 'question': question, 'relation_types': [_CATEGORIES[kind]],
            'depends_on': [], 'constraints': [_identity(item) for item in identities] + list(constraints),
            'complete': True, 'evidence_combination': 'independent'}


def compile_signal_plan(question, grounding, history=None):
    """Return a full scoped proposal or None for the normal planner.

    Signal linkage is three independent queries. It is never a mandatory
    triangle join, and never assumes that a requested variant is a lead.
    """
    if (history or not grounding or grounding.get('status') != 'ready'
            or grounding.get('identity', {}).get('graph_release') != REGISTRY['release']):
        return None
    # Punctuation can encode a modifier even if tokenization drops it.
    if re.search(r'[<>!=]|\b(?:not|except|excluding|without|only|top|first|lead|causal|causality|expression|splicing|exon)\b', question, re.I):
        return None
    entities = _entities(question, grounding)
    if not entities:
        return None
    words = set(phrase_tokens(question))
    requested = {kind for kind, expression in [('coloc', r'\bcoloc(?:aliz\w*|alis\w*)?\b'),
                 ('gwas', r'\bgwas\b'), ('qtl', r'\bqtls?\b')] if re.search(expression, question, re.I)}
    genes, diseases, variants, tissues = (entities.get(kind, []) for kind in
                                        ('Gene', 'disease', 'variants', 'anatomical_structure'))
    steps = []
    if 'coloc' in requested:
        if len(genes) != 1 or len(diseases) != 1 or len(variants) > 1 or tissues:
            return None
        gene, disease = genes[0], diseases[0]
        steps.append(_step('coloc', [gene, disease],
            f"Show recorded colocalization evidence between {gene['name']} and {disease['name']}."))
        if variants:
            variant = variants[0]
            steps.extend([
                _step('gwas', [variant, disease], f"Show recorded GWAS signal membership of {variant['id']} for {disease['name']}."),
                _step('qtl', [variant, gene], f"Show recorded molecular QTL signal membership of {variant['id']} for {gene['name']}."),
            ])
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
