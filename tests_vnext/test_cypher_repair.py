import hashlib

import pytest

from pankagent_vnext.cypher_repair import repair_candidate, failure_categories
from pankagent_vnext.release_schema import REGISTRY
from pankagent_vnext.graph import validate_cypher

RELEASE = REGISTRY['release']


def repair(query, release=RELEASE):
    return repair_candidate(query, graph_release=release)


def test_reversed_coloc_and_qtl_are_corrected_only_on_typed_endpoints():
    for query, fragment in [
        ('MATCH (d:disease)-[r:SIGNAL_COLOC_WITH]->(g:Gene) RETURN g,r,d', '(d:disease)<-[r:SIGNAL_COLOC_WITH]-(g:Gene)'),
        ('MATCH (g:Gene)-[r:PART_OF_QTL_SIGNAL]->(v:variants) RETURN g,r,v', '(g:Gene)<-[r:PART_OF_QTL_SIGNAL]-(v:variants)'),
        ('MATCH (g:Gene)<-[r:SIGNAL_COLOC_WITH]-(d:disease) RETURN g,r,d', '(g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease)'),
    ]:
        result = repair(query)
        assert fragment in result['query']
        assert result['changed'] and not result['skipped']
        assert all(x['kind'] == 'unique_direction' for x in result['transformations'])
        assert validate_cypher(result['query'], {'graph_version': RELEASE, 'constraints': [], 'complete': True}) == []


def test_filters_projection_limits_comments_and_literals_are_preserved():
    query = """MATCH (g:gene {Name:$gene})-[r:part_of_qtl_signal {TISSUE_NAME:'Pancreas'}]->(v:variants)
// A comment: (g:Gene)-[r:PART_OF_QTL_SIGNAL]->(v:variants)
WHERE r.PIP >= $threshold AND g.name='literal Gene NAME ->' AND v.id IN $variants
RETURN g,r,v, r.PIP AS pip, {TISSUE_NAME:'Not a schema field'} AS metadata LIMIT 7"""
    result = repair(query)
    fixed = result['query']
    assert '(g:`Gene` {`name`:$gene})<-[r:`PART_OF_QTL_SIGNAL` {`tissue_name`:' in fixed
    assert "WHERE r.`pip` >= $threshold AND g.name='literal Gene NAME ->' AND v.id IN $variants" in fixed
    assert "RETURN g,r,v, r.`pip` AS pip, {TISSUE_NAME:'Not a schema field'} AS metadata LIMIT 7" in fixed
    assert '// A comment: (g:Gene)-[r:PART_OF_QTL_SIGNAL]->(v:variants)' in fixed
    assert result['original_query'] == query
    assert result['original_sha256'] == hashlib.sha256(query.encode()).hexdigest()
    assert result['requires_validation'] is True
    # A repaired query remains invalid for a complete request with a limit.
    assert validate_cypher(fixed, {'graph_version': RELEASE, 'constraints': [], 'complete': True})


@pytest.mark.parametrize('query', [
    'MATCH (g)-[r:SIGNAL_COLOC_WITH]->(d) RETURN g,r,d',
    'MATCH (g:Gene)-[r:PHYSICAL_INTERACTION]->(h:Gene) RETURN g,r,h',
    'MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(h:Gene) RETURN g,r,h',
    'MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]-(d:disease) RETURN g,r,d',
    'MATCH (g:Gene)-[r:MADE_UP_RELATION]->(d:disease) RETURN g,r,d',
    'MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease) RETURN g,r,d',
    'MATCH (g:Gene) RETURN g.gene_id, g.symbol, g.NEAR_NAME',
])
def test_ambiguous_correct_or_unknown_candidates_are_not_rewritten(query):
    assert repair(query)['query'] == query
    assert not repair(query)['changed']


def test_wrong_property_owner_is_never_moved():
    q = 'MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease) WHERE d.t1d_stage=$stage RETURN g,r,d'
    assert repair(q)['query'] == q
    assert validate_cypher(q, {'graph_version': RELEASE, 'constraints': [], 'complete': True})


def test_release_mismatch_disables_all_repair():
    q = 'MATCH (g:gene)<-[r:signal_coloc_with]-(d:Disease) RETURN g,r,d'
    result = repair(q, 'other-release')
    assert result['query'] == q and not result['changed']
    assert result['skipped'] == ['graph_release_mismatch']


def test_union_branches_do_not_share_endpoint_bindings():
    q = ('MATCH (g:Gene)-[r:PART_OF_QTL_SIGNAL]->(v:variants) RETURN g,r,v UNION ALL '
         'MATCH (g)-[r:PART_OF_QTL_SIGNAL]->(v) RETURN g,r,v')
    fixed = repair(q)['query']
    assert fixed.startswith('MATCH (g:Gene)<-[r:PART_OF_QTL_SIGNAL]-(v:variants)')
    assert fixed.endswith('MATCH (g)-[r:PART_OF_QTL_SIGNAL]->(v) RETURN g,r,v')


def test_simple_with_alias_retains_type_and_drops_old_binding():
    q = ('MATCH (g:Gene),(d:disease) WITH g AS gene,d AS disease '
         'MATCH (gene)<-[r:SIGNAL_COLOC_WITH]-(disease) RETURN gene,r,disease')
    assert 'MATCH (gene)-[r:SIGNAL_COLOC_WITH]->(disease)' in repair(q)['query']
    dropped = 'MATCH (g:Gene),(d:disease) WITH d MATCH (g)<-[r:SIGNAL_COLOC_WITH]-(d) RETURN g,r,d'
    assert repair(dropped)['query'] == dropped


