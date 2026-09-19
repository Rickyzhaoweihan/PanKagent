import copy
import json
import unittest

from pankagent_vnext.evidence_context import MAX_BYTES, compact_evidence


def node(identifier, label="Gene", **properties):
    return {"id": identifier, "labels": [label], "properties": {"id": identifier, "name": identifier, **properties}}


def edge(source, target, kind="COMMON", **properties):
    return {"start_id": source, "end_id": target, "type": kind, "properties": properties}


def evidence(nodes=None, edges=None, rows=None, **extra):
    return {"status": "complete", "graph_version": "test-release", "truncated": False,
            "nodes": nodes or [], "edges": edges or [], "rows": rows or [], **extra}


class EvidenceContextTests(unittest.TestCase):
    def test_primary_and_related_evidence_roles_survive_compaction(self):
        result = compact_evidence({
            "primary": evidence([node("gene"), node("cell", "anatomical_structure")],
                [edge("gene", "cell", "GENE_ENRICHED_IN", condition="ND")],
                step_id="primary", purpose="primary", title="Check enrichment"),
            "related": evidence([node("gene"), node("other", "anatomical_structure")],
                [edge("gene", "other", "GENE_DETECTED_IN", condition="T2D")],
                step_id="related", purpose="context", context_for="primary", title="Check detection elsewhere"),
        })
        self.assertEqual([item["purpose"] for item in result], ["primary", "context"])
        self.assertEqual(result[1]["context_for"], "primary")
        self.assertEqual([item["evidence_id"] for item in result], ["G1", "G2"])
        self.assertEqual([item["edges"][0]["properties"]["condition"] for item in result], ["ND", "T2D"])

    def test_rare_edge_after_first_hundred_survives_with_labeled_endpoints(self):
        nodes = [node("g" + str(i)) for i in range(150)] + [node("cell", "anatomical_structure")]
        edges = [edge("g" + str(i), "cell") for i in range(130)]
        edges.append(edge("g149", "cell", "DECISIVE", score=0.02))
        result = compact_evidence([evidence(nodes, edges)])[0]
        rare = next(item for item in result["edges"] if item["type"] == "DECISIVE")
        self.assertEqual(rare["start_id"], "g149")
        visible = {item["id"]: item for item in result["nodes"]}
        self.assertTrue(visible["g149"]["context_stub"])
        self.assertEqual(visible["g149"]["properties"]["name"], "g149")
        self.assertEqual(visible["g149"]["labels"], ["Gene"])
        self.assertEqual(result["edges_count"], 131)
        self.assertEqual(result["context_dropped"]["edges_by_type"], {"COMMON": 31})
        self.assertEqual(len(result["edges"]), 100)
        self.assertTrue(result["context_sampled"])
        self.assertEqual(result["status"], "complete")

    def test_equal_size_common_groups_keep_both_types(self):
        edges = [edge("g", "cell", kind, rank=i) for kind in ["TYPE_A", "TYPE_B"] for i in range(110)]
        result = compact_evidence([evidence([node("g"), node("cell")], edges)])[0]
        self.assertEqual(sum(e["type"] == "TYPE_A" for e in result["edges"]), 50)
        self.assertEqual(sum(e["type"] == "TYPE_B" for e in result["edges"]), 50)

    def test_rare_node_labels_after_first_sixty_survive(self):
        nodes = [node(str(i)) for i in range(80)] + [node("rare", "GO_term")]
        result = compact_evidence([evidence(nodes)])[0]
        self.assertIn("rare", {item["id"] for item in result["nodes"]})
        self.assertEqual(result["nodes_count"], 81)
        self.assertEqual(len(result["nodes"]), 60)
        self.assertEqual(result["context_dropped"]["nodes_by_labels"], {"Gene": 21})

    def test_previous_step_endpoint_gets_a_named_explicit_stub(self):
        steps = {
            "first": evidence([node("prior", "Gene")]),
            "second": evidence([node("cell", "anatomical_structure")], [edge("prior", "cell", "GENE_DETECTED_IN")]),
        }
        result = compact_evidence(steps)
        stub = next(n for n in result[1]["nodes"] if n["id"] == "prior")
        self.assertEqual(stub["id"], "prior")
        self.assertTrue(stub["context_stub"])
        self.assertEqual(stub["context_stub_reason"], "endpoint_from_other_step")
        self.assertEqual(stub["properties"]["name"], "prior")
        self.assertEqual(stub["labels"], ["Gene"])
        self.assertEqual(result[1]["context_cross_step_endpoints"], 1)
        self.assertEqual([r["evidence_id"] for r in result], ["G1", "G2"])

    def test_unknown_endpoint_or_other_release_never_gets_invented_node_facts(self):
        items = [evidence([node("g", name="Other release name")], graph_version="other"),
                 evidence([node("cell")], [edge("g", "cell")])]
        result = compact_evidence(items)[1]
        stub = next(n for n in result["nodes"] if n["id"] == "g")
        self.assertEqual(stub["context_stub_reason"], "endpoint_missing_from_evidence")
        self.assertEqual(stub["properties"], {"id": "g"})
        self.assertEqual(stub["labels"], [])
        self.assertEqual(result["context_missing_endpoint_nodes"], 1)

    def test_empty_and_failed_steps_are_not_dropped_or_renumbered(self):
        result = compact_evidence({
            "a": evidence(status="empty", question="Empty lookup"),
            "b": evidence(status="failed", error="timeout", question="Failed lookup"),
        })
        self.assertEqual([r["status"] for r in result], ["empty", "failed"])
        self.assertEqual([r["evidence_id"] for r in result], ["G1", "G2"])
        self.assertEqual(result[1]["error"], "timeout")
        self.assertEqual(result[0]["nodes_count"], 0)
        self.assertFalse(result[0]["context_sampled"])

    def test_sampling_is_deterministic_and_does_not_mutate_evidence(self):
        item = evidence([node(str(i), "A" if i < 60 else "B") for i in range(100)],
                        [edge(str(i), str(i + 1), "LINK", score=i) for i in range(99)])
        original = copy.deepcopy(item)
        first = compact_evidence({"step": item})
        second = compact_evidence({"step": item})
        self.assertEqual(first, second)
        self.assertEqual(item, original)

    def test_raw_validation_queries_and_statement_messages_are_excluded(self):
        item = evidence(validation=[{"valid": False, "n": 1,
            "candidate_cypher": "PRIVATE_RAW_QUERY", "parameters": {"private": "PARAMETER"},
            "reasons": ["cypher_explain_failed:Neo.ClientError.Statement.SyntaxError:PRIVATE_RAW_QUERY"]}])
        result = compact_evidence([item])[0]
        self.assertNotIn("PRIVATE_RAW_QUERY", json.dumps(result))
        self.assertNotIn("PARAMETER", json.dumps(result))
        self.assertEqual(result["validation"][0]["reasons"], ["cypher_explain_failed:Neo.ClientError.Statement.SyntaxError"])
        self.assertNotIn("n", result["validation"][0])

    def test_recovered_generation_attempts_are_not_missing_graph_evidence(self):
        item = evidence([node("g")], validation=[
            {"valid": False, "n": 1, "candidate_cypher": "REJECTED_QUERY",
             "reasons": ["incomplete_limit_or_slice"]},
            {"valid": True, "n": 8, "candidate_cypher": "ACCEPTED_QUERY", "reasons": []},
        ])
        original = copy.deepcopy(item)
        result = compact_evidence([item])[0]
        self.assertEqual(result["validation"], [{"valid": True, "reasons": []}])
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["truncated"])
        self.assertFalse(result["context_sampled"])
        self.assertNotIn("context_content_omissions", result)
        self.assertNotIn("incomplete_limit_or_slice", json.dumps(result))
        self.assertNotIn("QUERY", json.dumps(result))
        self.assertEqual(item, original)

    def test_arbitrarily_many_recovered_checks_do_not_mark_context_sampled(self):
        checks = [{"valid": False, "n": 8, "reasons": ["rejected_candidate"]} for _ in range(100)]
        checks.append({"valid": True, "n": 8, "status": "accepted", "reasons": []})
        result = compact_evidence([evidence([node("g")], validation=checks)])[0]
        self.assertEqual(result["validation"], [{"valid": True, "status": "accepted", "reasons": []}])
        self.assertFalse(result["context_sampled"])
        self.assertNotIn("context_content_omissions", result)

    def test_execution_failure_after_accepted_query_is_terminal_validation(self):
        checks = [{"valid": True, "n": 8, "reasons": []},
                  {"valid": False, "reasons": ["graph_execution_failed:ServiceUnavailable"]},
                  None]
        result = compact_evidence([evidence(status="failed", validation=checks)])[0]
        self.assertEqual(result["validation"], [{"valid": False, "reasons": ["graph_execution_failed:ServiceUnavailable"]}])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["context_sampled"])

    def test_multikilobyte_property_fields_are_labeled_and_json_safe(self):
        result = compact_evidence([evidence([node("g", description="多" * 10000)])])[0]
        self.assertIn("[context text clipped]", result["nodes"][0]["properties"]["description"])
        self.assertEqual(result["nodes"][0]["id"], "g")
        self.assertTrue(result["context_sampled"])
        self.assertEqual(result["context_content_omissions"]["clipped_strings"], 1)
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False)), result)

    def test_oversized_context_exposes_only_node_identity_fields(self):
        properties = {"measurement_" + str(i): "x" * 200 for i in range(20)}
        edges = [edge("g", "cell", "COMMON", **properties) for _ in range(110)]
        edges.append(edge("g", "cell", "RARE", **properties))
        item = evidence([node("g"), node("cell")], edges, rows=[{"v": i} for i in range(50)])
        result = compact_evidence([item])[0]
        self.assertEqual(result["context_compaction"], "node_identity_only")
        self.assertNotIn("edges", result)
        self.assertNotIn("rows", result)
        self.assertEqual(result["context_dropped"]["edges"], 111)
        self.assertEqual(result["context_dropped"]["rows"], 50)
        self.assertEqual(set(result['nodes'][0]), {'id', 'type', 'description', 'source'})
        self.assertLessEqual(len(json.dumps([result], ensure_ascii=False, separators=(",", ":")).encode()), MAX_BYTES)

    def test_stable_identifiers_are_never_silently_shortened(self):
        huge_id = "g" * (MAX_BYTES + 1)
        result = compact_evidence([evidence([node(huge_id)])])[0]
        self.assertEqual(result['nodes'], [])
        self.assertEqual(result['context_dropped']['nodes'], 1)

    def test_total_bound_applies_across_steps(self):
        items = [evidence(question="x" * 1200) for _ in range(200)]
        # Public plans are capped at twelve checks. Do not silently drop or
        # renumber arbitrary extra check envelopes to meet the size bound.
        with self.assertRaisesRegex(ValueError, 'evidence_step_envelope_too_large'):
            compact_evidence(items)

    def test_invalid_shape_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "invalid_evidence_node"):
            compact_evidence([evidence(nodes=[{"labels": ["Gene"]}])])
        with self.assertRaisesRegex(ValueError, "invalid_evidence_shape"):
            compact_evidence("not a list")


