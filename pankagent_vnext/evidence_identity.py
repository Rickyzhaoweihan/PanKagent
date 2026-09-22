"""Stable citation identities shared by execution, excerpts and result views."""
import re

VERSION = 'evidence-identity-v1'


def evidence_id(step, index):
    value = step.get('evidence_id')
    if value is not None:
        if not isinstance(value, str) or not re.fullmatch(r'G[1-9][0-9]*', value):
            raise ValueError('invalid_evidence_id')
        return value
    return f'G{index + 1}'


def validate_ids(steps):
    ids = [evidence_id(step, index) for index, step in enumerate(steps)]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate_evidence_id')
    return ids
