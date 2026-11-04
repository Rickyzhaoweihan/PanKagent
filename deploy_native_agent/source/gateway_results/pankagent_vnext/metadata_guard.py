"""Fail closed for donor metadata scopes lacking a reviewed release contract.

This deployment gate does not change property meanings, casts, joins, or data.
BMI numeric parsing and sex-at-birth missingness need a separate scoped repair.
"""
import hashlib
import re
from pathlib import Path

VERSION = 'donor-metadata-deployment-gate-v1'
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
RELEASE = 'PanKgraph_08_04'


def recovery(step, release):
    if release != RELEASE:
        return None
    constraints = step.get('constraints') or []
    donor_fields = {
        str(c.get('property', '')).split('.')[-1]
        for c in constraints if c.get('entity_type') in (None, 'donor')
    }
    request = step.get('semantic_request') or {}
    # User wording takes precedence over a model-written explicit field name.
    question = (str(request.get('question', '')) + ' ' +
                str(request.get('revision_instruction', ''))).strip()
    question = question or str(step.get('question', ''))
    donor_scope = bool(donor_fields & {'bmi', 'gender', 'sex_at_birth', 't1d_stage'}) or bool(
        re.search(r'\b(?:donors?|HPAP)\b', question, re.I))
    if not donor_scope:
        return None
    bmi_comparison = bool(re.search(
        r'\b(?:BMI|body mass index)\b.{0,40}(?:[<>=]|\b(?:under|over|above|below|less|greater|between|at most|at least|exactly)\b|\d)',
        question, re.I))
    if 'bmi' in donor_fields or bmi_comparison:
        category = 'unsupported_donor_bmi_filter'
        message = ('BMI filtering is temporarily unavailable for this graph release: '
                   'recorded donor BMI values are strings and their numeric parsing and '
                   'missing-value contract have not been validated. Your filters are preserved; '
                   'this is not evidence of zero matching donors.')
    elif 'sex_at_birth' in donor_fields or re.search(r'\bsex[ _-]at[ _-]birth\b', question, re.I):
        category = 'unsupported_donor_sex_at_birth_coverage'
        message = ('A complete donor answer using sex_at_birth is temporarily unavailable '
                   'because that property has incomplete recorded coverage in this graph release. '
                   'Missing values cannot be treated as nonmatching donors, and gender is a '
                   'different property. Your filters are preserved; no zero-match conclusion was made.')
    elif (re.search(r'\b(?:male|female|men|women)\b', question, re.I)
          and not re.search(r'\bgender\b', question, re.I)):
        category = 'donor_sex_gender_needs_clarification'
        message = ('Specify whether male/female refers to recorded gender or sex_at_birth. '
                   'These donor properties have different meanings and coverage; the service '
                   'will not choose one silently. No donor count or zero-match conclusion was made.')
    else:
        return None
    return {'category': category, 'title': 'Donor metadata filter needs review',
            'message': message, 'retryable': False, 'suggestions': [],
            'evidence': {'graph_release': release, 'contract_version': VERSION,
                         'semantic_validation': 'unsupported', 'matching_records_checked': False}}
