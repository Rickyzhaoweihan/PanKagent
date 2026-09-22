"""Presentation changes cannot weaken the verified evidence contract."""
from copy import deepcopy

from pankagent_vnext.answer_blocks import catalogue, render, fallback
from pankagent_vnext.release_schema import REGISTRY
from tests_vnext.test_answer_synthesis import detection_evidence


def test_specificity_leads_with_uncertainty_without_inventing_negatives():
    step = detection_evidence()['s1']
    step['question'] = 'Is INS expression restricted to beta cells?'
    step['evidence_id'] = 'G4'
    original = deepcopy(step)
    answer = fallback(catalogue([step]))
    assert answer.startswith('The available RNA detection evidence cannot establish cell-type exclusivity.')
    assert 'measured and found absent' in answer
    assert 'threshold' not in answer and not answer.startswith('No')
    assert '[G4]' in answer and step == original
    step.update(status='partial', truncated=True)
    partial = fallback(catalogue([step]))
    assert 'Coverage is incomplete' in partial
    assert 'complete search' not in partial


def test_identical_caveats_merge_without_losing_citations_or_different_scope():
    a = detection_evidence()['s1']; a['evidence_id'] = 'G1'
    b = deepcopy(a); b['evidence_id'] = 'G2'
    facts = catalogue([a, b])
    answer = render({'fact_ids': []}, facts)
    assert answer.count('RNA detection describes expression') == 1
    assert '[G1] [G2]' in answer
    b.update(status='failed', nodes=[], edges=[], rows=[])
    answer = fallback(catalogue([a, b]))
    assert 'evidence could not be retrieved' in answer
    assert '[G1]' in answer and '[G2]' in answer


def test_readable_metrics_keep_values_mean_median_and_assay_separate():
    step = detection_evidence()['s1']; step['evidence_id'] = 'G1'
    edge = step['edges'][0]; edge['type'] = 'GENE_ACTIVITY_SCORE_IN'
    edge['properties'] = {'type_1_diabetes_ocr_gene_activity_score_mean': 49.6068,
                          'non_diabetic_ocr_gene_activity_score_mean': 52.9265,
                          'data_source': 'source_exact_v2'}
    facts = catalogue([step])
    answer = render({'fact_ids': [f['id'] for f in facts]}, facts)
    assert 'mean ATAC gene activity in T1D is 49.6068, lower than non-diabetic samples (52.9265)' in answer
    assert 'Source: source\\_exact\\_v2' in answer
    assert 'ATAC gene activity is accessibility-derived, not RNA expression' in answer
    assert 'type\\_1\\_diabetes' not in answer


def test_deduplication_never_combines_distinct_values_or_disappears_mandatory_facts():
    facts = [dict(id=f'G{i}:a', evidence_id=f'G{i}', kind='comparison',
                  text=f'Mean: {value}.', mandatory=True)
             for i, value in [(1, 1.5), (2, 2.5)]]
    answer = render({'fact_ids': []}, facts)
    assert 'Mean: 1.5. [G1]' in answer and 'Mean: 2.5. [G2]' in answer


def test_coloc_signal_accounting_keeps_shared_gwas_distinct_from_row_references():
    step = {'evidence_id':'G1', 'status':'complete', 'truncated':False,
            'graph_version':REGISTRY['release'], 'nodes':[
                {'id':'gene','labels':['Gene'],'properties':{'id':'gene'}},
                {'id':'t1d','labels':['disease'],'properties':{'id':'t1d'}},
            ], 'edges':[
                {'start_id':'gene','end_id':'t1d','type':'SIGNAL_COLOC_WITH','properties':{
                    'gwas_signal_id':'MAPT__credibleSet1__selected',
                    'qtl_signal_id':'PLEKHM1__exon__credibleSet3'}},
                {'start_id':'gene','end_id':'t1d','type':'SIGNAL_COLOC_WITH','properties':{
                    'gwas_signal_id':'MAPT__credibleSet1__selected',
                    'qtl_signal_id':'PLEKHM1__credibleSet1'}},
            ], 'rows':[]}
    facts = catalogue([step])
    fact = next(item for item in facts if item['kind'] == 'coloc_signal_counts')
    assert fact['mandatory']
    assert '2 recorded relationship rows' in fact['text']
    assert '2 GWAS signal references span 1 distinct recorded GWAS signal ID' in fact['text']
    assert '2 QTL signal references span 2 distinct recorded QTL signal IDs' in fact['text']
    answer = render({'fact_ids': []}, facts)
    assert fact['text'] in answer
    assert '2 distinct recorded GWAS signal IDs' not in answer


def test_coloc_rows_preserve_links_loci_h4_and_missing_tissue_context():
    step = {'evidence_id':'G1', 'status':'complete', 'truncated':False,
            'graph_version':REGISTRY['release'], 'nodes':[
                {'id':'gene','labels':['Gene'],
                 'properties':{'id':'gene','name':'PLEKHM1'}},
                {'id':'t1d','labels':['disease'],
                 'properties':{'id':'t1d','name':'type 1 diabetes'}},
            ], 'edges':[
                {'start_id':'gene','end_id':'t1d','type':'SIGNAL_COLOC_WITH','properties':{
                    'gwas_signal_id':'MAPT__credibleSet1__selected',
                    'qtl_signal_id':'PLEKHM1__credibleSet1',
                    'gwas_lead_vars':'rs35327136', 'qtl_lead_vars':'rs62065450',
                    'coloc_dataset':'t1d_eQTL-inspire_coloc',
                    'data_source':'HIRN_T1D_QTL_GWAS', 'data_version':'v1.0',
                    'gwas_locus_name':'MAPT', 'qtl_locus_name':'PLEKHM1',
                    'pp_h4_abf':0.984}},
            ], 'rows':[]}
    facts = catalogue([step])
    row = next(item for item in facts if item['kind'] == 'coloc_record')
    assert row['mandatory']
    assert r'GWAS signal MAPT\_\_credibleSet1\_\_selected' in row['text']
    assert 'QTL lead variant rs62065450' in row['text']
    assert 'recorded H4 0.984' in row['text']
    assert 'tissue context unavailable' in row['text']
    assert 'record link urn:pankgraph:signal-coloc-with:sha256:' in row['text']
    caveat = next(item for item in facts if item['kind'] == 'coloc_locus_scope')
    assert caveat['mandatory']
    assert 'queried gene anchor is PLEKHM1' in caveat['text']
    assert 'record the GWAS locus as MAPT' in caveat['text']
