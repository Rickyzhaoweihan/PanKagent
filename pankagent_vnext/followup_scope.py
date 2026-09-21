"""Carry a uniquely requested gene into referential follow-ups for re-resolution."""
import re

VERSION = 'followup-gene-scope-v1'


def grounding_question(question, prior):
    if not prior or not re.search(r'\b(?:those|that|these|it|now|earlier|previous|focus|restrict)\b', question, re.I):
        return question
    # An explicit new gene/variant owns its own scope. Do not pull a former
    # subject into 'What about gene CFTR?' or a request for a new named locus.
    if re.search(r'\bgene\s+[A-Z][A-Z0-9-]+\b|\brs\d+\b|\b[A-Z][A-Z0-9]{2,}\b', re.sub(r'\b(?:GO|T1D|RNA|ATAC|GWAS|QTL|HIRN)\b','',question)):
        return question
    anchors=set()
    for step in (prior.get('plan') or {}).get('steps',[]):
        for binding in step.get('constraints',[]):
            if binding.get('entity_type')=='Gene' and binding.get('property') in {'id','name'} and binding.get('operator','=')=='=' and isinstance(binding.get('value'),str):
                anchors.add(binding['value'])
    if len(anchors)!=1: return question
    return question + '\nRetain the previously requested gene: ' + next(iter(anchors)) + '.'
