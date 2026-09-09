import json
from pathlib import Path
import pytest
from pankagent_vnext.graph import tokenize,validate_cypher
from pankagent_vnext.measurement_properties import validation_errors,PROPERTIES

STEP={'question':'Check recorded detection','graph_version':'PanKgraph_08_04','complete':True}
BASE='MATCH(g:Gene)-[r:GENE_DETECTED_IN]->(c:anatomical_structure) '

@pytest.mark.parametrize('tail', ['RETURN r.rank','RETURN r.id','RETURN r ORDER BY r.rank','WITH r AS evidence RETURN evidence.id','WHERE r.rank<5 RETURN r'])
def test_wrong_detection_properties_rejected_in_every_clause(tail):
    assert validation_errors(tokenize(BASE+tail),STEP,{})
    assert any('invalid_relation_property:GENE_DETECTED_IN.' in e for e in validate_cypher(BASE+tail,STEP))


def test_optional_relationship_property_still_checked():
    assert validation_errors(tokenize('OPTIONAL '+BASE+'RETURN r.id'),STEP,{})


def test_known_detection_fields_match_independent_full_inventory():
    path=Path(__file__).parents[1]/'pankagent_vnext/release_schema.json'
    if not path.exists():
        pytest.skip('Full source inventory deliberately excluded from minimal promotion; compared in tracked source tests.')
    registry=json.loads(path.read_text())
    assert PROPERTIES==set(registry['relations']['GENE_DETECTED_IN']['properties'])


def test_all_recorded_properties_are_allowed_without_rewrite():
    for field in PROPERTIES:
        assert validation_errors(tokenize(BASE+'RETURN r.'+field),STEP,{})==[]


def test_other_node_or_relationship_ids_and_rank_are_not_globally_banned():
    query=BASE+'MATCH (x)-[e:GENE_ENRICHED_IN]->(y) RETURN g.id,c.id,e.rank_in_cell_type,id(r)'
    assert validation_errors(tokenize(query),STEP,{})==[]


def test_union_checks_each_binding_independently():
    good=BASE+'RETURN r.median_pct_cells_expressing'
    bad=BASE.replace('[r:', '[detection:')+'RETURN detection.rank'
    assert any('GENE_DETECTED_IN.rank' in e for e in validate_cypher(good+' UNION '+bad,STEP))


def test_lookalike_literal_does_not_bind_relationship_variable():
    query="MATCH(g:Gene) RETURN '[r:GENE_DETECTED_IN]',g.id"
    assert validation_errors(tokenize(query),STEP,{})==[]


def test_other_release_is_not_judged_by_this_property_snapshot():
    assert validation_errors(tokenize(BASE+'RETURN r.rank'),{**STEP,'graph_version':'other_release'}, {})==[]
