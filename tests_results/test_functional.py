import json
import unittest
from pankgraph_results import functional

class Response:
    headers={'content-type':'application/json'}
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    def raise_for_status(self):pass
    async def aiter_bytes(self):
        yield json.dumps({'times':[0,1,2],'mean':[1,None,3],'trace_type':'ins_ieq','y_label':'recorded units',
            'series':[{'donor_id':'a'},{'donor_id':'a'},{'donor_id':'b'}],'stimuli':[[0,2,'glucose']]}).encode()
class HTTP:
    def stream(self,*args,**kwargs):self.call=(args,kwargs);return Response()
class FunctionalTests(unittest.IsolatedAsyncioTestCase):
    async def test_authoritative_rows_and_exact_filters(self):
        http=HTTP();r=await functional.evidence(http,{'disease':'T1D','age_min':'18'},'Explain','release')
        self.assertEqual(r['nodes'],[])
        self.assertEqual(r['steps'][0]['functional_metadata']['unique_donors'],2)
        self.assertEqual(len(r['rows']),2)
        self.assertEqual(http.call[1]['params']['disease'],'T1D')
        self.assertEqual(r['steps'][0]['queries'],[])
    async def test_arbitrary_paths_and_overrides_rejected(self):
        for params in ({'donor_ids':'a'},{'url':'http://elsewhere'},{'trace_type':'unknown'},{'age_min':'nan'},{'age_min':'50','age_max':'10'}):
            with self.assertRaises(ValueError):functional.parameters(params,trace=True)
        with self.assertRaises(ValueError):await functional.fetch(HTTP(),'../../secret',{})

class FullTraceTests(unittest.TestCase):
    def test_keeps_all_timepoints_without_graph_sampling(self):
        rows=[{"time_minutes":i,"mean_response":i/10} for i in range(50)]
        body=json.loads(functional.synthesis_body("Explain",{"s1":{"rows":rows}}))
        self.assertEqual(body["evidence"][0]["rows"],rows)
        self.assertEqual(body["evidence"][0]["evidence_id"],"G1")