if __name__ == "__main__":
    unittest.main()


def test_scientific_excerpt_preserves_scope_without_operational_diagnostics():
    from pankagent_vnext.evidence_context import scientific_excerpt
    source=[{'context_sampled':True,'context_compaction':'reduced','context_dropped':{'nodes':40},
             'nodes_count':126,'donor_summary':{'unique_donors':126,'unique_samples':190},
             'nodes':[{'id':'x','context_stub':True,'properties':{'name':'Example'}}]}]
    result=scientific_excerpt(source)
    assert result[0]['donor_summary']['unique_donors']==126
    assert result[0]['answer_evidence_scope']['individual_records_are_selected_examples']
    assert 'context_compaction' not in str(result) and 'context_stub' not in str(result)
    assert source[0]['context_sampled'] is True


def test_totals_distinguish_focal_gene_cells_and_annotation_records():
    nodes=[{'id':'g','labels':['GENE'],'properties':{}},
           {'id':'c1','labels':['CellType'],'properties':{}},
           {'id':'c2','labels':['CellType'],'properties':{}}]
    edges=[{'start_id':'g','end_id':target,'type':'DETECTED_IN','properties':{}}
           for target in ('c1','c1','c2')]
    result=compact_evidence({'s1':{'nodes':nodes,'edges':edges,'rows':[]}})[0]
    assert result['evidence_totals']['distinct_nodes_by_label']=={'CellType':2,'GENE':1}
    assert result['evidence_totals']['relationships']['DETECTED_IN']=={
        'records':3,'unique_start_entities':1,'unique_end_entities':2}


