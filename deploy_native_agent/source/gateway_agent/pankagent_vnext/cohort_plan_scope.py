"""Keep a requested donor/sample intersection in the executable plan."""
from copy import deepcopy
import hashlib
from pathlib import Path
import re

VERSION='cohort-sample-scope-v1'
DIGEST=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def requested(question):
    return bool(re.search(r'\bdonors?\b',question,re.I) and re.search(r'\b(?:samples?|assay(?:[- ]records?)?s?)\b',question,re.I))


def compile_scope(question, grounding, plan):
    result=deepcopy(plan)
    if not requested(question) or result.get('clarification'):return result,None
    steps=result.get('steps',[])
    if not any(set(s.get('relation_types',[])) & {'HAS_DONOR','HAS_SAMPLE'} or any(c.get('entity_type')=='donor' for c in s.get('constraints',[])) for s in steps):
        return result,None
    if any('HAS_SAMPLE' in s.get('relation_types',[]) for s in steps):return result,None
    if (len(steps)!=1 or steps[0].get('relation_types')!=['HAS_DONOR'] or steps[0].get('depends_on')
            or re.search(r'\b(?:without|excluding|exclude|not|no)\s+(?:\w+\s+){0,2}(?:samples?|assays?)\b',question,re.I)):
        return result,'missing_requested_category:HAS_SAMPLE'
    # Preserve every supplied predicate; verified tissue/source/stage resolution
    # and the same-sample path validator run normally on this full question.
    step=steps[0]
    step['cohort_scope_compilation']={'version':VERSION,'original_question':step.get('question'),
        'original_relations':step['relation_types'],'source':'explicit original donor and assay/sample request'}
    step['relation_types']=['HAS_SAMPLE'];step['question']=question
    return result,None
