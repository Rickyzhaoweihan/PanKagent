"""Narrow streaming guard for contradictions of recorded coverage.

This is not a general scientific fact checker. Exhaustive database scope needs
current execution metadata; recorded one-versus-rest semantics are independent.
"""
import re
import json
from .evidence_coverage import CELL_MEASUREMENTS, coverage_for_answer

NOTE = ('The complete PanKgraph search covered all matching cell types under the recorded filters. '
        'The returned entries are the matching records for the checked evidence categories in this release; '
        'absence of further records does not establish biological absence.')
COMPARISON_NOTE = ('The recorded one-versus-rest analysis compared each target cell type with the remaining '
                   'cell types profiled in its source analysis; the returned entries do not narrow that comparison.')


def _steps(evidence):
    values = evidence.values() if isinstance(evidence, dict) else evidence
    return [s for s in values if isinstance(s, dict)]


def _measurement_scope_keys(coverage):
    """Exact category/gene/filter grouping; no inferred name-ID equivalence.

    A broad check may cover another check's cell restriction, but never a
    different gene, condition, evidence category, or unknown property owner.
    """
    scope = coverage['query_scope']
    relations = set(scope.get('relations') or []) & CELL_MEASUREMENTS
    if scope.get('verification') != 'validated_execution' or not relations:
        return None
    retained = []
    genes = []
    for constraint in scope.get('constraints') or []:
        if not isinstance(constraint, dict):
            return None
        owner = constraint.get('entity_type')
        if owner in {'anatomical_structure', 'CellType', 'CELL_TYPE'}:
            # Only ordinary identity restrictions may be subsumed by a full
            # cell-type search. An unknown anatomical predicate stays separate.
            if constraint.get('property') not in {'id', 'name'}:
                return None
            continue
        if owner == 'Gene' and constraint.get('property') in {'id', 'name'}:
            genes.append(constraint)
        elif constraint.get('relationship_type') not in relations:
            return None
        retained.append({k: v for k, v in constraint.items() if k != 'operator'} |
                        {'operator': constraint.get('operator', '=')})
    if not genes:
        return None
    identity = tuple(sorted(json.dumps(c, sort_keys=True, default=str) for c in retained))
    return {(relation, identity) for relation in relations}


def broad_cell_search(evidence):
    steps = _steps(evidence)
    primary = [s for s in steps if s.get('purpose') != 'context'] or steps
    requested, covered = set(), set()
    for step in primary:
        coverage = coverage_for_answer(step)
        relations = set((step.get('requested_scope') or {}).get('relation_types') or [])
        relations |= set(coverage['query_scope'].get('relations') or [])
        if not relations & CELL_MEASUREMENTS:
            continue
        keys = _measurement_scope_keys(coverage)
        if not keys:
            return False
        requested.update(keys)
        scope = coverage['query_scope']
        if (scope['complete_for_requested_scope'] and scope['cell_type_scope'] == 'all_matching'
                and coverage.get('record_membership_enumerated') is True):
            covered.update(keys)
    return bool(requested) and requested <= covered


class ScopeTextFilter:
    def __init__(self, evidence):
        self.steps = _steps(evidence)
        self.cited_steps = {'G'+str(index+1): step for index, step in enumerate(self.steps)}
        self.broad = broad_cell_search(evidence)
        self.source_comparison = any(coverage_for_answer(s)['source_comparisons'] for s in _steps(evidence))
        self.enabled = self.broad or self.source_comparison
        self.buffer = ''
        self.corrections = 0

    def _local_scope(self, sentence, paragraph):
        # A mixed profile may contain unrelated QTL/tissue comparisons. Never
        # attach an enrichment correction to another category's citation.
        foreign = r'\b(?:QTL|GWAS|colocali[sz]ation|tissues?|cohorts?|donors?|variants?|pathways?)\b'
        if re.search(foreign, sentence, re.I):
            return False, False
        context = r'\b(?:enrichment|cell[\s-]*types?|ductal|one[\s-]*(?:versus|vs)[\s-]*rest)\b'
        if not re.search(context, sentence, re.I):
            if re.search(foreign, paragraph, re.I) or not re.search(context, paragraph, re.I):
                return False, False
        citations = re.findall(r'\[(G\d+)\]', sentence) or re.findall(r'\[(G\d+)\]', paragraph)
        if citations:
            if any(citation not in self.cited_steps for citation in citations):
                return False, False
            selected = [self.cited_steps[citation] for citation in dict.fromkeys(citations)]
        else:
            selected = self.steps
        if not selected:
            return False, False
        for step in selected:
            scope = coverage_for_answer(step)['query_scope']
            kinds = set(scope.get('relations') or []) | set((step.get('requested_scope') or {}).get('relation_types') or [])
            if not kinds or not kinds <= CELL_MEASUREMENTS:
                return False, False
        source = all(coverage_for_answer(step)['source_comparisons'] for step in selected)
        return broad_cell_search(selected), source

    def clean(self, paragraph):
        sentences = re.split(r'(?<=[.!?])(?=\s+[A-Z])', paragraph)
        mentions_source = bool(re.search(r'one[\s-]*(?:versus|vs)[\s-]*rest', paragraph, re.I))
        for i, sentence in enumerate(sentences):
            broad, source_comparison = self._local_scope(sentence, paragraph)
            if not (broad or source_comparison):
                continue
            false_unsearched = re.search(
                r'\b(?:no|not\s+any)\s+other\s+cell\s+types?\b[^.!?]{0,100}\b(?:queried|searched|checked|examined)\b', sentence, re.I)
            if re.search(r'\b(?:source|original)\s+(?:study|analysis|experiment)\b', sentence, re.I):
                false_unsearched = False
            false_returned_comparison = re.search(
                r'\bcomparison\b[^.!?]{0,50}\b(?:limited|restricted|confined)\b[^.!?]{0,100}\b(?:returned|retrieved|these\s+two|two\s+ductal)', sentence, re.I)
            false_source_subset = (mentions_source and re.search(
                r'\b(?:specificity|comparison)\b[^.!?]{0,100}\b(?:among|between|within)\b[^.!?]{0,100}\b(?:two|returned|retrieved)\b', sentence, re.I))
            # A correct denial of the limited-comparison claim is not a
            # contradiction. Avoid replacing its independently useful wording.
            denies_limit = bool(re.search(r'\b(?:not|never)\b[^.!?]{0,30}\b(?:limited|restricted|confined|comparison)\b', sentence, re.I))
            if denies_limit:
                false_returned_comparison = false_source_subset = False
            replacement = None
            if broad and (false_unsearched or source_comparison and false_returned_comparison):
                replacement = NOTE + (' ' + COMPARISON_NOTE if source_comparison else '')
            elif source_comparison and (false_returned_comparison or false_source_subset):
                replacement = COMPARISON_NOTE
            if replacement:
                # Keep graph citation linkage even when replacing a known false
                # scope assertion. Quantitative sentences and tables are untouched.
                citations = ' '.join(dict.fromkeys(re.findall(r'\[G\d+\]', sentence)))
                sentences[i] = '\n\n' + replacement + (' ' + citations if citations else '')
                self.corrections += 1
        return ''.join(sentences)

    def feed(self, text, final=False):
        if not self.enabled:
            return text
        self.buffer += text
        chunks = self.buffer.split('\n\n')
        self.buffer = '' if final else chunks.pop()
        return '\n\n'.join(self.clean(chunk) for chunk in chunks) + ('\n\n' if chunks and not final else '')
