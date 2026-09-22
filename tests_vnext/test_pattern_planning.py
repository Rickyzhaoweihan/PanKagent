import asyncio
from copy import deepcopy
import pytest

from pankagent_vnext.pattern_planning import compile_signal_plan
from pankagent_vnext.schema_drafting import compile_schema_draft
from pankagent_vnext.preplanning_grounding import ground_question
from test_preplanning_grounding import FakeGraph


def grounded(question, extra=None):
    graph = FakeGraph()
    if extra:
        graph.rows['Gene'] += extra
    return asyncio.run(ground_question(graph, question))


PLEKHM1 = {'id': 'ENSG00000225190', 'name': 'PLEKHM1', 'labels': ['Gene'],
           'hgnc_symbol': 'PLEKHM1'}


@pytest.mark.parametrize('question', [
    'For ADCY3, does the T1D-associated GWAS signal rs13393590 colocalize with ADCY3 molecular QTL evidence?',
    'For\u00a0ADCY3, does the T1D-associated GWAS signal\u00a0rs13393590\u00a0colocalize with ADCY3 molecular QTL evidence?',
    'Show coloc evidence for ADCY3 and the T1D GWAS signal rs13393590.',
    'Show colocalisation evidence for ADCY3 and the T1D GWAS signal rs13393590.',
])
def test_three_signal_roles_compile_without_mandatory_join_or_lead_assumption(question):
    data = grounded(question)
    original = deepcopy(data)
    plan = compile_signal_plan(question, data)
    assert plan is not None
    assert plan['interpreted_question'] == question
    assert [s['relation_types'] for s in plan['steps']] == [['SIGNAL_COLOC_WITH'], ['PART_OF_GWAS_SIGNAL'], ['PART_OF_QTL_SIGNAL']]
    assert all(not s['depends_on'] and s['complete'] for s in plan['steps'])
    assert plan['planning_route']['claude_calls'] == 0
    assert [c['entity_type'] for c in plan['steps'][0]['constraints']] == ['Gene', 'disease']
    assert all(c['property'] == 'id' for s in plan['steps'] for c in s['constraints'])
    assert data == original


@pytest.mark.parametrize('question', [
    'Does the T1D GWAS signal near the PLEKHM1 gene colocalize with a QTL signal for PLEKHM1?',
    'Does the T1D GWAS signal near\u00a0PLEKHM1\u00a0colocalize with a QTL signal for PLEKHM1?',
    'Does the type 1 diabetes GWAS signal near PLEKHM1 colocalise with a molecular QTL signal for PLEKHM1?',
])
def test_no_variant_coloc_signal_role_frame_is_one_complete_primary_check(question):
    data = grounded(question, [PLEKHM1])
    original = deepcopy(data)
    plan = compile_signal_plan(question, data)
    assert plan is not None
    assert plan['interpreted_question'] == question
    assert len(plan['steps']) == 1
    step = plan['steps'][0]
    assert step['relation_types'] == ['SIGNAL_COLOC_WITH']
    assert step['depends_on'] == [] and step['complete']
    assert step['constraints'] == [
        {'property': 'id', 'operator': '=', 'value': 'ENSG00000225190', 'entity_type': 'Gene'},
        {'property': 'id', 'operator': '=', 'value': 'MONDO_0005147', 'entity_type': 'disease'},
    ]
    assert step['query_compilation'] == {
        'route': 'verified_local_template',
        'version': plan['planning_route']['version'],
        'digest': plan['planning_route']['digest'],
    }
    assert plan['planning_route']['kind'] == 'verified_signal_pattern'
    assert plan['planning_route']['claude_calls'] == 0
    assert data == original


@pytest.mark.parametrize('question', [
    'Show independent GWAS, QTL, and coloc evidence for PLEKHM1 and T1D.',
    'Show QTL signals near PLEKHM1.',
    'Does the T1D GWAS signal near PLEKHM1 colocalize with a QTL signal for CFTR?',
    'Does the T1D GWAS signal within 50 kb of PLEKHM1 colocalize with a QTL signal for PLEKHM1?',
])
def test_coloc_role_frame_does_not_swallow_independent_or_spatial_scope(question):
    assert compile_signal_plan(question, grounded(question, [PLEKHM1])) is None


@pytest.mark.parametrize('modifier', ['in spleen', 'not in pancreas', 'with PIP > 0.9',
    'with only positive slopes', 'and physical interactions', 'before treatment', 'causal mechanism',
    'with European ancestry', 'restricted to beta cells'])
