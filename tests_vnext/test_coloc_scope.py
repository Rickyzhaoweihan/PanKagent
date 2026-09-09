import copy
import pytest

from pankagent_vnext.coloc_scope import DIGEST, RELEASE, normalize_plan, summarize_linkage

GENE = 'ENSG00000138031'
DISEASE = 'MONDO_0005147'
VARIANT = 'rs13393590'


def identity(kind, value, prop='id'):
    return {'entity_type': kind, 'property': prop, 'operator': '=', 'value': value}


def plan():
    return {'steps': [{'id': 's1', 'question': 'Does the ADCY3 T1D GWAS signal rs13393590 colocalize with molecular QTL?',
        'relation_types': ['SIGNAL_COLOC_WITH', 'PART_OF_GWAS_SIGNAL', 'PART_OF_QTL_SIGNAL'],
        'constraints': [identity('Gene', GENE), identity('variants', VARIANT), identity('disease', DISEASE)],
        'complete': True, 'evidence_combination': 'cooccurrence', 'depends_on': []}],
        'display_groups': [{'id': 'g1', 'step_ids': ['s1']}], 'literature': True}


def edge(kind, start, end, props, identifier='e1'):
    return {'id': identifier, 'type': kind, 'start_id': start, 'end_id': end, 'properties': props}


def evidence():
    common = {'gwas_signal_id': 'ADCY3__credibleSet1__selected', 'gwas_lead_vars': 'rs55893453'}
    return {
        's1': {'status': 'complete', 'truncated': False, 'graph_version': RELEASE, 'edges': [
            edge('SIGNAL_COLOC_WITH', GENE, DISEASE, {**common, 'qtl_signal_id': 'ADCY3_exon_signal',
                'qtl_lead_vars': VARIANT, 'coloc_dataset': 't1d_exonQTL-inspire_coloc', 'pp_h4_abf': .973388588508594}, 'coloc-exon'),
            edge('SIGNAL_COLOC_WITH', GENE, DISEASE, {**common, 'qtl_signal_id': 'ADCY3_expression_signal',
                'qtl_lead_vars': 'rs10176214', 'coloc_dataset': 't1d_eQTL-inspire_coloc', 'pp_h4_abf': .957524151286953}, 'coloc-eqtl')]},
        's1_gwas': {'status': 'complete', 'truncated': False, 'graph_version': RELEASE, 'edges': [edge('PART_OF_GWAS_SIGNAL', VARIANT, DISEASE,
            {'credible_set_id': 'ADCY3__credibleSet1', 'lead_status': 'nonlead'}, 'gwas')]},
        's1_qtl': {'status': 'complete', 'truncated': False, 'graph_version': RELEASE, 'edges': [edge('PART_OF_QTL_SIGNAL', VARIANT, GENE,
            {'credible_set': 'ADCY3_exon_signal', 'data_source': 'exon; INSPIRE', 'tissue_id': 'UBERON_0000006'}, 'qtl')]}}


def summary(source=None, prior=None):
    return summarize_linkage(normalize_plan(source or plan(), RELEASE), prior or evidence())['groups'][0]


def test_split_preserves_original_and_all_requested_categories():
    source = plan(); original = copy.deepcopy(source)
    actual = normalize_plan(source, RELEASE)
    assert source == original
    assert [s['id'] for s in actual['steps']] == ['s1', 's1_gwas', 's1_qtl']
    assert [s['relation_types'] for s in actual['steps']] == [['SIGNAL_COLOC_WITH'], ['PART_OF_GWAS_SIGNAL'], ['PART_OF_QTL_SIGNAL']]
    assert [[c['entity_type'] for c in s['constraints']] for s in actual['steps']] == [['Gene', 'disease'], ['variants', 'disease'], ['variants', 'Gene']]
    assert actual['display_groups'][0]['step_ids'] == ['s1', 's1_gwas', 's1_qtl']
    assert actual['coloc_scope_normalization']['original_steps'] == original['steps']
    assert normalize_plan(actual, RELEASE) == actual
    assert all(s['complete'] and s['purpose'] == 'primary' and not s['depends_on'] for s in actual['steps'])


