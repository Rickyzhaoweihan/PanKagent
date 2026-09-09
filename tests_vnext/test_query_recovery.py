"""Offline recovery diagnosis: failed retrieval is never a zero-match result."""
from copy import deepcopy
import unittest

from pankagent_vnext.query_recovery import retrieval_recovery, stage_recovery


def preview(*steps, status='failed', finished=True, error=None):
    return {'status': status, 'preparation_complete': finished, 'error': error,
            'evidence': {'graph_version': 'fixture-release', 'steps': list(steps)}}


def failed(reason='missing_required_filter:name', **fields):
    return {'step_id': 's1', 'status': 'failed', 'nodes': [], 'edges': [], 'rows': [],
            'validation': [{'valid': False, 'reasons': [reason]}], **fields}


class QueryRecoveryTests(unittest.TestCase):
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
