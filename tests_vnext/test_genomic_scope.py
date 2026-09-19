import asyncio
from copy import deepcopy

import pytest

from pankagent_vnext.genomic_scope import (
    COORDINATE_METADATA_QUERY, compile_genomic_scope, coordinate_metadata,
    genomic_scope, has_verified_region_scope, is_verified_region_constraint,
    region_scope_issue,
)
from pankagent_vnext.planning_scope import scope_issue
from pankagent_vnext.preplanning_grounding import ground_question, grounding_guidance
from tests_vnext.test_preplanning_grounding import FakeGraph, make_index, candidate_ids


RELEASE = 'PanKgraph_08_04'
QUESTION = 'Which genes at the chr17 MAPT/PLEKHM1 locus (43–46 Mb) are differentially expressed in alpha cells in T1D?'
IDENTITY = {'graph_release': RELEASE, 'schema_digest': 'verified-schema'}
COUNTS = {'total': 10, 'coordinate_count': 10, 'chromosomes': ['chr1', 'chr17'],
          'assembly_count': 10, 'assembly_values': ['GRCh38.p14'],
          'genome_assembly_count': 10, 'genome_assembly_values': ['GRCh38.p14']}


def metadata():
    return coordinate_metadata([deepcopy(COUNTS)], IDENTITY)


def grounding():
    return {'status': 'ready', 'identity': deepcopy(IDENTITY),
            'genomic_coordinate_metadata': metadata(), 'mentions': []}


def proposal(*constraints, kinds=None):
    return {'clarification': None, 'steps': [{'id': 's1', 'question': QUESTION,
        'relation_types': ['T1D_DEG_IN'] if kinds is None else kinds,
        'constraints': list(constraints), 'complete': True, 'depends_on': []}]}


def prepared(question=QUESTION, plan=None, data=None):
    result, issue = compile_genomic_scope(question, data or grounding(), plan or proposal())
    assert issue is None
    step = result['steps'][0]
    step['graph_version'] = RELEASE
    return result


@pytest.mark.parametrize('interval,start,end', [
    ('43–46 Mb', 43000000, 46000000), ('43-46Mb', 43000000, 46000000),
    ('43.5 to 46.1 Mb', 43500000, 46100000), ('43000 kb–46 Mb', 43000000, 46000000),
    ('43,000,000-46,000,000', 43000000, 46000000),
])
def test_interval_units_and_unicode_ranges_preserve_raw_scope(interval, start, end):
    question = 'All genes in chr17:' + interval + ' on GRCh38.p14'
    scope = genomic_scope(question)
    assert scope['state'] == 'parsed'
    assert (scope['chromosome'], scope['start'], scope['end']) == ('17', start, end)
    assert scope['requested_assembly'] == 'GRCh38.p14'
    assert scope['requested_interval'] == interval
    assert scope['raw_question'] == question


def test_named_locus_is_not_two_genes_and_needs_bounds():
    question = 'Which genes at the chr17 MAPT/PLEKHM1 locus are expressed?'
    scope = genomic_scope(question)
    assert scope['state'] == 'missing_bounds'
    assert scope['locus_labels'][0]['surface'] == 'MAPT/PLEKHM1'
    result, issue = compile_genomic_scope(question, grounding(), proposal())
    assert issue.startswith('unresolved_genomic_scope:missing_bounds')
    assert result == proposal()


@pytest.mark.parametrize('question,state', [
    ('Are there other genes differentially expressed in beta cells at the MAPT/PLEKHM1 locus in T1D?', 'missing_chromosome_and_bounds'),
    ('Which genes at the MAPT/PLEKHM1 locus (43–46 Mb) are expressed in GRCh38.p14?', 'missing_chromosome'),
])
def test_named_locus_without_chromosome_cannot_be_replaced_by_label_genes(question, state):
    scope = genomic_scope(question)
    assert scope['state'] == state
    assert scope['chromosome'] is None and scope['collection_scope'] is True
    assert scope['raw_question'] == question
    assert scope['locus_labels'][0]['surface'] == 'MAPT/PLEKHM1'
    anchors = {'entity_type': 'Gene', 'property': 'name', 'operator': 'IN', 'value': ['MAPT', 'PLEKHM1']}
    source = proposal(anchors)
    result, issue = compile_genomic_scope(question, grounding(), source)
    assert issue.startswith('unresolved_genomic_scope:' + state)
    assert 'chromosome and interval bounds' in issue and 'reference-assembly context' in issue
    assert result == source
    assert scope_issue(question, grounding(), source) == issue
    mentions = make_index(alias_graph()).match(question)
    labels = [m for m in mentions if m.get('context_role', {}).get('kind') == 'genomic_locus_label']
    assert candidate_ids(labels) == {'mapt', 'plekhm1'}
    assert all(m['identity_complete'] is False for m in labels)