def test_descendant_dependencies_and_existing_groups_expand_without_loss():
    source = plan(); source['steps'].append({'id': 's2', 'question': 'Inspect the sources', 'depends_on': ['s1'], 'relation_types': [], 'constraints': []})
    source['display_groups'][0]['step_ids'].append('s2')
    actual = normalize_plan(source, RELEASE, max_steps=4)
    assert actual['steps'][-1]['depends_on'] == ['s1', 's1_gwas', 's1_qtl']
    assert actual['display_groups'][0]['step_ids'] == ['s1', 's1_gwas', 's1_qtl', 's2']
    assert actual['steps'][-1]['question'] == source['steps'][-1]['question']
    assert normalize_plan(source, RELEASE, max_steps=12) == actual


def test_cap_and_collision_leave_original_steps_with_explicit_recovery():
    source = plan(); source['steps'].append({'id': 'extra', 'question': 'Other check', 'constraints': [], 'relation_types': []})
    actual = normalize_plan(source, RELEASE)
    assert actual['steps'] == source['steps']
    assert actual['coloc_scope_issue'] == 'step_cap'
    assert actual['review_ready'] is False and actual['recovery']['suggestions']
    source['steps'][-1]['id'] = 's1_qtl'
    actual = normalize_plan(source, RELEASE, max_steps=4)
    assert actual['coloc_scope_issue'] == 'step_id_collision'
    assert actual['steps'] == source['steps']


def test_unsafe_extra_filters_entities_dependencies_and_other_releases_not_normalized():
    sources = []
    source = plan(); source['steps'][0]['constraints'].append(identity('Gene', 'OTHER')); sources.append(source)
    source = plan(); source['steps'][0]['constraints'].append({'property': 'pp_h4_abf', 'operator': '>', 'value': .9}); sources.append(source)
    source = plan(); source['steps'][0]['depends_on'] = ['prior']; sources.append(source)
    source = plan(); source['steps'][0]['constraints'][0]['operator'] = 'IN'; sources.append(source)
    source = plan(); source['steps'][0]['constraints'][0]['owner_kind'] = 'relationship'; sources.append(source)
    source = plan(); source['steps'][0]['complete'] = False; sources.append(source)
    for source in sources:
        assert normalize_plan(source, RELEASE) == source
    assert normalize_plan(plan(), 'other_release') == plan()


@pytest.mark.parametrize('restriction', [
    'Only use pancreatic tissue.', 'Require PP.H4 above 0.9.', 'Use only GTEx evidence.',
    'Use islet eQTL evidence.', 'Use INSPIRE.', 'Require PIP >= 0.1.', 'Exclude exonQTL records.',
    'Show the top 5 records.', 'Sort by the highest shared-signal probability.',
    'Use exactly this variant as the GWAS lead.', 'Find records from an unnamed consortium.',
    'Check within lung.', 'Include significant records.',
])
def test_unrepresented_text_restrictions_block_without_widening_or_dropping_original(restriction):
    source = plan(); source['steps'][0]['question'] += ' ' + restriction
    original = copy.deepcopy(source)
    actual = normalize_plan(source, RELEASE)
    assert source == original
    assert actual['steps'] == original['steps'] and actual['display_groups'] == original['display_groups']
    assert actual['coloc_scope_issue'] == 'unrepresented_restriction'
    assert actual['review_ready'] is False and 'coloc_scope_normalization' not in actual
    assert actual['recovery']['suggestions'][0]['label'] == 'Keep all my restrictions'
    assert actual['recovery']['evidence']['restrictions'][0]['step_id'] == 's1'


def test_frozen_adcy3_question_and_graph_relative_context_are_not_blocked():
    for suffix in ('', ' in PanKgraph', ' in the current graph'):
        source = plan()
        source['steps'][0]['question'] = ('For ADCY3, does the T1D-associated GWAS signal rs13393590 '
                                        'colocalize with ADCY3 molecular QTL evidence' + suffix + '?')
        actual = normalize_plan(source, RELEASE)
        assert 'coloc_scope_issue' not in actual and len(actual['steps']) == 3


