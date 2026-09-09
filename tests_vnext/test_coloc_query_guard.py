"""Scientific false-empty regressions; no database or provider calls."""
import pytest
from pankagent_vnext.graph import tokenize, validate_cypher
from pankagent_vnext.coloc_query_guard import validation_errors

BASE = 'MATCH (g:Gene)-[c:SIGNAL_COLOC_WITH]->(d:disease)'
SPEC = {'id':'s1','question':'Show recorded colocalization',
        'graph_version':'PanKgraph_08_04', 'complete':True,
        'constraints':[], 'relation_types':['SIGNAL_COLOC_WITH']}


@pytest.mark.parametrize('prefix', ['MATCH', 'OPTIONAL MATCH'])
@pytest.mark.parametrize('relation', ['PART_OF_GWAS_SIGNAL', 'PART_OF_QTL_SIGNAL'])
def test_separate_signal_availability_cannot_remove_primary_coloc(prefix, relation):
    query = BASE + f' {prefix} (v:sequence_variant)-[r:{relation}]->(g) RETURN g,c,d,v,r'
    assert 'coloc_requires_independent_evidence_checks' in validate_cypher(query, SPEC)


def test_correct_direct_coloc_and_scalar_result_are_valid():
    assert validate_cypher(BASE+' RETURN g,c,d', SPEC) == []
    assert validate_cypher(BASE+' RETURN count(c) AS total', SPEC) == []


def test_wrong_variant_endpoint_cannot_be_reported_as_valid_empty():
    query = 'MATCH (v:variants)-[c:SIGNAL_COLOC_WITH]->(g:Gene) RETURN v,c,g'
    assert 'invalid_relationship_endpoints:SIGNAL_COLOC_WITH' in validate_cypher(query, SPEC)


def test_relationship_words_in_comments_and_strings_are_not_patterns():
    query = BASE+" RETURN g,c,d,'PART_OF_QTL_SIGNAL' // PART_OF_GWAS_SIGNAL"
    assert validation_errors(tokenize(query), SPEC, {}) == []


def test_separate_gwas_query_remains_supported():
    query = 'MATCH (v:sequence_variant)-[r:PART_OF_GWAS_SIGNAL]->(d:disease) RETURN v,r,d'
    assert validation_errors(tokenize(query), SPEC, {}) == []


def test_union_cannot_hide_a_combined_coloc_query():
    query = BASE+' RETURN g,c,d UNION '+BASE+' MATCH (v)-[r:PART_OF_QTL_SIGNAL]->(g) RETURN g,c,d'
    assert 'coloc_requires_independent_evidence_checks' in validate_cypher(query, SPEC)


@pytest.mark.parametrize('primary', ['c:', ':'])
@pytest.mark.parametrize('secondary', ['q:', ':'])
@pytest.mark.parametrize('prefix', ['MATCH', 'OPTIONAL MATCH'])
def test_anonymous_relationships_cannot_hide_a_filtering_join(primary, secondary, prefix):
    query = (f'MATCH (g:Gene)-[{primary}SIGNAL_COLOC_WITH]->(d:disease) '
             f'{prefix} (v:variants)-[{secondary}PART_OF_QTL_SIGNAL]->(g) RETURN g,d')
    assert 'coloc_requires_independent_evidence_checks' in validate_cypher(query, SPEC)


def test_anonymous_nodes_and_quoted_types_also_get_coloc_guard():
    query = ('MATCH (:Gene)-[:`SIGNAL_COLOC_WITH`]->(:disease) '
             'MATCH (:Gene)<-[:`PART_OF_QTL_SIGNAL`]-(:variants) RETURN 0 AS total')
    assert validation_errors(tokenize(query), SPEC, {}) == ['coloc_requires_independent_evidence_checks']


def _normalized_coloc_check():
    from pankagent_vnext.coloc_scope import normalize_plan, RELEASE
    from tests_vnext.test_coloc_scope import plan
    return normalize_plan(plan(), RELEASE)['steps'][0]


def test_compact_generator_task_uses_canonical_bindings_without_query_template_or_mutation():
    import copy
    from pankagent_vnext.coloc_query_guard import compact_generation_request
    step = _normalized_coloc_check(); original = copy.deepcopy(step)
    text = compact_generation_request(step, step['question'])
    assert step == original
    assert len(text) <= 1500
    assert 'Gene.id = "ENSG00000138031"' in text
    assert 'disease.id = "MONDO_0005147"' in text
    assert 'SIGNAL_COLOC_WITH' in text and 'no LIMIT, SKIP, list slicing' in text
    assert 'pp_h4_abf' in text and 'full relationship objects' in text
    assert not any(word in text for word in ('PART_OF_GWAS_SIGNAL', 'PART_OF_QTL_SIGNAL', 'GWAS_signal', 'QTL_signal', 'MATCH ', 'RETURN '))
    from pankagent_vnext.graph_contract import generation_request
    assert generation_request(step, step['question']) == text


@pytest.mark.parametrize('change', ['extra_filter', 'other_relation', 'ranking', 'dependency', 'incomplete', 'stale', 'no_scope'])
def test_compact_generator_task_declines_unsupported_or_modified_scope(change):
    from pankagent_vnext.coloc_query_guard import compact_generation_request
    step = _normalized_coloc_check()
    if change == 'extra_filter': step['constraints'].append({'property': 'data_source', 'operator': '=', 'value': 'specific source'})
    elif change == 'other_relation': step['relation_types'].append('PART_OF_QTL_SIGNAL')
    elif change == 'ranking': step['ranking_contract'] = {'top_n': 5}
    elif change == 'dependency': step['depends_on'] = ['parent']
    elif change == 'incomplete': step['complete'] = False
    elif change == 'stale': step['coloc_scope']['digest'] = 'old'
    elif change == 'no_scope': step.pop('coloc_scope')
    assert compact_generation_request(step, step['question']) is None


def test_compact_generator_requires_verified_name_resolution_and_rejects_conflicting_id():
    from pankagent_vnext.coloc_query_guard import compact_generation_request
    step = _normalized_coloc_check()
    step['constraints'][0].update(property='name', value='ADCY3')
    assert compact_generation_request(step) is None
    step['resolved_entities'] = [{'constraint_index': 0, 'state': 'resolved', 'entity_type': 'Gene', 'id': 'ENSG00000138031'}]
    assert 'ENSG00000138031' in compact_generation_request(step)
    step['constraints'][0].update(property='id', value='wrong')
    assert compact_generation_request(step) is None