def interaction_evidence():
    from pankagent_vnext.evidence_coverage import build_evidence_coverage
    nodes = [node('focal')] + [node('p' + str(index)) for index in range(26)]
    edges = [edge('p' + str(index), 'focal', 'PHYSICAL_INTERACTION') for index in range(18)]
    edges += [edge('focal', 'p' + str(index), 'PHYSICAL_INTERACTION') for index in range(18, 26)]
    edges += [edge('p' + str(index), 'focal', 'PHYSICAL_INTERACTION', experiment='repeat') for index in range(5)]
    constraint = {'entity_type': 'Gene', 'property': 'id', 'operator': '=', 'value': 'focal'}
    query = 'MATCH (g:Gene)-[r:PHYSICAL_INTERACTION]-(p:Gene) WHERE g.id=$gene RETURN g,r,p'
    item = evidence(nodes, edges, graph_version='PanKgraph_08_04',
                    requested_scope={'constraints': [constraint], 'relation_types': ['PHYSICAL_INTERACTION'], 'complete': True},
                    queries=[{'cypher': query, 'parameters': {'gene': 'focal'}}])
    item['evidence_coverage'] = build_evidence_coverage(item['requested_scope'], item,
        graph_version=item['graph_version'], query=query, parameters={'gene': 'focal'}, validation_verified=True)
    return item