def test_unknown_or_scientific_modifiers_fall_back_without_silent_drop(modifier):
    question = f'Does ADCY3 colocalize with the T1D GWAS signal rs13393590 {modifier}?'
    assert compile_signal_plan(question, grounded(question)) is None


def test_unknown_variant_and_revisions_never_reuse_prior_pattern():
    question = 'Does ADCY3 colocalize with the T1D GWAS signal rs9999999?'
    assert compile_signal_plan(question, grounded(question)) is None
    question = 'Show coloc evidence for ADCY3 and T1D.'
    data = grounded(question)
    assert compile_signal_plan(question, data)
    assert compile_signal_plan(question, data, [{'revision_context': {'instruction': 'Keep the old filters.'}}]) is None
    data['identity']['graph_release'] = 'unknown'
    assert compile_signal_plan(question, data) is None


def test_new_genes_share_qtl_pattern_and_exact_tissue():
    question = 'In pancreas, what molecular QTL evidence is recorded for each of NEWGENE1 and NEWGENE2? Keep the genes separate and report the recorded alleles, fine-mapping support and source.'
    data = grounded(question, [{'id': 'id-new1', 'name': 'NEWGENE1', 'labels': ['Gene']},
                               {'id': 'id-new2', 'name': 'NEWGENE2', 'labels': ['Gene']}])
    plan = compile_signal_plan(question, data)
    assert len(plan['steps']) == 2
    assert [s['constraints'][0]['value'] for s in plan['steps']] == ['id-new1', 'id-new2']
    assert all(s['constraints'][-1] == {'entity_type': None, 'property': 'tissue_id',
               'operator': '=', 'value': 'UBERON_0001264'} for s in plan['steps'])


def test_single_gene_independent_categories_come_from_schema_roles():
    question = 'Build a short CFTR profile showing the cell types where it is detected, where it is enriched, and its recorded marker-cell annotations. Keep these three evidence categories separate.'
    # "showing" and "three categories" are ordinary presentation qualifiers,
    # deliberately unsupported by this first bounded recognizer.
    assert compile_schema_draft(question, grounded(question)) is None
    question = 'Show CFTR detection, enrichment and marker annotations separately.'
    plan = compile_schema_draft(question, grounded(question))
    assert [s['relation_types'][0] for s in plan['steps']] == ['GENE_DETECTED_IN', 'GENE_ENRICHED_IN', 'MARKER_GENE_OF']
    assert all(s['constraints'][0]['value'] == 'ENSG00000001626' for s in plan['steps'])


def test_sample_stage_scope_uses_verified_vocabulary_without_clinical_reclassification():
    question = 'How many HPAP donors have recorded stage-3 T1D?'
    data = grounded(question)
    plan = compile_schema_draft(question, data)
    assert plan is not None
    fields = plan['steps'][0]['constraints']
    assert any(c['entity_type'] == 'donor' and c['property'] == 't1d_stage' for c in fields)
    assert not any(c['entity_type'] == 'disease' for c in fields)
    assert plan['planning_route']['claude_calls'] == 0


def test_sample_extra_filters_use_general_planner_instead_of_missing_bmi():
    question = 'Find HPAP stage 3 spleen samples with BMI above 30.'
    assert compile_schema_draft(question, grounded(question)) is None


@pytest.mark.parametrize('question', [
    'Show QTL evidence for ADCY3 in pancreas and all QTL evidence for CFTR.',
    'Show QTL evidence for ADCY3 and rs13393590 and all QTL evidence for CFTR.',
    'Show CFTR detection and enrichment in T1D.',
    'Show cell types where CFTR is detected and enriched.',
])
def test_clause_specific_constraints_and_intersections_never_broadcast(question):
    data = grounded(question)
    assert compile_signal_plan(question, data) is None
    assert compile_schema_draft(question, data) is None


def test_gateway_pattern_admission_bypasses_provider_and_revalidates_cache():
    from test_planning_compiler_gateway import gateway_for
    question = 'For ADCY3, does the T1D-associated GWAS signal rs13393590 colocalize with ADCY3 molecular QTL evidence?'
    data = grounded(question)
    async def check():
        gateway, calls = gateway_for(lambda _: {})
        result = await gateway.plan(question, [], grounding=data)
        assert not calls and result['planning_route']['claude_calls'] == 0
        assert len(result['steps']) == 3
        assert all(s['complete'] for s in result['steps'])
        assert await gateway.plan(question, [], grounding=data) == result
        assert not calls
    asyncio.run(check())