@pytest.mark.parametrize('question', [
    'Does rs13393590 colocalize with ADCY3 in T1D?',
    'Does rs13393590 colocalize with ADCY3 in type 1 diabetes?',
    'Does rs13393590 colocalize for ADCY3?',
    'Does rs13393590 colocalize with ADCY3 in PanKgraph?',
])
def test_pre_resolution_identity_aliases_require_the_exact_verified_binding(question):
    source = plan(); source['steps'][0]['question'] = question
    assert 'resolved_entities' not in source['steps'][0]
    actual = normalize_plan(source, RELEASE)
    assert 'coloc_scope_issue' not in actual and len(actual['steps']) == 3
    assert actual['coloc_scope_normalization']['original_steps'] == source['steps']


@pytest.mark.parametrize('suffix', ['in T1D', 'in type 1 diabetes', 'in T1D lung',
                                   'in T1D islets', 'in T1D stage 3', 'in T1D from GTEx'])
def test_identity_alias_does_not_authorize_wrong_disease_or_additional_context(suffix):
    source = plan(); source['steps'][0]['question'] = 'Does rs13393590 colocalize with ADCY3 ' + suffix + '?'
    if suffix in ('in T1D', 'in type 1 diabetes'):
        source['steps'][0]['constraints'][2]['value'] = 'MONDO_0005148'
    actual = normalize_plan(source, RELEASE)
    assert actual['coloc_scope_issue'] == 'unrepresented_restriction'
    assert actual['steps'] == source['steps']


def test_original_ranking_metadata_is_not_lost_even_when_absent_from_text():
    source = plan(); source['steps'][0]['ranking_contract'] = {'top_n': 5}
    actual = normalize_plan(source, RELEASE)
    assert actual['coloc_scope_issue'] == 'unrepresented_restriction'
    assert actual['steps'] == source['steps']


def test_adcy3_two_primary_records_link_correct_roles_and_keep_four_relationships():
    prior = evidence(); original = copy.deepcopy(prior)
    actual = summary(prior=prior)
    assert prior == original
    assert actual['primary_record_count'] == actual['linked_record_count'] == 2
    exon, expression = actual['records']
    assert exon['match_kinds'] == ['recorded_qtl_lead', 'verified_gwas_credible_set_member', 'verified_qtl_credible_set_member']
    assert expression['match_kinds'] == ['verified_gwas_credible_set_member']
    assert 'recorded_gwas_lead' not in exon['match_kinds']
    assert {ref['step_id'] for ref in exon['supporting_references']} == {'s1_gwas', 's1_qtl'}
    assert sum(len(s['edges']) for s in prior.values()) == 4


def test_missing_or_failed_qtl_context_never_erases_primary_coloc():
    for state in ('empty', 'failed', 'blocked', 'partial'):
        prior = evidence(); prior['s1_qtl'] = {'status': state, 'edges': []}
        actual = summary(prior=prior)
        assert actual['primary_record_count'] == 2
        assert actual['linked_record_count'] == 2  # Exact GWAS membership still supports both.
        assert all('verified_qtl_credible_set_member' not in r['match_kinds'] for r in actual['records'])


def test_common_gene_or_disease_or_substring_never_links_signals():
    prior = evidence()
    for e in prior['s1']['edges']:
        e['properties'].update(qtl_lead_vars='rs999', gwas_lead_vars='rs998', gwas_signal_id='ADCY3__credibleSet10__selected')
    prior['s1_qtl']['edges'][0]['properties']['credible_set'] += '_other'
    actual = summary(prior=prior)
    assert actual['primary_record_count'] == 2 and actual['linked_record_count'] == 0
    assert all(not r['match_kinds'] for r in actual['records'])


