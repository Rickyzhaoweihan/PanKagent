"""Candidate validation must preserve the user's complete statistical scope."""
from copy import deepcopy

import pytest

from pankagent_vnext.graph import validate_cypher

STEP = {'id': 's1', 'question': 'Show all GCLC QTL evidence', 'complete': True,
        'graph_version': 'PanKgraph_08_04', 'relation_types': ['PART_OF_QTL_SIGNAL'],
        'constraints': [{'entity_type': 'Gene', 'property': 'id', 'operator': '=', 'value': 'ENSG00000001084'}]}
QUERY = "MATCH (v:variants)-[r:PART_OF_QTL_SIGNAL]->(g:Gene) WHERE g.id='ENSG00000001084'"


@pytest.mark.parametrize('predicate', [
    'r.pip > 0.99', 'r.nominal_p < 0.00001', 'r.purity = 1', "r.pip > '0.99'",
    'toFloat(r.pip) > 0.99', 'r.pip + 0 > 0.99', 'r.pip > 0.1 + 0.8',
    'r.pip IS NOT NULL', 'r.pip IN [0.8, 0.9]'])
def test_unrequested_qtl_statistics_never_narrow_complete_lookup(predicate):
    errors = validate_cypher(QUERY + ' AND ' + predicate + ' RETURN v,r,g', STEP)
    assert any('measurement_filter' in e for e in errors)


def test_unrequested_coloc_threshold_and_alias_are_rejected():
    step = {**deepcopy(STEP), 'relation_types': ['SIGNAL_COLOC_WITH']}
    query = "MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease) WHERE g.id='ENSG00000001084'"
    assert validate_cypher(query + ' RETURN g,r,d', step) == []
    assert 'unrequested_measurement_filter:pp_h4_abf' in validate_cypher(query + ' AND r.pp_h4_abf>0.99 RETURN g,r,d', step)
    assert 'unrequested_measurement_filter:pp_h4_abf' in validate_cypher(query + ' WITH g,r AS q,d WHERE q.pp_h4_abf>0.99 RETURN g,q,d', step)


def test_parameter_and_property_map_cannot_hide_unrequested_cutoff():
    assert 'unrequested_measurement_filter:pip' in validate_cypher(QUERY + ' AND r.pip>$cut RETURN v,r,g', STEP, {'cut': .9})
    query = QUERY.replace('r:PART_OF_QTL_SIGNAL', 'r:PART_OF_QTL_SIGNAL {pip:0.9}') + ' RETURN v,r,g'
    assert 'unrequested_measurement_filter:pip' in validate_cypher(query, STEP)


@pytest.mark.parametrize('operator,value,literal', [('>=', '0.1', '0.1'), ('=', 1, '1'), ('IN', [0.8, 0.9], '[0.8,0.9]')])
def test_exact_requested_statistic_is_retained(operator, value, literal):
    step = deepcopy(STEP)
    step['constraints'].append({'entity_type': None, 'owner_kind': 'relationship',
                               'relationship_type': 'PART_OF_QTL_SIGNAL', 'property': 'pip',
                               'operator': operator, 'value': value})
    assert validate_cypher(QUERY + f' AND r.pip {operator} {literal} RETURN v,r,g', step) == []


def test_different_relation_owner_cannot_authorize_same_named_statistic():
    step = deepcopy(STEP)
    step['constraints'].append({'entity_type': None, 'relationship_type': 'PART_OF_GWAS_SIGNAL',
                               'property': 'pip', 'operator': '>', 'value': .9})
    assert 'unrequested_measurement_filter:pip' in validate_cypher(QUERY + ' AND r.pip>0.9 RETURN v,r,g', step)


def test_projection_sorting_and_zero_counts_are_unchanged():
    assert validate_cypher(QUERY + ' RETURN v,r,g ORDER BY r.pip DESC', STEP) == []
    assert validate_cypher(QUERY + ' RETURN count(DISTINCT v) AS n', STEP) == []
    assert validate_cypher(QUERY + ' RETURN r.pip > 0.9 AS high_pip,v,r,g', STEP) == []


def test_each_union_branch_and_optional_evidence_are_checked():
    query = QUERY + ' RETURN v,r,g UNION ' + QUERY + ' AND r.pip>0.9 RETURN v,r,g'
    assert 'unrequested_measurement_filter:pip' in validate_cypher(query, STEP)
    query = "MATCH (g:Gene) WHERE g.id='ENSG00000001084' OPTIONAL MATCH (v:variants)-[r:PART_OF_QTL_SIGNAL]->(g) WHERE r.pip>0.9 RETURN v,r,g"
    assert 'unrequested_measurement_filter:pip' in validate_cypher(query, STEP)


def test_top_n_permission_does_not_authorize_extra_statistic_filter():
    assert 'unrequested_measurement_filter:pip' in validate_cypher(QUERY + ' AND r.pip>0.99 RETURN v,r,g LIMIT 5', {**STEP, 'complete': False})


def test_scalar_alias_cannot_hide_unrequested_statistic_filter():
    query = QUERY + ' WITH v,r,g,r.pip AS score WHERE score>0.99 RETURN v,r,g'
    assert 'unrequested_measurement_filter:pip' in validate_cypher(query, STEP)


def test_contract_tracks_repair_and_validator_source_hashes():
    import hashlib
    from pathlib import Path
    from pankagent_vnext import cypher_repair, graph, graph_contract
    assert graph_contract.REPAIR_DIGEST == hashlib.sha256(Path(cypher_repair.__file__).read_bytes()).hexdigest()
    assert graph_contract.VALIDATOR_DIGEST == hashlib.sha256(Path(graph.__file__).read_bytes()).hexdigest()


def test_verified_ranking_direction_is_allowed_without_allowing_other_cutoffs():
    from pankagent_vnext.ranking_contract import attach_to_plan
    step = attach_to_plan({'original_question': 'Count all downregulated genes', 'steps': [
        {'id': 's1', 'question': 'Count all downregulated genes', 'relation_types': ['T1D_DEG_IN'],
         'graph_version': 'PanKgraph_08_04', 'constraints': [], 'complete': True}]})['steps'][0]
    base = 'MATCH (g:Gene)-[r:T1D_DEG_IN]->(c:anatomical_structure) WHERE r.log2_fold_change<0'
    assert validate_cypher(base + ' RETURN count(DISTINCT g)', step) == []
    assert validate_cypher(base + ' AND r.adjusted_p_value<0.05 RETURN count(DISTINCT g)', step)
    assert validate_cypher(base.replace('<0', '<-1') + ' RETURN count(DISTINCT g)', step)