def test_named_gene_pair_without_locus_keeps_existing_lookup_behavior():
    question = 'Which of the genes MAPT and PLEKHM1 are expressed in beta cells?'
    assert genomic_scope(question) is None
    assert {'mapt', 'plekhm1'} <= candidate_ids(make_index(alias_graph()).match(question))


@pytest.mark.parametrize('question,state', [
    ('genes in chr17 46–43 Mb', 'invalid_coordinates'),
    ('genes in chr17 43–46 Mb and chr1 2–3 Mb', 'ambiguous_region'),
    ('genes in chr17 43–46 Mb using GRCh37 or GRCh38', 'ambiguous_region'),
    ('genes outside chr17 43–46 Mb', 'unsupported_region_complement'),
])
def test_ambiguous_or_complementary_regions_are_not_silently_compiled(question, state):
    assert genomic_scope(question)['state'] == state
    assert compile_genomic_scope(question, grounding(), proposal())[1]


def test_nonregional_questions_do_not_gain_constraints():
    assert genomic_scope('Show gene INS QTL evidence') is None
    plan = proposal()
    result, issue = compile_genomic_scope('Show gene INS QTL evidence', grounding(), plan)
    assert issue is None and result == plan


def test_missing_assembly_uses_complete_verified_release_value_with_provenance():
    result = prepared()
    step = result['steps'][0]
    assert step['genomic_scope_contract']['assembly_source'] == 'verified_release_default'
    assert step['genomic_scope_contract']['metadata']['default_assembly']['value'] == 'GRCh38.p14'
    assert {(c['property'], c['operator'], c['value']) for c in step['constraints']} == {
        ('chr', '=', 'chr17'), ('genome_assembly', '=', 'GRCh38.p14'),
        ('start_loc', '<=', 46000000), ('end_loc', '>=', 43000000)}
    assert has_verified_region_scope(step)
    assert all(is_verified_region_constraint(c, step) for c in step['constraints'])
    assert scope_issue(QUESTION, grounding(), result) is None


def test_explicit_build_is_preserved_and_mismatched_build_is_not_lifted():
    same = prepared(QUESTION + ' Use GRCh38.p14.')
    assert same['steps'][0]['genomic_scope_contract']['assembly_source'] == 'user_and_verified_release'
    assert 'assembly_mismatch' in compile_genomic_scope(QUESTION + ' Use GRCh37.', grounding(), proposal())[1]
    assert 'assembly_mismatch' in compile_genomic_scope(QUESTION + ' Use hg19.', grounding(), proposal())[1]


@pytest.mark.parametrize('field,value', [
    ('coordinate_count', 9), ('total', 0), ('chromosomes', [17]),
])
def test_partial_or_unverified_numeric_coordinate_storage_cannot_authorize_region(field, value):
    row = {**COUNTS, field: value}
    data = grounding()
    data['genomic_coordinate_metadata'] = coordinate_metadata([row], IDENTITY)
    assert data['genomic_coordinate_metadata']['state'] == 'unavailable'
    assert 'metadata_unverified' in compile_genomic_scope(QUESTION, data, proposal())[1]


@pytest.mark.parametrize('changes', [
    {'assembly_values': ['GRCh37', 'GRCh38.p14']},
    {'assembly_values': [], 'assembly_count': 0, 'genome_assembly_count': 9},
])
def test_mixed_or_incompletely_covered_assembly_cannot_be_default(changes):
    data = grounding()
    data['genomic_coordinate_metadata'] = coordinate_metadata([{**COUNTS, **changes}], IDENTITY)
    assert 'default_assembly' not in data['genomic_coordinate_metadata']
    assert 'assembly_unknown_or_mixed' in compile_genomic_scope(QUESTION, data, proposal())[1]


