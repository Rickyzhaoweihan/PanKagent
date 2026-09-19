"""One lossless list contract for planning, query templates and validation.

Commas belong to literal values unless a complete, verified category inventory
proves a legacy IN encoding unambiguous. This module never supplies that proof.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path


VALUE_SCHEMA = {'type': ['string', 'array'], 'items': {'type': 'string'}}
VERSION = 'typed-constraint-lists-v1'
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def list_value(value, *, categories=None):
    """Return an independent scalar list, accepting legacy JSON arrays.

    ``categories`` must be a verified complete inventory for the exact property
    owner and release. A scalar containing a comma is not generally a list.
    If the complete scalar is recorded, or a member is unknown/ambiguous, fail
    closed instead of choosing the broader interpretation.
    """
    if isinstance(value, str):
        raw = value
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if not isinstance(value, list) and categories is not None:
            members = [member.strip() for member in raw.split(',')]
            if (raw not in categories and len(members) > 1
                    and all(member and categories.count(member) == 1 for member in members)):
                value = members
    if (not isinstance(value, list) or not value
            or not all(isinstance(member, (str, bool, int))
                       or isinstance(member, float) and math.isfinite(member)
                       for member in value)):
        raise ValueError('invalid_constraint_list')
    return deepcopy(value)
