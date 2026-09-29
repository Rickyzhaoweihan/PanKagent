"""Closed cohort retrieval drafts; runtime inventory supplies every value.

This consumes no benchmark IDs and never repairs an unknown word by guessing.
The normal preparation and query validators still authorize every predicate.
"""
import re
from .agent_schemas import module
from .preplanning_grounding import phrase_tokens


def draft(question, grounding):
    release = module('database_schema')['release']
    if (not grounding or grounding.get('status') != 'ready'
            or grounding.get('identity', {}).get('graph_release') != release):
        return None
    vocabulary = {**(grounding.get('sample_terminology') or {}), **(grounding.get('term_vocabulary') or {})}
    if (not vocabulary.get('inventory_complete')
            or any(not isinstance(vocabulary.get(key), list) for key in ('sources', 'donor_sources', 'modalities'))):
        return None
    syntax = module('semantic_interpretation')['retrieval_interpretation']['clinical_syntax']
    recipe = module('query_patterns')['clinical_retrieval']
    if re.search(syntax['unsupported_existence_pattern'], question, re.I):
        return None
    comparison = re.fullmatch(syntax['comparison_pattern'], question, re.I)
    branches = [question]
    if comparison:
        source = [s for s in vocabulary.get('donor_sources', []) if s.casefold() == comparison['source'].casefold()]
        assay = [s for s in vocabulary.get('modalities', []) if s.casefold() == comparison['assay'].casefold()]
        if len(source) != 1 or len(assay) != 1:
            return None
        branches = [syntax['branch_question'].format(assay=assay[0],source=source[0],cohort=comparison[role])
                    for role in ('left','right')]
    else:
        if not re.search(r'\bdonors?\b|\bcontrols?\b', question, re.I):
            return None
        # Remove verified inventory literals and stage numbers; every remaining
        # token must belong to the small closed grammar. Comparisons/extensions
        # outside that grammar stay on the general planner.
        text = question
        literals = list(vocabulary.get('sources', [])) + list(vocabulary.get('modalities', []))
        literals += [v for t in vocabulary.get('tissues', []) for v in [t.get('name'), t.get('id')] if v]
        from .semantic_registry import ALIASES
        literals += [a for a,v in ALIASES.items() if v in vocabulary.get('modalities', [])]
        for value in sorted(literals,key=len,reverse=True):
            text = re.sub(r'(?<!\w)'+re.escape(value)+r'(?!\w)',' ',text,flags=re.I)
        text = re.sub(syntax['stage_pattern'], 'stage', text, flags=re.I)
        if set(phrase_tokens(text)) - set(syntax['closed_words']):
            return None
        if re.search(r'\b(?:between|compare|versus|both)\b',text,re.I):
            return None
    steps = []
    for index, text in enumerate(branches,1):
        sample = bool(re.search(r'\bsamples?\b',text,re.I))
        # Donor counts must not gain sample membership from a display annotation.
        if re.search(r'\b(?:whether\s+or\s+not|regardless)\b',text,re.I):
            sample = False
        steps.append({'id':'cohort'+str(index), 'question':text,
            'relation_types':[recipe['sample_relation']] if sample else [],
            'constraints':[], 'depends_on':[], 'complete':True,
            'evidence_combination':'independent',
            'semantic_request':{'source':'user_request','question':question,'revision_instruction':''},
            'retrieval_population_type':recipe['sample_type'] if sample else recipe['population_type']})
    from .semantic_registry import resolve
    steps = [resolve(step, vocabulary, release) for step in steps]
    if any(step.get('semantic_issues') or step.get('recovery') or not step.get('constraints') for step in steps):
        return None
    for step in steps:
        if any(c.get('entity_type') == recipe['diagnosis_type'] for c in step['constraints']):
            step['relation_types'].insert(0, recipe['diagnosis_relation'])
    return {'interpreted_question':question,'steps':steps,'clarification':None}
