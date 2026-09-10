import copy
import unittest
from pankgraph_results.list_layout import relationship_list
from pankgraph_results.layout import LayoutService

def star(count=25, reverse=False):
    nodes = [{"~id": "hub", "~labels": ["anatomical_structure"], "~properties": {"name": "Tissue"}}]
    nodes += [{"~id": str(i), "~labels": ["Sample_node"],
               "~properties": {"id": str(i), "data_modality": "RNA" if i % 2 else "Multiome",
                               "anatomical_structure": "Tissue", "source": "exact_source.tsv"}} for i in range(count-1)]
    edges = [{"~id": "e"+str(i), "~start": str(i) if reverse else "hub",
              "~end": "hub" if reverse else str(i), "~type": "HAS_SAMPLE"} for i in range(count-1)]
    return {"nodes": nodes, "edges": edges}

class ListTests(unittest.TestCase):
    def test_sorted_stable_and_no_mutation(self):
        graph=star(); before=copy.deepcopy(graph)
        result=relationship_list(graph)
        graph["nodes"].reverse(); graph["edges"].reverse()
        self.assertEqual(result,relationship_list(graph))
        self.assertEqual(sorted(before["nodes"],key=lambda n:n["~id"]),sorted(graph["nodes"],key=lambda n:n["~id"]))
        ordered=sorted(result["xy_json"],key=lambda n:result["xy_json"][n]["y"])
        leaves=[n for n in ordered if n!="hub"]
        self.assertTrue(all(int(n)%2==0 for n in leaves[:12]))
        self.assertEqual(before["nodes"][1]["~properties"]["source"],"exact_source.tsv")

    def test_forward_and_reverse_edges_keep_direction(self):
        for reverse in (False,True):
            result=relationship_list(star(reverse=reverse))
            for route in result["edge_routes"].values():
                self.assertEqual(route["list_leaf_endpoint"],"source" if reverse else "target")
                self.assertEqual(route["route_type"],"polyline")

    def test_reject_mixed_labels_types_direction_and_networks(self):
        for field,value in (("~type","OTHER"),("~start","1")):
            graph=star(); graph["edges"][0][field]=value
            self.assertIsNone(relationship_list(graph))
        graph=star(); graph["nodes"][1]["~labels"]=["Gene"]
        self.assertIsNone(relationship_list(graph))
        graph=star(); graph["edges"][0]["~start"]="0"; graph["edges"][0]["~end"]="hub"
        self.assertIsNone(relationship_list(graph))
        self.assertIsNone(relationship_list(star(3)))

    def test_shared_parent_balanced_columns_and_direction(self):
        for reverse in (False, True):
            graph=star(70, reverse)
            for i,n in enumerate(graph['nodes'][1:]):
                n['~labels']=['ontology', 'kegg' if i<54 else 'reactome']
            before=copy.deepcopy(graph)
            result=relationship_list(graph)
            self.assertEqual(result['details']['common_labels'], ['ontology'])
            self.assertEqual(result['details']['groups'], [
                {'labels':['kegg'],'side':'left','count':54},
                {'labels':['reactome'],'side':'right','count':15}])
            self.assertEqual(result['edge_routes'], {})
            for members in (range(54), range(54,69)):
                ys=[result['xy_json'][str(i)]['y'] for i in members]
                self.assertEqual(min(ys)+max(ys), 0)
            for i in range(69):
                point=result['xy_json'][str(i)]
                self.assertEqual(point['x'] < 0, i<54)
            graph['nodes'].reverse();graph['edges'].reverse()
            self.assertEqual(result,relationship_list(graph))
            self.assertEqual(before['edges'][0]['~type'],graph['edges'][-1]['~type'])

    def test_multiple_groups_balance_without_splitting_collections(self):
        graph=star(25)
        labels=['a']*10+['b']*8+['c']*4+['d']*2
        for n,label in zip(graph['nodes'][1:],labels):n['~labels']=['ontology',label]
        result=relationship_list(graph)
        totals={'left':0,'right':0}
        for group in result['details']['groups']:totals[group['side']]+=group['count']
        self.assertEqual(totals,{'left':12,'right':12})
        self.assertEqual(len(result['xy_json']),25)

    def test_25_50_100_rows_do_not_overlap(self):
        for count in (25,50,100):
            result=relationship_list(star(count))
            points=list(result["xy_json"].values())
            self.assertEqual(len(points),count)
            for i,a in enumerate(points):
                for b in points[i+1:]:
                    self.assertTrue(abs(a["x"]-b["x"]) >= (a["width"]+b["width"])/2 or
                                    abs(a["y"]-b["y"]) >= (a["height"]+b["height"])/2)

class ListServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_cache_preserves_evidence_and_mode(self):
        graph=star(55)
        evidence={"nodes":[{"id":n["~id"],"labels":n["~labels"],"properties":n["~properties"]} for n in graph["nodes"]],
                  "edges":[{"start_id":e["~start"],"end_id":e["~end"],"type":e["~type"],"properties":{}} for e in graph["edges"]],
                  "completeness":"complete","graph_version":"test"}
        before=copy.deepcopy(evidence)
        service=LayoutService()
        try:
            for _ in range(2):
                result=await service.layout(evidence,["hub"])
                self.assertEqual(result["layout"]["engine"],"relationship_list")
                self.assertEqual(result["combined_query_result"]["presentation_mode"],"relationship_list")
                self.assertEqual(result["full_evidence"]["node_count"],55)
                self.assertEqual(result["edge_routes"], {})
            self.assertTrue(result["layout"]["cache_hit"])
            self.assertEqual(evidence,before)
        finally: await service.close()
