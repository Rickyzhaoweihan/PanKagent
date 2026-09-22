"""Numeric age filters preserve fractional values, owner, operation and scope."""
import pytest
from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.age_units import expression
from pankagent_vnext.graph_contract import generation_request,RELATIONS


def step(operator='<', value='100'):
    return {'question':'Find donors under 100 years old','graph_version':'PanKgraph_08_04',
            'constraints':[{'entity_type':'donor','property':'age','operator':operator,'value':value}],
            'relation_types':[],'complete':True}


@pytest.mark.parametrize('operator,value', [('<','100'),('<=','100'),('=','10.5'),('>','0.5'),('>=','90')])
def test_unit_expression_preserves_explicit_numeric_age_predicate(operator,value):
    query=f'MATCH (d:donor) WHERE {expression("d")} {operator} {value} RETURN d'
    assert validate_cypher(query,step(operator,value))==[]


def test_parameter_and_node_alias_preserve_owner():
    query='MATCH (d:donor) WITH d AS cohort WHERE '+expression('cohort')+' < $cutoff RETURN cohort'
    assert validate_cypher(query,step(),{'cutoff':100})==[]


@pytest.mark.parametrize('expression,operator,value',[
    ('toFloat(d.age)','<=',100), ('toFloat(d.age)','<',101),
    ('toFloat(d.age)','<',"'100'"), ('toFloat(d.age)','<','true'),
    ('toFloat(d.age) + 1','<',100), ('abs(toFloat(d.age))','<',100),
    ('coalesce(toFloat(d.age),0)','<',100),
])
def test_wrong_operator_value_or_transformation_does_not_satisfy_filter(expression,operator,value):
    errors=validate_cypher(f'MATCH (d:donor) WHERE {expression} {operator} {value} RETURN d',step())
    assert 'missing_required_filter:age' in errors


@pytest.mark.parametrize('operator,value',[('<=','100'),('=','10.5'),('<','100'),('>=','90')])
def test_integer_cast_rejected_with_actionable_feedback(operator,value):
    errors=validate_cypher(f'MATCH (d:donor) WHERE toInteger(d.age) {operator} {value} RETURN d',step(operator,value))
    assert 'missing_required_filter:age' in errors
    assert 'age_comparison_requires_units:donor.age:use_year_month_expression' in errors


def test_fractional_boundary_demonstrates_why_integer_equivalence_is_unsafe():
    assert int(100.9)<=100
    assert not float(100.9)<=100


def test_wrong_owner_does_not_match_numeric_age():
    errors=validate_cypher('MATCH (d:disease) WHERE toFloat(d.age)<100 RETURN d',step())
    assert 'missing_required_filter:age' in errors


def test_numeric_looking_identifier_never_converts():
    identity={'question':'Find one donor','graph_version':'PanKgraph_08_04','constraints':[{'entity_type':'donor','property':'id','operator':'=','value':'0001'}],'complete':True}
    assert 'missing_required_filter:id' in validate_cypher('MATCH(d:donor) WHERE toFloat(d.id)=1 RETURN d',identity)


def test_every_union_branch_must_preserve_the_numeric_filter():
    good='MATCH(d:donor) WHERE '+expression('d')+'<100 RETURN d'
    bad='MATCH(d:donor) WHERE toInteger(d.age)<100 RETURN d'
    assert 'missing_required_filter:age' in validate_cypher(good+' UNION '+bad,step())
    assert validate_cypher(good+' UNION '+good,step())==[]


def test_return_cast_is_not_a_required_filter_or_a_lossy_filter_warning():
    assert 'missing_required_filter:age' in validate_cypher('MATCH(d:donor) RETURN toFloat(d.age)<100 AS matches',step())
    errors=validate_cypher('MATCH(d:donor) WHERE '+expression('d')+'<100 RETURN toInteger(d.age)<100 AS displayed',step())
    assert not any(e.startswith('age_comparison_requires_units') for e in errors)


