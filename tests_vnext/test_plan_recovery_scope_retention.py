"""An internal proposal failure must not rewrite the user's requested scope."""
from copy import deepcopy

import pytest

from pankagent_vnext.plan_recovery import mark_failure


@pytest.mark.parametrize('question,issue', [
    ('How many PLN scRNAseq samples from T1D stage 3 HPAP donors available?',
     'unsupported_bounded_path_spec:missing_path_anchor_identity'),
    ('Find complete connected paths from ADCY3 through QTL and GWAS evidence.',
     'unsupported_bounded_path_spec:path_constraint_owner_mismatch'),
    ('Find the samples from those donors, excluding scRNA-seq.',
     'missing_dependency_binding'),
    ('Show the overlap of these two sets.', 'invalid_final_join'),
])
def test_internal_path_errors_offer_original_retry_without_adding_conditions(question, issue):
    plan = {'interpreted_question': question, 'proposal_issue': issue, 'steps': []}
    before = deepcopy(plan)
    result = mark_failure(plan)
    assert plan == before
    assert result['interpreted_question'] == question
    assert result['recovery']['retryable'] is True
    assert result['recovery']['suggestions'] == []
    assert result['recovery']['evidence']['reason'] == issue
    assert 'requested connected-path condition' not in result['recovery']['message']
    assert 'no broader search was executed' in result['recovery']['message']