def test_metadata_from_another_identity_is_not_usable():
    data = grounding()
    data['genomic_coordinate_metadata']['identity']['schema_digest'] = 'other'
    assert 'metadata_unverified' in compile_genomic_scope(QUESTION, data, proposal())[1]


def test_locus_anchor_only_is_replaced_by_complete_region_with_original_audit():
    anchors = {'entity_type': 'Gene', 'property': 'name', 'operator': 'IN', 'value': ['MAPT', 'PLEKHM1']}
    source = proposal(anchors)
    original = deepcopy(source)
    assert 'genomic_region' in region_scope_issue(QUESTION, grounding(), source)
    result = prepared(plan=source)
    assert source == original
    step = result['steps'][0]
    assert not any(c['property'] in {'id', 'name'} for c in step['constraints'])
    assert step['requested_scope_compilation'][0]['original_predicates'] == [anchors]
    assert has_verified_region_scope(step)


def test_unrelated_gene_anchor_cannot_silently_narrow_region_collection():
    c = {'entity_type': 'Gene', 'property': 'name', 'operator': '=', 'value': 'INS'}
    result = prepared(plan=proposal(c))
    assert 'gene_identity_filter' in region_scope_issue(QUESTION, grounding(), result)
    assert not has_verified_region_scope(result['steps'][0])


@pytest.mark.parametrize('index', range(4))
def test_all_four_constraints_required_for_execution(index):
    result = prepared()
    del result['steps'][0]['constraints'][index]
    assert not has_verified_region_scope(result['steps'][0])
    assert 'genomic_region' in region_scope_issue(QUESTION, grounding(), result)


@pytest.mark.parametrize('change', [
    {'entity_type': 'variants'}, {'owner_kind': 'relationship'},
    {'relationship_type': 'T1D_DEG_IN'}, {'value': 'chr1'},
])
def test_wrong_owner_or_value_cannot_cover_region(change):
    result = prepared()
    result['steps'][0]['constraints'][0].update(change)
    assert not has_verified_region_scope(result['steps'][0])
    assert region_scope_issue(QUESTION, grounding(), result)


def test_contract_mutation_does_not_authorize_an_arbitrary_predicate():
    result = prepared()
    step = result['steps'][0]
    step['genomic_scope_contract']['predicates'].append({'entity_type': 'Gene', 'property': 'name', 'operator': '=', 'value': 'INS'})
    assert not has_verified_region_scope(step)
    assert not is_verified_region_constraint(step['constraints'][0], step)


@pytest.mark.parametrize('phrase,expected', [
    ('fully within', {('start_loc', '>='), ('end_loc', '<=')}),
    ('overlapping', {('start_loc', '<='), ('end_loc', '>=')}),
])
def test_overlap_is_not_silently_replaced_by_containment(phrase, expected):
    step = prepared('All genes ' + phrase + ' chr17 43–46 Mb')['steps'][0]
    assert {(c['property'], c['operator']) for c in step['constraints'] if c['property'].endswith('_loc')} == expected


def test_explicit_half_open_convention_is_preserved_but_not_assumed_from_numeric_types():
    question = 'Genes overlapping chr17 43000000-46000000, 0-based half-open'
    assert genomic_scope(question)['coordinate_convention'] == '0-based_half-open'
    assert 'coordinate_convention_unverified' in compile_genomic_scope(question, grounding(), proposal())[1]


def test_single_step_wording_preserves_full_request_and_discloses_default_build():
    source = proposal()
    source['interpreted_question'] = 'Show MAPT and PLEKHM1 only.'
    source['steps'][0]['question'] = source['interpreted_question']
    result = prepared(plan=source)
    assert result['interpreted_question'] == QUESTION
    assert result['steps'][0]['question'] == QUESTION + ' Reference assembly: GRCh38.p14 (verified release default).'


def test_region_compilation_is_idempotent():
    first = prepared()
    second, issue = compile_genomic_scope(QUESTION, grounding(), first)
    assert issue is None and second == first


