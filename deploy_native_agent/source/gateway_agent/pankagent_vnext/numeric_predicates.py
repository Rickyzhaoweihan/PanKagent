"""Narrow numeric predicate equivalence; no implicit identifier conversions.

Registered donor age contains explicit year/month units. A raw numeric cast is
not an equivalent age comparison in this release. The reviewed unit expression
preserves fractional years; missing/unrecognized values remain unknown.
"""
import math
import hashlib
from pathlib import Path

from .age_units import DIGEST as AGE_UNITS_DIGEST, expression_at
VERSION = 'numeric-predicate-equivalence-v3-age-grouping'
DIGEST = hashlib.sha256(Path(__file__).read_bytes() + AGE_UNITS_DIGEST.encode()).hexdigest()
# Explicit semantic ownership, not a heuristic on names or numeric-looking IDs.
NUMERIC_NODE_FIELDS = {'donor': {'age'}}
COMPARISONS = {'=', '<', '<=', '>', '>='}


def cast_at(tokens, index):
    """Recognize only function(variable.property), never nested arithmetic."""
    if index < 4 or index + 1 >= len(tokens):
        return None
    if (tokens[index-3].value != '(' or tokens[index-2].kind not in ('WORD','IDENT')
            or tokens[index-1].value != '.' or tokens[index+1].value != ')'):
        return None
    token=tokens[index-4]
    return token.value.lower() if token.kind in ('WORD','IDENT') else None


def finite_number(value):
    if isinstance(value,bool) or not isinstance(value,(int,float,str)):
        return False
    try:
        return math.isfinite(float(value))
    except (ValueError,OverflowError):
        return False


def numeric_owner(tokens, index, constraint):
    # Local import avoids a graph module import cycle. Existing owner/type guards
    # still validate aliases and each UNION branch independently.
    from .graph import _pattern_bindings, _predicate_owner
    bindings,_=_pattern_bindings(tokens)
    labels=bindings.get(_predicate_owner(tokens,index),set())
    requested=constraint.get('_entity_type') or constraint.get('entity_type')
    if requested and requested not in labels:
        return None
    matches=[label for label,fields in NUMERIC_NODE_FIELDS.items()
             if label in labels and tokens[index].value in fields]
    return matches[0] if len(matches)==1 else None


def float_cast_allowed(tokens, index, constraint, actual):
    # No registered raw field currently has a unit-free numeric-string contract.
    return False


def unit_predicate_present(tokens, constraint, parameters, allowed_variables=None):
    """Match the exact reviewed expression on a mandatory typed donor binding."""
    if (str(constraint.get('property', '')).split('.')[-1] != 'age'
            or str(constraint.get('operator', '=')).upper() not in COMPARISONS
            or not finite_number(constraint.get('value'))
            or (constraint.get('_entity_type') or constraint.get('entity_type')) not in (None, 'donor')):
        return False
    from .graph import _pattern_bindings, _value
    bindings, _ = _pattern_bindings(tokens)
    opens, closing_to_open = [], {}
    for i, token in enumerate(tokens):
        if token.value == '(':
            opens.append(i)
        elif token.value == ')':
            if not opens:
                return False
            closing_to_open[i] = opens.pop()
    if opens:
        return False
    clause, optional_match = '', False
    for index, token in enumerate(tokens):
        if token.kind == 'WORD' and token.value.upper() in {'MATCH','WHERE','RETURN','WITH','UNWIND','ORDER'}:
            clause = token.value.upper()
            if clause == 'MATCH':
                optional_match = index > 0 and tokens[index-1].value.upper() == 'OPTIONAL'
        if clause != 'WHERE' or optional_match:
            continue
        found = expression_at(tokens, index)
        if not found:
            continue
        variable, end = found
        # Grouping the reviewed CASE expression does not change its meaning.
        # Accept only immediately enclosing parentheses, not a function,
        # arithmetic, another CASE, or a boolean comparison of its predicate.
        left = index
        grouping = set()
        while left > 0 and tokens[left-1].value == '(':
            left -= 1
            grouping.add(left)
        if left == 0 or tokens[left-1].value.upper() not in {'WHERE', 'AND'}:
            continue
        while end < len(tokens) and tokens[end].value == ')' and closing_to_open.get(end) in grouping:
            grouping.remove(closing_to_open[end])
            end += 1
        if ('donor' not in bindings.get(variable, set())
                or allowed_variables is not None and variable not in allowed_variables
                or end >= len(tokens) or tokens[end].value != constraint.get('operator', '=')):
            continue
        actual, after = _value(tokens, end+1, parameters)
        if (after == end+1 or not isinstance(actual, (int, float)) or isinstance(actual, bool)
                or not finite_number(actual) or float(actual) != float(constraint['value'])):
            continue
        while after < len(tokens) and tokens[after].value == ')' and closing_to_open.get(after) in grouping:
            grouping.remove(closing_to_open[after])
            after += 1
        # Do not accept a prefix of a different numeric expression, e.g. END < 1+99.
        if after < len(tokens) and tokens[after].value.upper() not in {'AND','RETURN','WITH','ORDER','LIMIT','UNION',';'}:
            continue
        return True
    return False


def validation_errors(tokens, step, parameters):
    """Give precise feedback rather than silently accepting lossy age casts."""
    from .graph import _value
    errors=[]
    clause,optional_match='',False
    for index,token in enumerate(tokens):
        if token.kind=='WORD' and token.value.upper() in {'MATCH','WHERE','RETURN','WITH','UNWIND','ORDER'}:
            clause=token.value.upper()
            if clause=='MATCH':
                optional_match=index>0 and tokens[index-1].value.upper()=='OPTIONAL'
        if clause!='WHERE' or optional_match:
            continue
        if token.kind not in ('WORD','IDENT'):
            continue
        for constraint in step.get('constraints',[]):
            if (constraint.get('property','').split('.')[-1]!=token.value
                    or str(constraint.get('operator','=')).upper() not in COMPARISONS
                    or not finite_number(constraint.get('value'))):
                continue
            owner=numeric_owner(tokens,index,constraint)
            cast = cast_at(tokens,index)
            offset = 2 if cast in ('tointeger', 'tofloat') else 1
            if not owner or index+offset+1>=len(tokens) or tokens[index+offset].value not in COMPARISONS:
                continue
            actual,end=_value(tokens,index+offset+1,parameters)
            if end!=index+offset+1:
                errors.append('age_comparison_requires_units:'+owner+'.'+token.value+':use_year_month_expression')
    return sorted(set(errors))


def guidance(step):
    if any(c.get('entity_type')=='donor' and c.get('property')=='age'
           and str(c.get('operator','=')).upper() in COMPARISONS
           and finite_number(c.get('value')) for c in step.get('constraints',[])):
        from .age_units import guidance as age_guidance
        return '\n' + age_guidance()
    return ''
