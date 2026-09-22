import unittest

from pankgraph_results.functional import contributor_counts


class ContributorTests(unittest.TestCase):
    def test_five_selected_three_contributors_all_fifty_points(self):
        data = {'times': list(range(50)), 'mean': [2.] * 50,
                'series': [{'donor_id': str(i), 'values': [i if i < 3 else None] * 50}
                           for i in range(5)]}
        result = contributor_counts(data)
        self.assertEqual(result['selected_donors'], 5)
        self.assertEqual(result['contributing_donors'], 3)
        self.assertEqual(result['contributing_donors_by_timepoint'], [3] * 50)
        self.assertEqual([p['mean_response'] for p in result['trace_points']], data['mean'])
        self.assertNotIn('donor_id', str(result))

    def test_missingness_duplicate_donors_and_zero_are_not_missing(self):
        data = {'times': [0, 1, 2], 'mean': [0., 4., None], 'series': [
            {'donor_id': 'a', 'values': [0., None, None]},
            {'donor_id': 'a', 'values': [0., 4., None]},
            {'donor_id': 'b', 'values': [None, 4., None]}]}
        result = contributor_counts(data)
        self.assertEqual(result['contributing_donors_by_timepoint'], [1, 2, 0])
        self.assertEqual(result['contributing_donors'], 2)
        self.assertIsNone(result['trace_points'][2]['mean_response'])

    def test_absent_or_invalid_series_values_remain_unknown(self):
        for values in (None, [1], [float('nan'), 2], [True, 2]):
            result = contributor_counts({'times': [0, 1], 'mean': [1, 2],
                                         'series': [{'donor_id': 'a', 'values': values}]})
            self.assertFalse(result['contributor_counts_verified'])
            self.assertIsNone(result['contributing_donors'])
            self.assertEqual(result['contributing_donors_by_timepoint'], [None, None])

    def test_no_series_cannot_explain_nonempty_mean(self):
        result = contributor_counts({'times':[0,1], 'mean':[1,2], 'series':[]})
        self.assertIsNone(result['contributing_donors'])
        self.assertEqual(result['contributing_donors_by_timepoint'], [None,None])
