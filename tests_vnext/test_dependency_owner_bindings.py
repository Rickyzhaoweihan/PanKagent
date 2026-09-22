import asyncio
from copy import deepcopy

import pytest

from pankagent_vnext.graph import validate_cypher
from test_graph import FakeAdapter

RELEASE = 'PanKgraph_08_04'
GENE, DISEASE, VARIANT = 'ENSG00000138031', 'MONDO_0005147', 'rs13393590'


def specification():
    return {'id': 'coloc', 'question': 'Check the recorded gene and disease relationships from both prior checks.',
        'graph_version': RELEASE, 'complete': True, 'evidence_combination': 'cooccurrence',
        'relation_types': ['SIGNAL_COLOC_WITH'], 'depends_on': ['gwas', 'qtl'],
        'constraints': [{'entity_type': 'Gene', 'property': 'id', 'operator': '=', 'value': GENE},
                        {'entity_type': 'disease', 'property': 'id', 'operator': '=', 'value': DISEASE}]}


PARAMS = {'dep_0': [DISEASE, VARIANT], 'dep_1': [GENE, VARIANT]}
BINDINGS = {
    'dep_0': {'graph_version': RELEASE, 'id_labels': {DISEASE: ['disease'], VARIANT: ['variants', 'sequence_variant']}},
    'dep_1': {'graph_version': RELEASE, 'id_labels': {GENE: ['Gene'], VARIANT: ['variants', 'sequence_variant']}},
}
PREFIX = ('MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease) '
          "WHERE g.id='" + GENE + "' AND d.id='" + DISEASE + "'")
BAD = PREFIX + ' AND g.id IN $dep_0 AND d.id IN $dep_1 RETURN g,r,d'
GOOD = PREFIX + ' AND g.id IN $dep_1 AND d.id IN $dep_0 RETURN g,r,d'


def test_wrong_dependency_node_owners_are_rejected_before_query_execution():
    errors = validate_cypher(BAD, specification(), PARAMS, dependency_bindings=BINDINGS)
    assert any(error.startswith('dependency_owner_mismatch:dep_0:g:') for error in errors)
    assert any(error.startswith('dependency_owner_mismatch:dep_1:d:') for error in errors)
    assert validate_cypher(GOOD, specification(), PARAMS, dependency_bindings=BINDINGS) == []


@pytest.mark.parametrize('query', [BAD + ' UNION ' + GOOD,
    PREFIX + ' WITH g AS gene,d AS disease,r WHERE gene.id IN $dep_0 AND disease.id IN $dep_1 RETURN gene,r,disease',
    BAD.replace('$dep_0', "['MONDO_0005147','rs13393590']")])
def test_each_union_branch_alias_and_literal_dependency_retains_owner_check(query):
    assert any(error.startswith('dependency_owner_mismatch:') for error in
        validate_cypher(query, specification(), PARAMS, dependency_bindings=BINDINGS))


def test_compatible_type_with_no_matching_id_is_a_legitimate_zero():
    params = {'dep_0': ['MONDO_OTHER'], 'dep_1': ['ENSG_OTHER']}
    labels = {'dep_0': {'graph_version': RELEASE, 'id_labels': {'MONDO_OTHER': ['disease']}},
              'dep_1': {'graph_version': RELEASE, 'id_labels': {'ENSG_OTHER': ['Gene']}}}
    assert validate_cypher(GOOD, specification(), params, dependency_bindings=labels) == []


@pytest.mark.parametrize('mutation', ['missing', 'stale', 'partial_labels'])
def test_unknown_parent_labels_do_not_become_guessed_id_prefix_types(mutation):
    labels = deepcopy(BINDINGS)
    if mutation == 'missing':
        labels = {}
    elif mutation == 'stale':
        for entry in labels.values():
            entry['graph_version'] = 'old'
    else:
        for entry in labels.values():
            entry['id_labels'][VARIANT] = []
    assert not any(error.startswith('dependency_owner_mismatch:') for error in
        validate_cypher(BAD, specification(), PARAMS, dependency_bindings=labels))


def test_execute_derives_dependency_types_from_parent_nodes_and_never_runs_bad_query():
    async def check():
        graph = FakeAdapter([[BAD], [GOOD]])
        graph.settings.graph_version = RELEASE
        graph.settings.grounded_query_policy = True
        previous = {
            'gwas': {'status': 'complete', 'graph_version': RELEASE, 'nodes': [
                {'id': DISEASE, 'labels': ['disease']}, {'id': VARIANT, 'labels': ['variants']}]},
            'qtl': {'status': 'complete', 'graph_version': RELEASE, 'nodes': [
                {'id': GENE, 'labels': ['Gene']}, {'id': VARIANT, 'labels': ['variants']}]}}
        async def emit(*args):
            pass
        actual = await graph.execute(specification(), previous, emit)
        assert actual['status'] == 'complete'
        assert any(error.startswith('dependency_owner_mismatch:') for error in actual['validation'][0]['reasons'])
        assert len(graph.retrieved) == 1 and graph.retrieved[0][0] == GOOD
        assert len(graph.explained) == 1 and graph.explained[0][0] == GOOD
    asyncio.run(check())