def test_oversized_interaction_view_does_not_expose_hidden_measurement_totals(monkeypatch):
    import pankagent_vnext.evidence_context as module
    source = interaction_evidence(); before = copy.deepcopy(source)
    monkeypatch.setattr(module, 'TARGET_BYTES', 1)
    result = module.scientific_excerpt(compact_evidence([source]))[0]
    assert source == before
    assert 'evidence_totals' not in result and 'edges' not in result
    assert result['answer_evidence_scope']['mode'] == 'node_identity_only'
    assert result['answer_evidence_scope']['relationship_and_measurement_evidence_available'] is False


def test_interaction_focal_name_requires_unique_verified_release_resolution():
    source = interaction_evidence()
    c = source['requested_scope']['constraints'][0]
    c.update(property='name', value='FocalAlias')
    assert compact_evidence([source])[0]['evidence_totals']['relationships']['PHYSICAL_INTERACTION']['unique_partner_genes'] is None
    source['resolved_entities'] = [{'constraint_index': 0, 'state': 'resolved', 'entity_type': 'Gene',
        'graph_version': source['graph_version'], 'id': 'focal', 'requested': copy.deepcopy(c)}]
    assert compact_evidence([source])[0]['evidence_totals']['relationships']['PHYSICAL_INTERACTION']['unique_partner_genes'] == 26
    source['resolved_entities'][0]['graph_version'] = 'old'
    assert compact_evidence([source])[0]['evidence_totals']['relationships']['PHYSICAL_INTERACTION']['unique_partner_genes'] is None