def test_no_variant_coloc_role_frame_bypasses_provider_and_reuses_verified_cache():
    from test_planning_compiler_gateway import gateway_for
    question = 'Does the T1D GWAS signal near the PLEKHM1 gene colocalize with a QTL signal for PLEKHM1?'
    data = grounded(question, [PLEKHM1])
    async def check():
        gateway, calls = gateway_for(lambda _: {})
        result = await gateway.plan(question, [], grounding=data)
        assert not calls and result['planning_route']['claude_calls'] == 0
        assert len(result['steps']) == 1
        step = result['steps'][0]
        assert step['relation_types'] == ['SIGNAL_COLOC_WITH']
        assert {c['entity_type']: c['value'] for c in step['constraints']} == {
            'Gene': 'ENSG00000225190', 'disease': 'MONDO_0005147'}
        assert await gateway.plan(question, [], grounding=data) == result
        assert not calls
    asyncio.run(check())


def test_no_variant_coloc_executes_local_template_with_grounded_policy_disabled():
    from test_graph import FakeAdapter
    from test_planning_compiler_gateway import gateway_for
    from pankagent_vnext.release_schema import REGISTRY

    question = 'Does the T1D GWAS signal near\u00a0PLEKHM1\u00a0colocalize with a QTL signal for PLEKHM1?'
    data = grounded(question, [PLEKHM1])

    async def check():
        gateway, planning_calls = gateway_for(lambda _: {})
        plan = await gateway.plan(question, [], grounding=data)
        assert not planning_calls

        graph = FakeAdapter([])
        graph.settings.graph_version = REGISTRY['release']
        graph.settings.grounded_query_policy = False
        graph.release_relations = {'SIGNAL_COLOC_WITH'}
        graph.answer = {
            'nodes': [
                {'id': 'ENSG00000225190', 'labels': ['Gene'],
                 'properties': {'id': 'ENSG00000225190', 'name': 'PLEKHM1'}},
                {'id': 'MONDO_0005147', 'labels': ['disease'],
                 'properties': {'id': 'MONDO_0005147', 'name': 'type 1 diabetes'}},
            ],
            'edges': [
                {'start_id': 'ENSG00000225190', 'end_id': 'MONDO_0005147',
                 'type': 'SIGNAL_COLOC_WITH', 'properties': {
                     'gwas_signal_id': 'MAPT__credibleSet1__selected',
                     'qtl_signal_id': 'PLEKHM1__credibleSet1',
                     'gwas_lead_vars': 'rs35327136', 'qtl_lead_vars': 'rs62065450',
                     'gwas_locus_name': 'MAPT', 'qtl_locus_name': 'PLEKHM1',
                     'coloc_dataset': 't1d_eQTL-inspire_coloc',
                     'data_source': 'HIRN_T1D_QTL_GWAS', 'data_version': 'v1.0',
                     'pp_h4_abf': 0.984}},
                {'start_id': 'ENSG00000225190', 'end_id': 'MONDO_0005147',
                 'type': 'SIGNAL_COLOC_WITH', 'properties': {
                     'gwas_signal_id': 'MAPT__credibleSet1__selected',
                     'qtl_signal_id': 'PLEKHM1__exon__credibleSet3',
                     'gwas_lead_vars': 'rs35327136', 'qtl_lead_vars': 'rs62064652',
                     'gwas_locus_name': 'MAPT', 'qtl_locus_name': 'PLEKHM1',
                     'coloc_dataset': 't1d_exonQTL-inspire_coloc',
                     'data_source': 'HIRN_T1D_QTL_GWAS', 'data_version': 'v1.0',
                     'pp_h4_abf': 0.991}},
            ],
            'rows': [], 'status': 'complete', 'truncated': False,
            'retrieval_execution': {'completed': True, 'cursor_exhausted': True},
        }

        prepared = await graph.prepare_plan(plan, lambda *_: None)

        async def emit(*_):
            pass

        first = await graph.execute(prepared['steps'][0], {}, emit)
        second = await graph.execute(prepared['steps'][0], {}, emit)
        assert first['status'] == second['status'] == 'complete'
        assert first['query_route'] == 'template'
        assert second['query_route'] == 'cache'
        assert graph.generated == []
        assert first['colocalization_signal_counts']['record_count'] == 2
        assert first['colocalization_signal_counts']['distinct_recorded_qtl_signal_count'] == 2
        assert first['colocalization_signal_counts']['distinct_recorded_gwas_signal_count'] == 1
        assert len(first['colocalization_record_links']) == 2
        assert 'rs112550936' not in repr(first)

    asyncio.run(check())
