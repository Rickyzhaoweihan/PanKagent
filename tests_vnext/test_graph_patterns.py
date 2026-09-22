from copy import deepcopy

from pankagent_vnext import graph_patterns
from pankagent_vnext.graph_contract import generation_request
from pankagent_vnext.release_schema import REGISTRY


def test_all_stored_paths_and_rules_are_verified_against_actual_release():
    patterns = graph_patterns.patterns_for(list(REGISTRY['relations']))
    assert len(patterns) == len(REGISTRY['relations'])
    for pattern in patterns:
        relation = pattern['relationship']
        for path in pattern['directed_paths']:
            assert any(set(path['source_labels']) == set(actual['source'])
                       and set(path['target_labels']) == set(actual['target'])
                       for actual in REGISTRY['relations'][relation]['paths'])
        assert pattern['relationship_properties'] == REGISTRY['relations'][relation]['properties']
        assert all(graph_patterns._verified_rule(rule) for rule in pattern['join_rules'])


def test_coloc_pattern_preserves_primary_and_exact_signal_context_not_incidental_nodes():
    pattern = graph_patterns.patterns_for(['SIGNAL_COLOC_WITH'])[0]
    assert pattern['directed_paths'] == [{'source_labels': ['Gene'], 'target_labels': ['disease', 'ontology']}]
    text = graph_patterns.guidance(['SIGNAL_COLOC_WITH'])
    assert 'absent membership must not remove primary coloc' in text
    assert 'recorded signal identifiers with source/context' in text


def test_patterns_are_not_a_known_question_or_entity_allowlist():
    assert graph_patterns.patterns_for(['NOVEL_UNREGISTERED_RELATION']) == []
    output = graph_patterns.patterns_for(['HAS_SAMPLE'])
    assert len(output[0]['directed_paths']) > 1
    assert 'SAME sample' in graph_patterns.guidance(['HAS_SAMPLE'])
    before = deepcopy(graph_patterns.LIBRARY)
    output[0]['join_rules'][0]['guidance'] = 'changed copy'
    assert graph_patterns.LIBRARY == before


def test_wrong_release_does_not_publish_stale_pattern_rules(monkeypatch):
    monkeypatch.setitem(graph_patterns.LIBRARY, 'graph_release', 'other')
    assert graph_patterns.patterns_for(['SIGNAL_COLOC_WITH']) == []


def test_generator_receives_patterns_and_keeps_actual_requested_constraints():
    s = {'question': 'Find molecular QTL for the exact gene', 'relation_types': ['PART_OF_QTL_SIGNAL'],
         'constraints': [{'property': 'id', 'entity_type': 'Gene', 'operator': '=', 'value': 'ENSG00000139515'}],
         'complete': True}
    prompt = generation_request(s, s['question'])
    assert 'QTL tissue and credible_set belong to the relationship' in prompt
    assert 'ENSG00000139515' in prompt
    assert 'without LIMIT' in prompt
    assert len(prompt) <= 4000
