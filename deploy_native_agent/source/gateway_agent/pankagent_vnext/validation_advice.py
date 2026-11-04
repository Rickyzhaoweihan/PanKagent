"""Semantic diagnostics are evidence for models, not execution vetoes."""
from copy import deepcopy


def add(target, phase, reasons):
    """Retain original diagnostic codes and provenance without claiming validity."""
    for reason in reasons or []:
        item = {'code': str(reason.get('category', 'semantic_issue') if isinstance(reason, dict) else reason),
                'phase': phase, 'blocking': False, 'disposition': 'model_review_required'}
        if isinstance(reason, dict):
            item['detail'] = deepcopy(reason)
        if item not in target.setdefault('python_diagnostics', []):
            target['python_diagnostics'].append(item)


def prepare(step):
    result = deepcopy(step)
    for field in ('semantic_issues', 'runtime_binding_issues'):
        add(result, 'preparation', result.pop(field, []))
    for field in ('recovery', 'filter_warning'):
        issue = result.pop(field, None)
        if issue:
            add(result, 'preparation', [issue])
    resolution = result.get('entity_resolution') or {}
    if resolution.get('state') == 'needs_clarification':
        add(result, 'entity_resolution', ['unresolved_plan_entities'])
        # Do not falsely mark the identity as verified.
        resolution['state'] = 'diagnosed'
    return result


def execution_errors(query, parameters):
    """Independent execution envelope, evaluated even if semantic checks exit early."""
    from .graph import tokenize, GraphValidationError
    if not isinstance(query, str) or not query.strip() or len(query) > 24000:
        return ['missing_or_oversized_cypher']
    try:
        tokens = tokenize(query)
    except GraphValidationError as exc:
        return [str(exc)]
    if tokens and tokens[-1].value == ';':
        tokens = tokens[:-1]
    forbidden = {'CREATE', 'MERGE', 'DELETE', 'DETACH', 'SET', 'REMOVE', 'DROP',
                 'LOAD', 'CALL', 'FOREACH', 'ALTER', 'RENAME', 'GRANT', 'DENY',
                 'REVOKE', 'SHOW', 'USE', 'INSERT', 'FINISH'}
    errors = []
    if any(t.kind == 'SYMBOL' and t.value == ';' for t in tokens):
        errors.append('multiple_statements')
    if any(t.kind == 'WORD' and t.value.upper() in forbidden for t in tokens):
        errors.append('non_readonly_clause')
    if any(t.value == '(' and i >= 3 and tokens[i-1].kind in {'WORD', 'IDENT'}
           and tokens[i-2].value == '.' and tokens[i-3].kind in {'WORD', 'IDENT'}
           for i, t in enumerate(tokens)):
        errors.append('external_function_not_allowed')
    if {t.value for t in tokens if t.kind == 'PARAM'} - set(parameters or {}):
        errors.append('unknown_parameters')
    return errors
