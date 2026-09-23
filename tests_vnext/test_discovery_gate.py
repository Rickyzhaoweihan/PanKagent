from pankagent_vnext.semantic_registry import _unresolved_tissue_role, resolve
from pankagent_vnext.app import normalize_plan
from pankagent_vnext.evidence_status import PARTIAL_INDEPENDENT_POLICY
from test_sample_scope_recovery import VOCAB


def test_tissue_discovery_does_not_create_a_filter():
    for q in ['What tissue/cell type are these samples from?', 'Which tissues are available?']:
        assert not _unresolved_tissue_role(q, VOCAB, [])
    assert _unresolved_tissue_role('samples from MysteryOrgan; what tissue are they from?', VOCAB, [])


def test_donor_parent_defers_assay_but_keeps_cohort():
    raw='How many scRNAseq samples available for T1D stage 1 HPAP donors?'
    step={'id':'donors','question':'Find HPAP stage 1 donors', 'relation_types':['HAS_DONOR'],
          'constraints':[], 'deferred_sample_scope':True,
          'semantic_request':{'source':'user_request','question':raw}}
    out=resolve(step,VOCAB,'PanKgraph_08_04')
    assert all(c['entity_type']=='donor' for c in out['constraints'])
    assert {'t1d_stage','data_source'} <= {c['property'] for c in out['constraints']}
    assert out['sample_requirements']=={}
    assert all(b['canonical_binding']==out['constraints'][b['constraint_index']] for b in out['request_filter_bindings'])


def test_new_plans_default_to_partial_evidence_policy():
    plan=normalize_plan({'steps':[{'id':'s','question':'Find samples','relation_types':['HAS_SAMPLE']}]})
    assert plan['retrieval_policy']==PARTIAL_INDEPENDENT_POLICY