def test_interaction_multiple_focal_genes_or_missing_scope_cannot_invent_partner_total():
    source = interaction_evidence()
    source['requested_scope']['constraints'].append({'entity_type': 'Gene', 'property': 'id', 'value': 'p1'})
    assert compact_evidence([source])[0]['evidence_totals']['relationships']['PHYSICAL_INTERACTION']['unique_partner_genes'] is None
    source.pop('requested_scope')
    assert compact_evidence([source])[0]['evidence_totals']['relationships']['PHYSICAL_INTERACTION']['unique_partner_genes'] is None


def test_interaction_self_records_and_both_directions_do_not_double_count_partner_genes():
    source = interaction_evidence()
    source['edges'].extend([edge('focal', 'focal', 'PHYSICAL_INTERACTION'), edge('focal', 'p0', 'PHYSICAL_INTERACTION')])
    total = compact_evidence([source])[0]['evidence_totals']['relationships']['PHYSICAL_INTERACTION']
    assert total['records'] == 33 and total['unique_partner_genes'] == 26
    assert total['self_interaction_records'] == 1
    assert total['complete_for_requested_scope'] is False  # Existing coverage describes the earlier rows.


def test_interaction_partial_retrieval_totals_never_claim_complete_scope():
    for mutation in ('truncated', 'failed', 'missing_coverage', 'unrelated_edge'):
        source = interaction_evidence()
        if mutation == 'truncated': source['truncated'] = True
        elif mutation == 'failed': source['status'] = 'failed'
        elif mutation == 'missing_coverage': source.pop('evidence_coverage')
        else: source['edges'].append(edge('p0', 'p1', 'PHYSICAL_INTERACTION'))
        total = compact_evidence([source])[0]['evidence_totals']['relationships']['PHYSICAL_INTERACTION']
        assert total['unique_partner_genes'] == 26
        assert total['complete_for_requested_scope'] is False
        assert total['count_scope'] == 'all_retrieved_records_before_excerpt_selection'


def test_interaction_unverified_partner_node_types_do_not_claim_gene_counts():
    source = interaction_evidence(); source['nodes'][-1]['labels'] = ['unknown']
    total = compact_evidence([source])[0]['evidence_totals']['relationships']['PHYSICAL_INTERACTION']
    assert total['unique_partner_genes'] is None and total['unique_partner_entities'] == 26
    assert total['partner_count_state'] == 'partner_gene_labels_unverified'
    assert total['complete_for_requested_scope'] is False


def test_scientific_excerpt_hides_presentation_markers_without_erasing_source_missingness():
    from pankagent_vnext.evidence_context import scientific_excerpt
    source = [{'nodes_count': 2, 'evidence_coverage': {'query_scope': {'complete_for_requested_scope': True}},
        'nodes': [{'id': 'd1', 'labels': ['donor'], 'context_stub': True,
                   'endpoint_stub': True, 'endpoint_stub_reason': 'display_only',
                   'properties': {'id': 'd1', 'data_source': 'Source study', 'source_row': 17,
                                  'recorded_measurement': None, 'context_stub': True}},
                  {'id': 'd2', 'labels': ['donor'], 'properties': {'id': 'd2', 'recorded_measurement': 2.5}}],
        'context_sampled': True}]
    before = copy.deepcopy(source)
    result = scientific_excerpt(source, include_donor_details=True)
    assert source == before
    assert source[0]['nodes'][0]['endpoint_stub'] is True
    assert len(source[0]['nodes']) == len(result[0]['nodes']) == 2
    assert 'endpoint_stub' not in json.dumps(result) and 'context_stub' not in json.dumps(result)
    assert result[0]['evidence_coverage']['query_scope']['complete_for_requested_scope'] is True
    properties = result[0]['nodes'][0]['properties']
    assert properties['data_source'] == 'Source study' and properties['source_row'] == 17
    assert 'recorded_measurement' in properties and properties['recorded_measurement'] is None
    assert result[0]['nodes'][1]['properties']['recorded_measurement'] == 2.5
    assert 'do not establish missing metadata' in result[0]['answer_evidence_scope']['metadata_rule']
