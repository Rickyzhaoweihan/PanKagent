"""Offline recovery diagnosis: failed retrieval is never a zero-match result."""
from copy import deepcopy
import unittest

from pankagent_vnext.query_recovery import retrieval_recovery, stage_recovery, oversized_preview_recovery


def preview(*steps, status='failed', finished=True, error=None):
    return {'status': status, 'preparation_complete': finished, 'error': error,
            'evidence': {'graph_version': 'fixture-release', 'steps': list(steps)}}


def failed(reason='missing_required_filter:name', **fields):
    return {'step_id': 's1', 'status': 'failed', 'nodes': [], 'edges': [], 'rows': [],
            'validation': [{'valid': False, 'reasons': [reason]}], **fields}


def limited_step():
    return {'step_id': 's1', 'status': 'partial', 'truncated': True,
            'validation': [{'valid': True}], 'queries': [{'cypher': 'MATCH (g:Gene) RETURN g'}],
            'retrieval_execution': {'completed': True, 'cursor_exhausted': False}}


def dependency_step(identifier, parents):
    # Match Runtime.failed_step and the preview worker's blocked_by field.
    from pankagent_vnext.app import Runtime
    result = Runtime.failed_step({'id': identifier, 'question': 'Retrieve requested evidence.'},
        {'category': 'dependency_unavailable', 'message': 'A required earlier check could not be completed.'})
    result['blocked_by'] = parents
    return result


