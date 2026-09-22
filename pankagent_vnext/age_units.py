"""Unit-aware donor age comparison contract for PanKgraph_08_04.

Complete read-only inventory: 190 ages recorded in years, two in months.
Numbers alone, unknown units and metadata-definition text are not numeric ages.
The source records are preserved; no database mutation or reference query is used.
"""
from functools import lru_cache
import hashlib
import math
from pathlib import Path
import re

VERSION = 'donor-age-units-v1'
RELEASE = 'PanKgraph_08_04'
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
PATTERN = re.compile(r'^([0-9]+(?:[.][0-9]+)?) (years?|months?)$')
YEARS_PATTERN = '^[0-9]+([.][0-9]+)? years?$'
MONTHS_PATTERN = '^[0-9]+([.][0-9]+)? months?$'


def age_years(value):
    """Reference semantics for the versioned expression, not runtime retrieval."""
    match = PATTERN.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    amount = float(match[1])
    if not math.isfinite(amount):
        return None
    return amount / 12.0 if match[2].startswith('month') else amount


def expression(variable):
    if not isinstance(variable, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', variable):
        raise ValueError('unsupported_age_variable')
    access = '`' + variable + '`.age'
    return (f"CASE WHEN {access} =~ '{YEARS_PATTERN}' THEN toFloat(split({access}, ' ')[0]) "
            f"WHEN {access} =~ '{MONTHS_PATTERN}' THEN toFloat(split({access}, ' ')[0]) / 12.0 "
            'ELSE null END')


def _token_key(token, variable):
    # Variable identity and literal values stay exact. Cypher functions/keywords
    # are case-insensitive; quoted and ordinary identifiers share their value.
    if token.kind in ('WORD', 'IDENT') and token.value == variable:
        return 'VARIABLE', variable
    if token.kind == 'WORD':
        return 'WORD', token.value.upper()
    return token.kind, token.value


@lru_cache(maxsize=64)
def _template(variable):
    from .graph import tokenize
    return tuple(_token_key(t, variable) for t in tokenize(expression(variable)))


def expression_at(tokens, start):
    """Recognize the exact reviewed unit expression; never arbitrary CASE math.

    Returns (donor_variable, exclusive_end) or None. The caller still verifies
    donor type, mandatory WHERE scope, comparison operator and finite bound.
    """
    if start + 3 >= len(tokens) or tokens[start].value.upper() != 'CASE':
        return None
    if tokens[start + 1].value.upper() != 'WHEN' or tokens[start + 2].kind not in ('WORD', 'IDENT'):
        return None
    variable = tokens[start + 2].value
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', variable):
        return None
    template = _template(variable)
    end = start + len(template)
    if end > len(tokens):
        return None
    actual = tuple(_token_key(t, variable) for t in tokens[start:end])
    return (variable, end) if actual == template else None


def guidance(variable='donor_variable'):
    return ('Donor age is stored with units, such as 45 years or 14 months. '
            'For a requested numeric age in years, use this complete expression with the actual donor variable, '
            'then the requested comparison and numeric bound: ' + expression(variable) + '. '
            'Do not compare toFloat(age), remove only the unit text, round to whole years, or treat months as years. '
            'Missing or unrecognized ages remain null and cannot satisfy the age comparison.')
