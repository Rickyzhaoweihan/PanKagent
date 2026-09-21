import copy
import json
import unittest

from pankagent_vnext.answer_facts import build_answer_facts, GO_CODES
from pankagent_vnext.evidence_context import compact_evidence, scientific_excerpt
from pankagent_vnext.release_schema import REGISTRY


def node(identifier, label, **props):
    return {'id':identifier,'labels':[label],'properties':{'id':identifier,**props}}


def edge(a,b,kind,**props):
    return {'start_id':a,'end_id':b,'type':kind,'properties':props}


def evidence(nodes=(),edges=(),**extra):
    return {'step_id':'s1','graph_version':REGISTRY['release'],'status':'complete','truncated':False,
            'nodes':list(nodes),'edges':list(edges),'rows':[],
            'requested_scope':{'constraints':[],'relation_types':[]},**extra}


def proven(item):
    return {'query_scope':{'complete_for_requested_scope':True,
                           'constraints':item['requested_scope']['constraints']}}


class AnswerFactsTests(unittest.TestCase):
    def test_counts_all_samples_before_excerpt_with_distinct_assays_and_histogram(self):
        ns=[node('d1','donor'),node('d2','donor')]
        ns += [node('s'+str(i),'Sample_node',data_modality='snMultiomics' if i<100 else 'CITE-seq Protein',data_source='Lab') for i in range(144)]
        es=[edge('d1' if i<100 else 'd2','s'+str(i),'HAS_SAMPLE') for i in range(144)]
        es += [edge('d1','s0','HAS_SAMPLE')]
        item=evidence(ns,es);original=copy.deepcopy(item)
        result=build_answer_facts(item,coverage=proven(item));facts=result['sample_counts']
        self.assertEqual(facts['unique_retrieved_samples'],144)
        self.assertEqual({g['assay']['value']:g['unique_samples'] for g in facts['by_recorded_assay']},{'snMultiomics':100,'CITE-seq Protein':44})
        hist=facts['donor_sample_distribution'];self.assertEqual(hist['histogram'],[{'matching_samples_per_donor':44,'donors':1},{'matching_samples_per_donor':100,'donors':1}])
        self.assertEqual(hist['distinct_donor_sample_pairs'],144)
        self.assertEqual(hist['donor_sample_link_records'],145)
        self.assertTrue(hist['complete_for_executed_scope'])
        self.assertNotIn('"d1"',json.dumps(result));self.assertNotIn('"s143"',json.dumps(result))
        self.assertEqual(item,original)
        compact=compact_evidence([item])[0]
        self.assertLess(len(compact['nodes']),len(ns))
        self.assertEqual(compact['answer_facts']['sample_counts']['unique_retrieved_samples'],144)

    def test_unknown_assay_is_not_assigned_from_neighbor_or_example(self):
        item=evidence([node('d','donor'),node('s1','Sample_node',data_modality='BCR-seq'),node('s2','Sample_node')],
                      [edge('d','s1','HAS_SAMPLE'),edge('d','s2','HAS_SAMPLE')])
        facts=build_answer_facts(item)['sample_counts']['by_recorded_assay']
        self.assertEqual(sum(g['unique_samples'] for g in facts),2)
        self.assertEqual(next(g for g in facts if g['assay']['state']=='not_recorded')['unique_samples'],1)

    def test_mixed_methods_keep_joint_source_context_and_missing_throughput(self):
        es=[edge('g','a','PHYSICAL_INTERACTION',experimental_system='Two-hybrid',throughput='High Throughput',data_source='BioGRID'),
            edge('g','b','PHYSICAL_INTERACTION',experimental_system='Affinity Capture-MS',throughput='Low Throughput',data_source='BioGRID'),
            edge('g','c','PHYSICAL_INTERACTION',experimental_system='Protein-peptide')]
        facts=build_answer_facts(evidence(edges=es))['physical_interaction_methods']
        self.assertEqual(facts['full_record_count'],3);self.assertEqual(facts['full_group_count'],3)
        self.assertEqual({g['recorded_fields']['throughput']['value'] for g in facts['groups']},{'High Throughput','Low Throughput',None})
        self.assertIn('Mixed groups',facts['interpretation'])

    def test_omitted_method_groups_retain_full_totals(self):
        es=[edge('g',str(i),'PHYSICAL_INTERACTION',experimental_system='method'+str(i)) for i in range(50)]
        facts=build_answer_facts(evidence(edges=es),max_groups=2)['physical_interaction_methods']
        self.assertEqual(facts['full_record_count'],50);self.assertEqual(facts['omitted_group_count'],48)
        self.assertEqual(facts['omitted_record_count'],48)

    def test_lead_role_is_record_specific_not_inferred_from_membership(self):
        ns=[node('rs1','variants'),node('g','Gene'),node('d','disease')]
        es=[edge('rs1','d','PART_OF_GWAS_SIGNAL',lead_status='nonlead',pip_rank=6,data_source='GWAS'),
            edge('rs1','g','PART_OF_QTL_SIGNAL',pip=.9,data_source='exon; INSPIRE'),
            edge('g','d','SIGNAL_COLOC_WITH',gwas_lead_vars='rs2',qtl_lead_vars='rs1',data_source='HIRN',coloc_dataset='t1d_exonQTL-inspire_coloc')]
        facts=build_answer_facts(evidence(ns,es))['signal_roles']['records']
        self.assertEqual(facts[0]['lead_role'],'recorded_nonlead')
        self.assertEqual(facts[1]['lead_role'],'not_established')
        self.assertEqual(facts[2]['gwas_lead_variant_ids'],['rs2'])
        self.assertEqual(facts[2]['qtl_lead_variant_ids'],['rs1'])
        self.assertEqual(facts[2]['shared_recorded_lead_variant_ids'],[])
        self.assertIs(facts[2]['same_complete_lead_set'],False)
        self.assertEqual(facts[2]['recorded_source']['value'],'HIRN')

    def test_partial_multi_lead_overlap_not_same_lead_set(self):
        row=build_answer_facts(evidence(edges=[edge('g','d','SIGNAL_COLOC_WITH',gwas_lead_vars='rs1;rs2',qtl_lead_vars=['rs2','rs3'])]))['signal_roles']['records'][0]
        self.assertEqual(row['shared_recorded_lead_variant_ids'],['rs2'])
        self.assertFalse(row['same_complete_lead_set'])
        unknown=build_answer_facts(evidence(edges=[edge('g','d','SIGNAL_COLOC_WITH',gwas_lead_vars='unknown',qtl_lead_vars='rs2')]))['signal_roles']['records'][0]
        self.assertIsNone(unknown['same_complete_lead_set'])
        self.assertIsNone(unknown['shared_recorded_lead_variant_ids'])

    def test_go_codes_formal_and_only_recorded(self):
        ns=[node('g','Gene'),node('go1','GO_term',go_domain='molecular_function'),node('go2','GO_term',go_domain='biological_process')]
        es=[edge('g','go1','ASSOCIATED_WITH_GO',go_evidence_code='IDA'),edge('g','go1','ASSOCIATED_WITH_GO',go_evidence_code='TAS'),edge('g','go2','ASSOCIATED_WITH_GO',go_evidence_code='UNKNOWN')]
        facts=build_answer_facts(evidence(ns,es))['go_annotations'];codes={row['code']:row for row in facts['recorded_codes']}
        self.assertEqual(codes['IDA']['formal_name'],'Inferred from Direct Assay')
        self.assertEqual(codes['TAS']['category'],'author statement')
        self.assertIsNone(codes['UNKNOWN']['formal_name']);self.assertNotIn('IEA',codes)
        self.assertEqual(facts['full_record_count'],3);self.assertEqual(facts['unique_recorded_term_ids'],2)
        self.assertEqual(GO_CODES['IBA']['formal_name'],'Inferred from Biological aspect of Ancestor')
        self.assertEqual(GO_CODES['ISS']['formal_name'],'Inferred from Sequence or structural Similarity')

    def test_failed_or_truncated_cannot_claim_scope_complete(self):
        for status,truncated in [('failed',False),('complete',True),('complete',None)]:
            item=evidence([node('d','donor'),node('s','Sample_node')],[edge('d','s','HAS_SAMPLE')],status=status,truncated=truncated)
            self.assertFalse(build_answer_facts(item,coverage=proven(item))['complete_for_executed_scope'])
        item=evidence([node('g','Gene')]);self.assertFalse(build_answer_facts(item)['complete_for_executed_scope'])
        self.assertFalse(build_answer_facts(item,coverage={'query_scope':{'complete_for_requested_scope':True,'constraints':[{'x':1}]}})['complete_for_executed_scope'])

    def test_donor_only_query_does_not_create_zero_sample_distribution(self):
        item=evidence([node('d','donor')])
        self.assertNotIn('sample_counts',build_answer_facts(item))

    def test_scalar_sample_results_never_become_zero_enumerated_samples(self):
        from pankagent_vnext.evidence_coverage import build_evidence_coverage
        query=('MATCH (a:anatomical_structure)-[r:HAS_SAMPLE]->(s:Sample_node) '
               'RETURN count(DISTINCT s) AS result')
        spec={'relation_types':['HAS_SAMPLE'],'constraints':[],'complete':True}
        for value in (823, 0):
            with self.subTest(value=value):
                item=evidence(rows=[{'result':value}],requested_scope=copy.deepcopy(spec),
                              queries=[{'cypher':query,'parameters':{}}])
                coverage=build_evidence_coverage(spec,item,graph_version=REGISTRY['release'],
                    query=query,parameters={},validation_verified=True)
                # HAS_SAMPLE is outside the measurement projection classifier;
                # row shape, not a projection label or alias, proves that no
                # sample identities were enumerated for this count query.
                self.assertEqual(coverage['result_representation'],'not_applicable')
                self.assertFalse(coverage['record_membership_enumerated'])
                self.assertTrue(coverage['query_scope']['complete_for_requested_scope'])
                item['evidence_coverage']=coverage
                before=copy.deepcopy(item)
                facts=build_answer_facts(item,coverage=coverage)['sample_counts']
                self.assertEqual(facts['enumeration_state'],'sample_identities_not_returned')
                self.assertFalse(facts['complete_sample_enumeration_verified'])
                self.assertNotIn('unique_retrieved_samples',facts)
                self.assertNotIn('by_recorded_assay',facts)
                self.assertNotIn('donor_sample_distribution',facts)
                excerpt=scientific_excerpt(compact_evidence([item]))[0]
                self.assertEqual(excerpt['rows'],[{'result':value}])
                self.assertEqual(excerpt['answer_facts']['sample_counts'],facts)
                self.assertEqual(item,before)

    def test_scalar_projection_is_not_interpreted_by_alias_or_value(self):
        for rows in ([{'sample_count':.5}], [{'unknown_alias':None}], [{'value':2},{'value':3}]):
            with self.subTest(rows=rows):
                item=evidence(rows=rows,requested_scope={'relation_types':['HAS_SAMPLE'],'constraints':[]})
                facts=build_answer_facts(item)['sample_counts']
                self.assertNotIn('unique_retrieved_samples',facts)
                self.assertNotIn('donor_sample_distribution',facts)
                self.assertEqual(item['rows'],rows)

    def test_donor_scalar_zero_and_positive_counts_do_not_invent_samples(self):
        for value in (0, 27):
            item=evidence(rows=[{'donor_count':value}],
                          requested_scope={'relation_types':['HAS_DONOR'],'constraints':[]})
            self.assertNotIn('sample_counts',build_answer_facts(item))
            self.assertEqual(scientific_excerpt(compact_evidence([item]))[0]['rows'],[{'donor_count':value}])

    def test_empty_enumerated_sample_result_keeps_verified_zero(self):
        item=evidence(status='empty',requested_scope={'relation_types':['HAS_SAMPLE'],'constraints':[]})
        facts=build_answer_facts(item,coverage=proven(item))['sample_counts']
        self.assertEqual(facts['unique_retrieved_samples'],0)
        self.assertEqual(facts['donor_sample_distribution']['histogram'],[])
        self.assertTrue(facts['donor_sample_distribution']['complete_for_executed_scope'])

    def test_tissue_sample_links_do_not_invent_donor_distribution(self):
        item=evidence([node('a','anatomical_structure'),node('s','Sample_node',data_modality='snMultiomics')],[edge('a','s','HAS_SAMPLE')])
        dist=build_answer_facts(item)['sample_counts']['donor_sample_distribution']
        self.assertEqual(dist['unique_linked_donors'],0);self.assertEqual(dist['histogram'],[])
        self.assertEqual(dist['samples_without_a_returned_donor_link'],1)

    def test_conflicting_node_metadata_not_arbitrarily_selected(self):
        item=evidence([node('s','Sample_node',data_modality='A'),node('s','Sample_node',data_modality='B')],
                      [edge('a','s','HAS_SAMPLE')])
        result=build_answer_facts(item,coverage=proven(item))
        self.assertEqual(result['conflicting_node_identities'],1);self.assertFalse(result['complete_for_executed_scope'])
        self.assertEqual(result['sample_counts']['unique_retrieved_samples'],0)

    def test_aggregate_excerpt_hides_donor_examples_but_raw_and_explicit_list_survive(self):
        item=evidence([node('private-donor','donor',age=42),node('private-sample','Sample_node',data_modality='BCR-seq'),node('a','anatomical_structure')],
            [edge('private-donor','private-sample','HAS_SAMPLE'),edge('a','private-sample','HAS_SAMPLE')],
            donor_summary={'unique_donors':1,'unique_samples':1,'rows':[{'donor_id':'private-donor','sample_count':1}]})
        before=copy.deepcopy(item);compact=compact_evidence([item]);aggregate=scientific_excerpt(compact)
        self.assertNotIn('private-donor',json.dumps(aggregate));self.assertNotIn('private-sample',json.dumps(aggregate))
        self.assertEqual(aggregate[0]['answer_facts']['sample_counts']['unique_retrieved_samples'],1)
        self.assertEqual(item,before)
        details=scientific_excerpt(compact,include_donor_details=True)
        self.assertIn('private-donor',json.dumps(details));self.assertIn('private-sample',json.dumps(details))

    def test_wrong_release_and_missing_sources_are_not_filled(self):
        self.assertIsNone(build_answer_facts(evidence(graph_version='other')))
        row=build_answer_facts(evidence(edges=[edge('g','d','SIGNAL_COLOC_WITH')]))['signal_roles']['records'][0]
        self.assertEqual(row['recorded_source']['state'],'not_recorded')
        self.assertIsNone(row['gwas_lead_variant_ids'])


if __name__ == '__main__':
    unittest.main()


def test_sample_only_aggregate_excerpt_omits_individual_records():
    item=evidence([node('t','anatomical_structure'),
        node('sample-private-id','Sample_node',data_modality='snMultiomics',contact='private-contact')],
        [edge('t','sample-private-id','HAS_SAMPLE')])
    compact=compact_evidence([item])
    excerpt=scientific_excerpt(compact)[0]
    assert 'sample-private-id' not in json.dumps(excerpt)
    assert 'private-contact' not in json.dumps(excerpt)
    assert excerpt['answer_facts']['sample_counts']['unique_retrieved_samples'] == 1
    assert scientific_excerpt(compact,include_donor_details=True)[0]['nodes'] == compact[0]['nodes']
    assert item['nodes'][1]['id'] == 'sample-private-id'
