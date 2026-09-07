"""Narrow, observable guard for contradicting a verified broad cell-type search.

This is not a general scientific fact checker. It never infers coverage for
legacy evidence without an explicit validated scope record.
"""
import re
from .graph_contract import MEASUREMENTS

NOTE = ('The graph search included all matching cell types under the recorded filters. '
        'Only the returned records support this answer; missing records do not establish biological absence.')

def broad_cell_search(evidence):
    steps = evidence.values() if isinstance(evidence, dict) else evidence
    for step in steps:
        scope = step.get('requested_scope') or {}
        constraints = scope.get('constraints', [])
        if (step.get('status') == 'complete' and not step.get('truncated') and step.get('queries')
            and scope.get('complete') is True and set(scope.get('relation_types') or []) & MEASUREMENTS
            and constraints and all(c.get('entity_type') == 'Gene' and c.get('property') in ('id', 'name') for c in constraints)):
            return True
    return False

class ScopeTextFilter:
    def __init__(self, evidence):
        self.enabled = broad_cell_search(evidence)
        self.buffer = ''
        self.corrections = 0

    def clean(self, paragraph):
        # Sentence-level replacement keeps supported tables and statements.
        sentences = re.split(r'(?<=[.!?])(?=\s+[A-Z])', paragraph)
        for i, sentence in enumerate(sentences):
            if re.search(r'\b(?:no|not\s+any)\s+other\s+cell\s+types?\b[^.!?]{0,100}\b(?:queried|searched|checked|examined)\b', sentence, re.I):
                sentences[i] = '\n\n' + NOTE
                self.corrections += 1
        return ''.join(sentences)

    def feed(self, text, final=False):
        if not self.enabled:
            return text
        self.buffer += text
        chunks = self.buffer.split('\n\n')
        self.buffer = '' if final else chunks.pop()
        return '\n\n'.join(self.clean(chunk) for chunk in chunks) + ('\n\n' if chunks and not final else '')
