"""Bind verified categorical identities without reproducing damaged source text.

Only donor stage equality is ordinal-addressable. No fuzzy disease, tissue,
threshold, or other property substitution is authorized by this module.
"""
import re

VERSION = 'verified-categorical-bindings-1'

def bind_verified_categories(query, step, parameters):
    from .graph import tokenize, _pattern_bindings, _predicate_owner, GraphValidationError
    result_parameters = dict(parameters)
    if not step.get('semantic_registry') or step.get('semantic_issues'):
        return query, result_parameters, []
    choices = [c for c in step.get('constraints', []) if c.get('entity_type') == 'donor'
               and c.get('property') == 't1d_stage' and c.get('operator', '=') == '=']
    if len(choices) != 1:
        return query, result_parameters, []
    canonical = choices[0]['value']
    ordinal = re.match(r'^Stage (\d+):', str(canonical))
    if not ordinal:
        return query, result_parameters, []
    try:
        tokens = tokenize(query)
    except GraphValidationError:
        return query, result_parameters, []
    bindings, _ = _pattern_bindings(tokens)
    edits, notes = [], []
    for index, token in enumerate(tokens):
        if token.value != 't1d_stage' or token.kind not in ('WORD', 'IDENT') or index + 2 >= len(tokens):
            continue
        if tokens[index+1].value not in ('=', ':') or tokens[index+2].kind != 'STRING':
            continue
        owner = _predicate_owner(tokens, index)
        if bindings.get(owner) != {'donor'}:
            continue
        value = tokens[index+2]
        proposed = re.match(r'^Stage\s+(\d+)\s*:', value.value, re.I)
        if not proposed or proposed[1] != ordinal[1]:
            continue
        name = 'canonical_donor_stage'
        if name in result_parameters and result_parameters[name] != canonical:
            continue
        result_parameters[name] = canonical
        edits.append((value.start, value.end, '$'+name))
        notes.append({'kind':'verified_stage_ordinal','property':'donor.t1d_stage',
                      'requested_stage':ordinal[1], 'parameter':name, 'version':VERSION,
                      'registry_version':step['semantic_registry'].get('version')})
    for start, end, replacement in sorted(edits, reverse=True):
        query = query[:start] + replacement + query[end:]
    return query, result_parameters, notes