def test_scalar_alias_cannot_supply_node_type():
    q = ('MATCH (g:Gene),(d:disease) WITH g.name AS g,d '
         'MATCH (g)<-[r:SIGNAL_COLOC_WITH]-(d) RETURN g,r,d')
    assert repair(q)['query'] == q
    q = 'MATCH (g:Gene) WITH g.name AS g RETURN g.NAME'
    assert repair(q)['query'] == q


def test_relationship_property_alias_uses_correct_owner():
    q = ('MATCH (v:variants)-[r:PART_OF_QTL_SIGNAL]->(g:Gene) '
         'WITH r AS q RETURN q.TISSUE_NAME, q.not_a_property')
    assert repair(q)['query'].endswith('RETURN q.`tissue_name`, q.not_a_property')


def test_named_path_optional_pattern_and_anonymous_node_labels():
    q = ('MATCH p=(g:Gene)-[r:PART_OF_QTL_SIGNAL]->(v:variants) '
         'OPTIONAL MATCH (g)<-[c:SIGNAL_COLOC_WITH]-(d:disease) RETURN p,c,d')
    fixed = repair(q)['query']
    assert '(g:Gene)<-[r:PART_OF_QTL_SIGNAL]-(v:variants)' in fixed
    assert '(g)-[c:SIGNAL_COLOC_WITH]->(d:disease)' in fixed
    q = 'MATCH (:disease)-[:SIGNAL_COLOC_WITH]->(:Gene) RETURN count(*)'
    assert repair(q)['query'] == 'MATCH (:disease)<-[:SIGNAL_COLOC_WITH]-(:Gene) RETURN count(*)'


@pytest.mark.parametrize('query', [
    'MATCH (g:gene)<-[r:SIGNAL_COLOC_WITH*1..2]-(d:Disease) RETURN g,r,d',
    'MATCH (g:gene) CALL { WITH g MATCH (d:Disease) RETURN d } RETURN g,d',
    'MATCH (g:gene) RETURN [g IN $items | g.NAME]',
    'MATCH (g:gene) UNWIND $items AS g RETURN g.NAME',
    'MATCH (g:gene WHERE g.NAME=$name) RETURN g',
    'MATCH (g:gene) SET g.NAME=$name RETURN g',
    'MATCH (g:gene) RETURN "unterminated',
    'MATCH (g:gene RETURN g',
])
def test_unsupported_or_invalid_syntax_abandons_all_tentative_edits(query):
    result = repair(query)
    assert result['query'] == query
    assert not result['changed'] and result['skipped']
    assert result['transformations'] == []


def test_no_property_case_repair_across_quoted_values_or_comments():
    q = "MATCH (g:Gene) RETURN {NAME:'g.NAME'}, 'MATCH (g:gene)' // g.NAME"
    assert repair(q)['query'] == q


def test_unknown_or_incompatible_labels_do_not_support_direction_change():
    for q in ('MATCH (g:Gene:unknown)<-[r:SIGNAL_COLOC_WITH]-(d:disease) RETURN g,r,d',
              'MATCH (g:unknown)<-[r:SIGNAL_COLOC_WITH]-(d:disease) RETURN g,r,d'):
        assert repair(q)['query'] == q


def test_repair_is_idempotent():
    q = 'MATCH (g:gene)<-[r:signal_coloc_with]-(d:Disease) RETURN g.NAME,r,d'
    first = repair(q)
    second = repair(first['query'])
    assert first['changed']
    assert second['query'] == first['query'] and not second['changed']


def test_failure_categories_preserve_important_distinctions():
    assert failure_categories(['invalid_relationship_endpoints:SIGNAL_COLOC_WITH']) == ['direction_or_endpoints']
    assert failure_categories(['invalid_node_property:Gene.tissue_name']) == ['property_or_binding']
    assert failure_categories(['missing_constraint:stage', 'unrequested_limit']) == ['filter_or_scope']
    assert failure_categories(['Neo.ClientError.Statement.SyntaxError', 'unknown_label:Genes']) == ['label', 'syntax']
    assert failure_categories(['incomplete_evidence_projection', 'query_timeout']) == ['incomplete_evidence', 'infrastructure']


def test_return_scalar_alias_does_not_inherit_binding_in_order_by():
    q = 'MATCH (g:Gene) RETURN g.name AS g ORDER BY g.NAME'
    assert repair(q)['query'] == q


def test_chained_directions_and_comment_offsets_remain_valid():
    q = 'MATCH (g:Gene) /*left*/ <-[r:SIGNAL_COLOC_WITH]- /*right*/ (d:disease)-[s:PART_OF_GWAS_SIGNAL]->(v:variants) RETURN g,r,d,s,v'
    fixed = repair(q)['query']
    assert '(g:Gene) /*left*/ -[r:SIGNAL_COLOC_WITH]-> /*right*/ (d:disease)<-[s:PART_OF_GWAS_SIGNAL]-(v:variants)' in fixed


def test_alternative_types_never_receive_direction_repair():
    q = 'MATCH (g:Gene)<-[r:SIGNAL_COLOC_WITH|EFFECTOR_GENE_OF]-(d:disease) RETURN g,r,d'
    assert repair(q)['query'] == q