def test_gateway_compiles_region_before_scope_checks_and_revalidates_cache():
    from tests_vnext.test_planning_compiler_gateway import gateway_for

    async def check():
        raw = proposal({'entity_type': 'Gene', 'property': 'name', 'operator': 'IN', 'value': ['MAPT', 'PLEKHM1']})
        raw['interpreted_question'] = 'Show MAPT and PLEKHM1 differential expression.'
        raw['steps'][0]['question'] = ''
        gateway, calls = gateway_for(lambda _: deepcopy(raw))
        result = await gateway.plan(QUESTION, [], grounding=grounding())
        assert not result.get('proposal_issue')
        assert result['steps'] and result['interpreted_question'] == QUESTION
        assert scope_issue(QUESTION, grounding(), result) is None
        assert await gateway.plan(QUESTION, [], grounding=grounding()) == result
        assert len(calls) <= 1

    asyncio.run(check())


def test_interaction_partner_scope_is_not_unilaterally_put_on_both_gene_roles():
    result, issue = compile_genomic_scope('Interactions of genes in chr17 43–46 Mb', grounding(), proposal(kinds=['PHYSICAL_INTERACTION']))
    assert 'multiple_Gene_roles' in issue


def alias_graph():
    graph = FakeGraph()
    graph.rows['Gene'] += [
        {'id': 'ids', 'name': 'IDS', 'labels': ['Gene']},
        {'id': 'mb', 'name': 'MB', 'labels': ['Gene']},
        {'id': 'top', 'name': 'TOP1', 'synonyms': ['top'], 'labels': ['Gene']},
        {'id': 'for', 'name': 'TESTFOR', 'synonyms': ['for'], 'labels': ['Gene']},
        {'id': 'ins', 'name': 'INS', 'labels': ['Gene']},
        {'id': 'mapt', 'name': 'MAPT', 'labels': ['Gene']},
        {'id': 'plekhm1', 'name': 'PLEKHM1', 'labels': ['Gene']},
    ]
    return graph


@pytest.mark.parametrize('question,excluded', [
    ('Which of the Ensembl IDs in the chr17 43–47 Mb region have DEG_in edges for beta cells in T1D?', {'ids', 'mb'}),
    (QUESTION, {'mb'}),
    ('Cross-reference alpha cell DEGs against genes in the chr17 43–46 Mb region.', {'mb'}),
    ('Show me the top 5 differentially expressed genes in beta cells between T1D and non-diabetic', {'top'}),
    ('Find SNVs that are QTLs for gene INS', {'for'}),
])
def test_replay_grammar_regressions_are_not_required_genes(question, excluded):
    matches = make_index(alias_graph()).match(question)
    assert not candidate_ids(matches) & excluded
    if 'INS' in question:
        assert 'ins' in candidate_ids(matches)


@pytest.mark.parametrize('question,identifier', [
    ('Show gene IDS expression', 'ids'), ('Show gene MB expression', 'mb'),
    ('Show gene top expression', 'top'), ('Show symbol for expression', 'for'),
])
def test_explicit_real_word_aliases_remain_available(question, identifier):
    assert identifier in candidate_ids(make_index(alias_graph()).match(question))


def test_locus_aliases_are_context_not_complete_gene_collection():
    mentions = make_index(alias_graph()).match(QUESTION)
    locus = [m for m in mentions if m.get('context_role', {}).get('kind') == 'genomic_locus_label']
    assert candidate_ids(locus) == {'mapt', 'plekhm1'}
    assert all(m['identity_complete'] is False for m in locus)


def test_grounding_fetches_coordinate_metadata_and_keeps_it_in_compact_guidance():
    class RegionGraph(FakeGraph):
        async def _small_query(self, query, params=None):
            if query == COORDINATE_METADATA_QUERY:
                self.calls.append((query, params))
                return [deepcopy(COUNTS)]
            return await super()._small_query(query, params)

    graph = RegionGraph()
    first = asyncio.run(ground_question(graph, QUESTION))
    second = asyncio.run(ground_question(graph, QUESTION))
    assert first['genomic_coordinate_metadata']['default_assembly']['value'] == 'GRCh38.p14'
    assert len([q for q, _ in graph.calls if q == COORDINATE_METADATA_QUERY]) == 1
    text = grounding_guidance(second, max_chars=7000, relation_types=['T1D_DEG_IN'])
    assert 'genomic_scope' in text and 'GRCh38.p14' in text and '43000000' in text