def test_wrong_qtl_source_tissue_or_endpoint_prevents_membership_link():
    for field, value in [('data_source', 'GTEx; SusieR'), ('tissue_id', 'UBERON_0001264'), ('credible_set', 'other')]:
        prior = evidence(); prior['s1_qtl']['edges'][0]['properties'][field] = value
        assert 'verified_qtl_credible_set_member' not in summary(prior=prior)['records'][0]['match_kinds']
    for role, endpoint in [('s1_qtl', 'end_id'), ('s1_gwas', 'end_id'), ('s1_gwas', 'start_id')]:
        prior = evidence(); prior[role]['edges'][0][endpoint] = 'wrong'
        actual = summary(prior=prior)['records'][0]
        kind = 'verified_qtl_credible_set_member' if role == 's1_qtl' else 'verified_gwas_credible_set_member'
        assert kind not in actual['match_kinds']


def test_truncated_or_failed_results_cannot_supply_linkage_proof():
    prior = evidence(); prior['s1_gwas']['truncated'] = True; prior['s1_qtl']['status'] = 'failed'
    actual = summary(prior=prior)
    assert actual['records'][0]['match_kinds'] == ['recorded_qtl_lead']
    assert actual['records'][1]['match_kinds'] == []
    prior['s1']['truncated'] = True
    actual = summary(prior=prior)
    assert actual['status'] == 'primary_unverified' and actual['records'] == []


def test_unknown_truncation_and_wrong_release_cannot_supply_linkage_proof():
    for role in ('s1_gwas', 's1_qtl'):
        for mutation in ('unknown', 'other_release', 'missing_release'):
            prior = evidence()
            if mutation == 'unknown':
                prior[role].pop('truncated')
            elif mutation == 'other_release':
                prior[role]['graph_version'] = 'other_release'
            else:
                prior[role].pop('graph_version')
            kind = 'verified_gwas_credible_set_member' if role == 's1_gwas' else 'verified_qtl_credible_set_member'
            assert kind not in summary(prior=prior)['records'][0]['match_kinds']
    prior = evidence(); prior['s1'].pop('truncated')
    actual = summary(prior=prior)
    assert actual['status'] == 'primary_unverified' and actual['primary_record_count'] is None


def test_aggregate_linkage_never_claims_checked_for_unverified_primary_or_context():
    prepared = normalize_plan(plan(), RELEASE)
    assert summarize_linkage(prepared, evidence())['status'] == 'checked'
    for role in ('s1', 's1_gwas', 's1_qtl'):
        prior = evidence(); prior[role].pop('truncated')
        assert summarize_linkage(prepared, prior)['status'] == 'partial'
        prior = evidence(); prior[role]['status'] = 'failed'
        assert summarize_linkage(prepared, prior)['status'] == 'partial'
    prepared['steps'][0]['constraints'][0] = identity('Gene', 'ADCY3', 'name')
    assert summarize_linkage(prepared, evidence())['status'] == 'unknown'


def test_scalar_counts_do_not_claim_zero_enumerated_coloc_or_complete_linkage():
    prepared = normalize_plan(plan(), RELEASE)
    for count in (0, 2):
        scalar = {'status': 'complete', 'truncated': False, 'graph_version': RELEASE,
                  'rows': [{'count': count}], 'nodes': [], 'edges': []}
        for role in ('s1', 's1_gwas', 's1_qtl'):
            prior = evidence(); prior[role] = copy.deepcopy(scalar)
            actual = summarize_linkage(prepared, prior)
            assert actual['status'] == 'partial'
            group = actual['groups'][0]
            if role == 's1':
                assert group['primary_record_count'] is None and group['linked_record_count'] is None
                assert group['status'] == 'primary_unverified' and group['records'] == []
            else:
                assert group['primary_record_count'] == 2
                kind = 'verified_gwas_credible_set_member' if role == 's1_gwas' else 'verified_qtl_credible_set_member'
                assert all(kind not in record['match_kinds'] for record in group['records'])
        prior = {role: copy.deepcopy(scalar) for role in ('s1', 's1_gwas', 's1_qtl')}
        actual = summarize_linkage(prepared, prior)
        assert actual['status'] == 'partial' and actual['groups'][0]['primary_record_count'] is None