@pytest.mark.parametrize('bound',[float('inf'),float('nan')])
def test_nonfinite_parameter_rejected(bound):
    assert 'missing_required_filter:age' in validate_cypher('MATCH(d:donor) WHERE toFloat(d.age)<$age RETURN d',step(),{'age':bound})


def test_initial_generation_guidance_preserves_age_not_integer_truncation():
    text=generation_request(step(),step()['question'])
    assert expression('donor_variable') in text and 'months as years' in text
    assert step()['question'] in text


def test_detection_guidance_rejects_invented_sort_fields_without_substitute_ranking():
    note=RELATIONS['GENE_DETECTED_IN']
    assert 'median_pct_cells_expressing' in note
    assert 'no generic rank or id property' in note
    assert 'do not ORDER BY nonexistent fields or invent a ranking' in note


@pytest.mark.parametrize('raw',['d.age','toFloat(d.age)','toInteger(d.age)'])
def test_raw_age_without_units_cannot_be_accepted_as_numeric(raw):
    assert 'age_comparison_requires_units:donor.age:use_year_month_expression' in validate_cypher('MATCH(d:donor) WHERE '+raw+'<100 RETURN d',step())

@pytest.mark.parametrize('prefix',['OPTIONAL MATCH(d:donor) WHERE ', 'MATCH(d:disease) WHERE '])
def test_unit_expression_still_requires_mandatory_correct_owner(prefix):
    assert 'missing_required_filter:age' in validate_cypher(prefix+expression('d')+'<100 RETURN d',step())

@pytest.mark.parametrize('tail',['+1 < 100','< 100 + 1','<= 100','< 101',"< '100'",'< true'])
def test_reviewed_expression_cannot_hide_wrong_bound_or_extra_arithmetic(tail):
    assert 'missing_required_filter:age' in validate_cypher('MATCH(d:donor) WHERE '+expression('d')+tail+' RETURN d',step())


def test_month_unit_expression_has_same_requested_numeric_bounds():
    for operator in ['<','<=','=','>','>=']:
        assert validate_cypher('MATCH(d:donor) WHERE '+expression('d')+operator+'$years RETURN d',step(operator,14/12),{'years':14/12})==[]


@pytest.mark.parametrize('predicate', ['({age}) < 100', '(({age})) < 100', '(({age}) < 100)', '({age} < 100)', '({age}) < $years'])
def test_reviewed_unit_expression_accepts_semantically_neutral_grouping(predicate):
    query='MATCH(d:donor) WHERE '+predicate.format(age=expression('d'))+' RETURN d'
    assert validate_cypher(query,step(),{'years':100})==[]


@pytest.mark.parametrize('predicate', ['abs({age}) < 100', '1 + ({age}) < 100', '({age}) + 1 < 100',
                                      '(({age}) < 100) = false', '({age}) < 100 + 1',
                                      'coalesce(({age}),0) < 100', '(({age}) < 100) OR true'])
def test_grouping_does_not_approve_changed_numeric_or_boolean_meaning(predicate):
    query='MATCH(d:donor) WHERE '+predicate.format(age=expression('d'))+' RETURN d'
    assert validate_cypher(query,step())


def test_donor_only_filter_rejects_unrequested_disease_membership_join():
    joined=("MATCH (disease:disease)-[r:HAS_DONOR]->(donor:donor) WHERE donor.gender = 'Female' AND ("
            +expression('donor')+") < 100.0 WITH DISTINCT disease,r,donor "
            "ORDER BY disease.id,donor.id WITH collect(DISTINCT disease)+collect(DISTINCT donor) AS nodes, "
            "collect(DISTINCT r) AS edges RETURN nodes,edges")
    current=step();current['constraints'].append({'entity_type':'donor','property':'gender','operator':'=','value':'Female'})
    assert 'unrequested_mandatory_relation:HAS_DONOR' in validate_cypher(joined,current)
    direct=("MATCH (donor:donor) WHERE donor.gender = 'Female' AND ("
            +expression('donor')+") < 100.0 RETURN donor")
    assert validate_cypher(direct,current)==[]
