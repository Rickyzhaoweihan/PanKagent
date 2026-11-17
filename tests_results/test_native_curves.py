"""Regression tests for the dev gateway's native Cytoscape geometry."""
import json
import unittest

from deploy_native_agent.source.gateway_results.pankgraph_results.native_curves import native_body


class NativeCurvesTest(unittest.TestCase):
    def project(self, route):
        raw = json.dumps({"edge_routes": {"edge": route}, "evidence": {"id": "example"}}).encode()
        rendered = native_body(raw)
        self.assertEqual(native_body(rendered), rendered)
        data = json.loads(rendered)
        self.assertEqual(data["evidence"], {"id": "example"})
        return data["edge_routes"]["edge"]

    def test_cubic_midpoint_and_directions(self):
        route = self.project(dict(route_type="bezier", source_port=[0, 0], target_port=[80, 0],
                                  control_points=[[20, 24], [60, 24]]))
        self.assertEqual(route["control_points"], [[15, 18], [65, 18]])
        self.assertEqual(route["route_type"], "unbundled-bezier")

    def test_collinear_label_waypoints_do_not_create_turns(self):
        route = self.project(dict(route_type="polyline", source_port=[0, 0], target_port=[60, 0],
                                  waypoints=[[10, 0], [10, 0], [40, 0]], label_anchor=[40, 0]))
        self.assertEqual(route["control_points"], [[30, 0]])
        self.assertEqual(route["label_anchor"], [40, 0])

    def test_a_right_angle_has_distinct_incoming_and_outgoing_controls(self):
        route = self.project(dict(route_type="polyline", source_port=[0, 0], target_port=[50, 50],
                                  waypoints=[[50, 0]]))
        before, corner, after = route["control_points"]
        self.assertEqual(corner, [50, 0])
        self.assertEqual(before[1], 0)
        self.assertLess(before[0], 50)
        self.assertEqual(after[0], 50)
        self.assertGreater(after[1], 0)

    def test_parallel_spokes_have_separate_bounded_arcs(self):
        pairs = [('h', 'a'), ('a', 'h'), ('h', 'b')]
        edges = [{'~id': str(i), '~start': a, '~end': b} for i, (a,b) in enumerate(pairs)]
        data = {'layout': {'engine': 'center_spread'}, 'xy_json': {
            'h': {'x':0,'y':0}, 'a': {'x':100,'y':-30}, 'b': {'x':100,'y':30}},
            'combined_query_result': {'edges': edges}, 'edge_routes': {
            str(i): dict(route_type='polyline', source_port=[1,0], target_port=[99,30],
                         waypoints=[[300,-80]]) for i in range(3)}}
        result = json.loads(native_body(json.dumps(data).encode()))
        a,b = (result['edge_routes'][str(i)] for i in range(2))
        self.assertEqual(len(a['control_points']), 1)
        self.assertEqual(a['source_port'], b['target_port'])
        self.assertLess(a['control_points'][0][0], a['target_port'][0])
        self.assertNotEqual(a['control_points'], b['control_points'])
