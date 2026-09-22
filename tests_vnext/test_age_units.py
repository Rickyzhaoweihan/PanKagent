import pytest
from pankagent_vnext.age_units import age_years, expression, expression_at
from pankagent_vnext.graph import tokenize

@pytest.mark.parametrize('raw, expected', [('0 years',0),('1 year',1),('65 years',65),
    ('13 months',13/12),('14 months',14/12),('1 month',1/12),('1.5 years',1.5),('0.5 months',.5/12)])
def test_units_preserve_fractional_years(raw, expected):
    assert age_years(raw)==expected

@pytest.mark.parametrize('raw', [None, 45, True, '45', 'Age at donation', '13 months years',
    '13 months old', '65 days','-1 years','NaN years','inf years','1e3 years','65 Years'])
def test_unknown_metadata_or_units_are_not_invented_ages(raw):
    assert age_years(raw) is None

def test_month_boundaries_do_not_use_integer_years():
    age=age_years('14 months')
    assert age>1 and age<2 and age>=14/12 and not age<=1
    assert age_years('13 months')<age

@pytest.mark.parametrize('name',['d','donor','other_2'])
def test_recognizer_accepts_exact_units_with_current_variable(name):
    tokens=tokenize('WHERE '+expression(name)+' < 100 RETURN '+name)
    found=expression_at(tokens,1)
    assert found and found[0]==name and tokens[found[1]].value=='<'
    assert expression_at(tokenize(expression(name).replace('`','')),0)

@pytest.mark.parametrize('old,new', [('12.0','1.0'),('months?','days?'),('ELSE null','ELSE 0'),
    ('END','END + 1'),("[0]) / 12.0", "[0]) / 12.0 + 5"),('`d`.age','`other`.age')])
def test_changed_units_unknown_defaults_or_mixed_variables_not_equivalent(old,new):
    query=expression('d')
    if old=='`d`.age':
        query=query.replace(old,new,1)
    else:
        query=query.replace(old,new)
    found=expression_at(tokenize(query),0)
    if old=='END':
        # Recognition is an expression boundary. Caller must require a direct
        # comparison immediately after END, so extra arithmetic cannot match.
        assert found and tokenize(query)[found[1]].value=='+'
    else:
        assert found is None

@pytest.mark.parametrize('variable',['d.age','d) SET','d x','d`x',''])
def test_variable_injection_rejected(variable):
    with pytest.raises(ValueError):expression(variable)
