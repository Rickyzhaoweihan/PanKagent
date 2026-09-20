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

class ResultPlotTests(unittest.IsolatedAsyncioTestCase):
    async def test_square_format_keeps_cohort_filters(self):
        http=HTTP()
        await functional.fetch(http,'api/charts/cohort-traces.png',{'trace_type':'ins_ieq','age_min':'3','age_max':'68','result_page':'Yes'})
        self.assertEqual(http.call[1]['params'],{'trace_type':'ins_ieq','age_min':'3','age_max':'68','result_page':'Yes'})
        for path, value in [('api/charts/cohort-traces','Yes'),('api/charts/cohort-traces.png','invalid')]:
            with self.assertRaises(ValueError):await functional.fetch(http,path,{'result_page':value})

    async def test_association_chart_preserves_axis_and_cohort_filters(self):
        # The functional service OpenAPI and native frontend both use x_key,
        # including BMI; dropping it silently falls back to the age axis.
        params={'x_key':'bmi','y_trait':'INS-IEQ G 16.7 AUC','disease':'T1D',
                'sex':'F','center':'Penn','age_min':'18','age_max':'65','bmi_max':'30'}
        for path in ('api/charts/association','api/charts/association.png'):
            with self.subTest(path=path):
                http=HTTP()
                await functional.fetch(http,path,params)
                self.assertEqual(http.call[0],('GET',functional.BASE+'/'+path))
                self.assertEqual(http.call[1]['params'],params)

    async def test_association_axis_keeps_existing_input_boundaries(self):
        for params in ({'x_key':'x'*201},{'x_key':'age\n'},
                       {'x_key':'age','donor_ids':'private'},
                       {'x_key':'age','url':'http://elsewhere'},
                       {'x_key':'age','age_min':'65','age_max':'18'}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                await functional.fetch(HTTP(),'api/charts/association',params)
        with self.assertRaises(ValueError):
            functional.parameters({'x_key':'bmi'},trace=True)