class QueryRecoveryTests(unittest.TestCase):
    def test_direct_and_transitive_limit_dependencies_keep_the_size_recovery(self):
        for children in ([dependency_step('s2', ['s1'])],
                         [dependency_step('s3', ['s2']), dependency_step('s2', ['s1'])]):
            with self.subTest(children=[child['step_id'] for child in children]):
                value = preview(limited_step(), *children, status='partial')
                original = deepcopy(value)
                result = oversized_preview_recovery(value)
                assert result['category'] == 'retrieval_limit' and result['retryable'] is False
                assert result['evidence']['blocked_by_retrieval_limit_step_ids'] == [c['step_id'] for c in children]
                assert 'service' not in result['message']
                assert value == original

    def test_unexecuted_materialization_limit_can_explain_its_dependents(self):
        resource = {'step_id': 's2', 'status': 'partial', 'truncated': True,
                    'validation': [{'valid': False, 'reasons': ['run_graph_materialization_limit']}],
                    'queries': [], 'generator_attempts': [], 'nodes': [], 'edges': [], 'rows': []}
        result = oversized_preview_recovery(preview(limited_step(), resource, dependency_step('s3', ['s2']), status='partial'))
        assert result['category'] == 'retrieval_limit'
        assert result['evidence']['blocked_by_retrieval_limit_step_ids'] == ['s3']

    def test_mixed_service_and_limit_parents_remain_actionable(self):
        service = failed('authentication', step_id='s2', error={'category': 'authentication'})
        result = oversized_preview_recovery(preview(limited_step(), service,
            dependency_step('s3', ['s1', 's2']), status='partial'))
        assert result['category'] == 'authentication' and result['retryable'] is False
        assert result['evidence']['failed_step_ids'] == ['s2', 's3']
        assert result['evidence']['blocked_by_retrieval_limit_step_ids'] == []

    def test_independent_failure_is_retained_beside_a_proven_limit_dependent(self):
        service = failed('generation_unavailable:ConnectionError', step_id='s3')
        result = oversized_preview_recovery(preview(limited_step(), dependency_step('s2', ['s1']), service, status='partial'))
        assert result['category'] == 'retrieval_unavailable' and result['retryable'] is True
        assert result['evidence']['failed_step_ids'] == ['s3']
        assert result['evidence']['blocked_by_retrieval_limit_step_ids'] == ['s2']

    def test_unknown_empty_or_malformed_parents_never_prove_limit_derivation(self):
        for parents in ([], ['unknown'], ['s1', 'unknown'], ['s2'], 's1', [None]):
            with self.subTest(parents=parents):
                result = oversized_preview_recovery(preview(limited_step(), dependency_step('s2', parents), status='partial'))
                assert result['category'] == 'retrieval_unavailable'
                assert result['evidence']['blocked_by_retrieval_limit_step_ids'] == []

    def test_cycles_and_duplicate_parent_identities_never_prove_limit_derivation(self):
        for children in ([dependency_step('s2', ['s1', 's3']), dependency_step('s3', ['s2'])],
                         [dependency_step('s2', ['s1']), dependency_step('s2', ['s1'])]):
            with self.subTest(children=children):
                result = oversized_preview_recovery(preview(limited_step(), *children, status='partial'))
                assert result['category'] == 'retrieval_unavailable'
                assert result['evidence']['blocked_by_retrieval_limit_step_ids'] == []

    def test_own_execution_or_independent_error_is_never_hidden_by_blocked_by(self):
        mutations = [
            {'queries': [{'cypher': 'MATCH (g:Gene) RETURN g'}]},
            {'retrieval_execution': {'completed': False}},
            {'generator_attempts': [{'status': 'failed', 'http_status': 403}]},
            {'nodes': [{'id': 'already-retrieved'}]},
            {'error': {'category': 'dependency_unavailable', 'code': 'authentication'}},
            {'validation': [{'valid': False, 'reasons': ['dependency_unavailable', 'graph_execution_failed']}]},
        ]
        for change in mutations:
            with self.subTest(change=change):
                child = {**dependency_step('s2', ['s1']), **change}
                result = oversized_preview_recovery(preview(limited_step(), child, status='partial'))
                assert result['category'] != 'retrieval_limit'
                assert result['evidence']['blocked_by_retrieval_limit_step_ids'] == []

    def test_recovery_does_not_make_the_truncated_parent_or_dependents_ready(self):
        from pankagent_vnext.evidence_status import query_readiness, confirmation_eligible
        plan = {'steps': [{'id': 's1', 'depends_on': [], 'complete': True},
                          {'id': 's2', 'depends_on': ['s1'], 'complete': True}]}
        value = preview(limited_step(), dependency_step('s2', ['s1']), status='partial')
        before = query_readiness(plan, value)
        assert oversized_preview_recovery(value)['category'] == 'retrieval_limit'
        assert query_readiness(plan, value) == before
        assert before['blocked_step_ids'] == ['s1', 's2']
        assert not confirmation_eligible(plan, value)

    def test_top_level_service_error_is_not_replaced_by_truncation_notice(self):
        limited = {'step_id': 's1', 'status': 'partial', 'truncated': True,
                   'validation': [{'valid': True}], 'queries': [{'cypher': 'MATCH (g:Gene) RETURN g'}],
                   'retrieval_execution': {'completed': True, 'cursor_exhausted': False}}
        for category, retryable in [('authentication', False), ('budget_exhausted', False), ('rate_limited', True)]:
            with self.subTest(category=category):
                error = {'category': category}
                # A resource code must not erase an additional service category.
                for extra in ({}, {'code': 'run_graph_materialization_limit'}):
                    recovery = oversized_preview_recovery(preview(limited, status='partial', error={**error, **extra}))
                    assert recovery['category'] == category
                    assert recovery['retryable'] == retryable
                    assert recovery['evidence']['limited_step_ids'] == ['s1']

    def test_mixed_truncation_and_service_failure_preserve_the_actionable_error(self):
        limited = {'step_id': 's1', 'status': 'partial', 'truncated': True,
                   'validation': [{'valid': True}], 'queries': [{'cypher': 'MATCH (g:Gene) RETURN g'}],
                   'retrieval_execution': {'completed': True, 'cursor_exhausted': False}}
        unavailable = failed('generation_unavailable:ConnectionError', step_id='s2')
        value = preview(limited, unavailable, status='partial', error={'category': 'run_graph_materialization_limit'})
        recovery = oversized_preview_recovery(value)
        assert recovery['category'] == 'retrieval_unavailable'
        assert recovery['retryable'] is True
        assert recovery['evidence']['failed_step_ids'] == ['s2']
        assert recovery['evidence']['limited_step_ids'] == ['s1']
        assert 'does not resolve the separate failure' in recovery['message']

    def test_validated_truncation_has_specific_recovery_without_claiming_query_failure(self):
        step = {'step_id': 's1', 'status': 'partial', 'truncated': True,
                'validation': [{'valid': True}], 'queries': [{'cypher': 'MATCH (g:Gene) RETURN g'}],
                'retrieval_execution': {'completed': True, 'cursor_exhausted': False},
                'nodes': [{'id': 'g', 'labels': ['Gene'], 'properties': {'private': 'DO_NOT_EXPOSE'}}]}
        value = preview(step, status='partial')
        before = deepcopy(value)
        recovery = oversized_preview_recovery(value)
        self.assertEqual(recovery['category'], 'retrieval_limit')
        self.assertIn('too broad', recovery['message'])
        self.assertIn('more specific query', recovery['message'])
        self.assertFalse(recovery['retryable'])
        self.assertFalse(recovery['evidence']['complete_for_requested_scope'])
        self.assertNotIn('DO_NOT_EXPOSE', str(recovery))
        self.assertEqual(value, before)
        for change in ({'truncated': False}, {'validation': [{'valid': False}]},
                       {'retrieval_execution': {'completed': False, 'cursor_exhausted': False}},
                       {'queries': []}, {'error': {'category': 'budget_exhausted'}}):
            with self.subTest(change=change):
                self.assertIsNone(oversized_preview_recovery(preview({**step, **change}, status='partial')))

    def test_waits_for_final_preview_and_ignores_unexecuted_checks(self):
        value = preview(failed(), status='partial', finished=False)
        value['pending_step_ids'] = ['s2', 's3']
        self.assertIsNone(retrieval_recovery(value))
        value['preparation_complete'] = True
        issue = retrieval_recovery(value)
        self.assertEqual(issue['category'], 'query_validation')
        self.assertEqual(issue['evidence']['failed_step_ids'], ['s1'])
        self.assertEqual(issue['suggestions'], [])

    def test_successful_empty_scalar_zero_and_partial_evidence_remain_usable(self):
        for success in ({'status': 'empty'}, {'status': 'complete', 'rows': [{'count': 0}]},
                        {'status': 'partial', 'rows': [{'count': 0}]},
                        {'status': 'partial', 'nodes': [{'id': 'retained'}]}):
            with self.subTest(success=success):
                self.assertIsNone(retrieval_recovery(preview(failed(), success, status='partial')))

    def test_context_success_cannot_mask_primary_failure(self):
        context = {'status': 'complete', 'purpose': 'context', 'nodes': [{'id': 'context'}]}
        self.assertEqual(retrieval_recovery(preview(failed(), context, status='partial'))['category'], 'query_validation')

    def test_failed_context_cannot_block_complete_primary(self):
        self.assertIsNone(retrieval_recovery(preview({'status': 'complete', 'rows': [{'count': 0}]},
                                                    failed(purpose='context'), status='partial')))

    def test_service_failure_without_steps_stays_service_failure(self):
        issue = retrieval_recovery(preview(error='timeout'))
        self.assertEqual(issue['category'], 'retrieval_unavailable')
        self.assertEqual(issue['suggestions'], [])
        self.assertTrue(issue['retryable'])

    def test_error_prose_never_leaks_and_filter_values_do_not_classify_errors(self):
        value = preview(failed('missing_required_filter:name:401:connect'), error={'message': 'PRIVATE auth secret 403'})
        issue = retrieval_recovery(value)
        self.assertEqual(issue['category'], 'query_validation')
        self.assertNotIn('PRIVATE', str(issue))
        self.assertNotIn('403', str(issue))

    def test_generation_http_status_and_structured_errors_are_actionable(self):
        for status, category, retryable in [(401, 'authentication', False), (403, 'authentication', False),
                                             (402, 'billing', False), (429, 'rate_limited', True),
                                             (503, 'retrieval_unavailable', True)]:
            with self.subTest(status=status):
                value = preview(failed('generation_unavailable:HTTPStatusError', generator_attempts=[
                    {'status': 'failed', 'error_category': 'HTTPStatusError', 'http_status': status}]))
                issue = retrieval_recovery(value)
                self.assertEqual(issue['category'], category)
                self.assertEqual(issue['retryable'], retryable)
                self.assertEqual(issue['suggestions'], [])

    def test_recovered_old_auth_failure_does_not_override_final_validation_failure(self):
        step = failed('missing_required_filter:tissue', generator_attempts=[
            {'status': 'failed', 'http_status': 401}, {'status': 'completed'}])
        step['validation'].insert(0, {'valid': False, 'reasons': ['authentication']})
        self.assertEqual(retrieval_recovery(preview(step))['category'], 'query_validation')

    def test_historical_mapping_and_string_error_payloads_are_safe(self):
        value = preview(failed(error='authentication'))
        value['evidence']['steps'] = {'s1': value['evidence']['steps'][0]}
        self.assertEqual(retrieval_recovery(value)['category'], 'authentication')
        for malformed in [None, 'failure', [], {'status': 'failed', 'evidence': 'bad'},
                          {'status': 'failed', 'error': {'message': 'PRIVATE'}, 'evidence': {'steps': [None, 'bad']}}]:
            with self.subTest(malformed=malformed):
                self.assertNotIn('PRIVATE', str(retrieval_recovery(malformed)))

    def test_execution_error_is_not_reported_as_query_validation_rejection(self):
        issue = retrieval_recovery(preview(failed('graph_execution_failed:RuntimeError')))
        self.assertEqual(issue['category'], 'graph_execution_failed')
        self.assertIn('passed validation', issue['message'])
        self.assertEqual(issue['suggestions'], [])

    def test_unknown_complete_partial_without_failure_is_not_invented_failure(self):
        for status in ['complete', 'empty', 'not_requested', 'partial']:
            self.assertIsNone(retrieval_recovery(preview(status=status)))

    def test_incomplete_inventory_cannot_establish_missing_stage(self):
        issue = stage_recovery('2', {'stages': ['Stage 3: recorded']}, 'fixture-release')
        self.assertEqual(issue['category'], 'stage_inventory_unavailable')
        self.assertEqual(issue['suggestions'], [])
        self.assertFalse(issue['evidence']['inventory_complete'])

    def test_stage_two_is_not_called_absent_when_it_is_in_inventory(self):
        issue = stage_recovery('2', {'stages': ['Stage 2: recorded'], 'inventory_complete': True}, 'fixture-release')
        self.assertNotEqual(issue['category'], 'recorded_stage_unavailable')