def test_genuine_empty_graphs_still_report_enumerated_zero():
    empty = {'status': 'empty', 'truncated': False, 'graph_version': RELEASE, 'nodes': [], 'edges': [], 'rows': []}
    actual = summarize_linkage(normalize_plan(plan(), RELEASE), {key: copy.deepcopy(empty) for key in ('s1', 's1_gwas', 's1_qtl')})
    assert actual['status'] == 'checked'
    assert actual['groups'][0]['primary_record_count'] == actual['groups'][0]['linked_record_count'] == 0


def test_explicit_coverage_without_record_enumeration_blocks_linkage_even_with_edges():
    prior = evidence(); prior['s1']['evidence_coverage'] = {'record_membership_enumerated': False}
    actual = summarize_linkage(normalize_plan(plan(), RELEASE), prior)
    assert actual['status'] == 'partial' and actual['groups'][0]['primary_record_count'] is None
    prior['s1'] = {'status': 'complete', 'truncated': False, 'graph_version': RELEASE, 'rows': [{'count': 2}],
                   'edges': [], 'evidence_coverage': {'record_membership_enumerated': True}}
    actual = summarize_linkage(normalize_plan(plan(), RELEASE), prior)
    assert actual['status'] == 'partial' and actual['groups'][0]['primary_record_count'] is None


def test_digest_includes_implementation_bytes_and_reviewed_release_conventions():
    import hashlib
    import json
    from pathlib import Path
    import pankagent_vnext.coloc_scope as module
    expected = hashlib.sha256(Path(module.__file__).read_bytes() + b'\n' + json.dumps({
        'version': module.VERSION, 'release': RELEASE, 'qtl_context': module.QTL_CONTEXT,
        'gwas_selected_suffix': '__selected'}, sort_keys=True).encode()).hexdigest()
    assert DIGEST == expected


def test_malformed_lead_lists_and_signal_suffixes_do_not_match():
    for value in ('variant rs13393590 here', 'rs13393590_else', ['rs13393590', 12], 'rs13393590; nonsense'):
        prior = evidence(); prior['s1']['edges'][0]['properties']['qtl_lead_vars'] = value
        assert 'recorded_qtl_lead' not in summary(prior=prior)['records'][0]['match_kinds']
    prior = evidence(); prior['s1']['edges'][0]['properties']['gwas_signal_id'] = 'prefix ADCY3__credibleSet1__selected'
    assert 'verified_gwas_credible_set_member' not in summary(prior=prior)['records'][0]['match_kinds']


def test_canonical_names_require_actual_resolution_and_stale_registry_is_rejected():
    source = plan(); source['steps'][0]['constraints'][0] = identity('Gene', 'ADCY3', 'name')
    normalized = normalize_plan(source, RELEASE)
    assert summarize_linkage(normalized, evidence())['groups'][0]['status'] == 'unresolved_identity'
    for step in normalized['steps']:
        for index, constraint in enumerate(step['constraints']):
            if constraint['entity_type'] == 'Gene':
                step['resolved_entities'] = [{'constraint_index': index, 'state': 'resolved', 'entity_type': 'Gene', 'id': GENE}]
    assert summarize_linkage(normalized, evidence())['groups'][0]['linked_record_count'] == 2
    normalized['coloc_scope_normalization']['digest'] = 'old'
    assert summarize_linkage(normalized, evidence())['status'] == 'not_applicable_or_stale'


def test_summary_bound_is_separate_from_retrieved_evidence_and_complete_counts():
    prior = evidence(); base = prior['s1']['edges'][0]
    prior['s1']['edges'] = [{**copy.deepcopy(base), 'id': f'coloc-{i}'} for i in range(30)]
    actual = summary(prior=prior)
    assert actual['primary_record_count'] == actual['linked_record_count'] == 30
    assert len(actual['records']) == 25 and actual['summary_omitted_record_count'] == 5
    assert len(prior['s1']['edges']) == 30
