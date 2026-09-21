from copy import deepcopy
import json

import pytest

from pankagent_vnext.planning_requirements import compile_requested_scope, requirements_issue
from pankagent_vnext.planning_scope import scope_issue


def grounding():
    result = {'status': 'ready', 'identity': {'graph_release': 'PanKgraph_08_04'},
              'schema': {'categories': {'FUNCTION_ANNOTATION.data_source': ['Reactome', 'KEGG']}}, 'mentions': []}
    for name, identifier, kind in [('GCLC', 'ENSG00000001084', 'Gene'), ('NFYA', 'ENSG00000001167', 'Gene'),
                                  ('pancreas', 'UBERON_0001264', 'anatomical_structure')]:
        result['mentions'].append({'requested': name, 'state': 'resolved', 'candidates': [
            {'id': identifier, 'name': name, 'entity_type': kind, 'labels': [kind]}]})
    return result


def field(prop, value, kind=None, operator='=', **extra):
    return {'property': prop, 'value': value, 'entity_type': kind, 'operator': operator, **extra}


def step(gene='GCLC', relation='PART_OF_QTL_SIGNAL', *constraints):
    return {'id': gene, 'question': '', 'relation_types': [relation], 'depends_on': [],
            'constraints': [field('name', gene, 'Gene'), *constraints], 'complete': True}


def plan(*steps):
    return {'steps': list(steps), 'clarification': None}


@pytest.mark.parametrize('existing', [None, field('tissue_name', 'Pancreas,Islet', operator='IN'),
    field('tissue_name', '["Pancreas","Islet"]', operator='IN'),
    field('id', '["UBERON_0001264","UBERON_0000006"]', 'anatomical_structure', 'IN')])
def test_fronted_exact_tissue_compiles_for_each_gene_without_model_roundtrip(existing):
    question = 'In pancreas, what molecular QTL evidence is recorded for each of GCLC and NFYA?'
    original = plan(step('GCLC', 'PART_OF_QTL_SIGNAL', *([existing] if existing else [])), step('NFYA'))
    before = deepcopy(original)
    result, issue = compile_requested_scope(question, grounding(), original)
    assert issue is None and original == before
    assert scope_issue(question, grounding(), result) is None
    for s in result['steps']:
        assert s['constraints'][-1] == field('tissue_id', 'UBERON_0001264', relationship_type='PART_OF_QTL_SIGNAL', owner_kind='relationship')
        assert s['requested_scope_compilation'][0]['raw_question'] == question


def test_scope_guard_does_not_accept_broader_tissue_in_even_if_requested_value_present():
    question = 'Compare pancreatic QTL for GCLC and NFYA'
    data = grounding()
    data['mentions'][-1]['requested'] = 'pancreatic'
    p = plan(step('GCLC', 'PART_OF_QTL_SIGNAL', field('tissue_name', '["Pancreas","Islet"]', operator='IN')), step('NFYA'))
    assert scope_issue(question, data, p).startswith('missing_requested_scope:tissue:')


def test_negative_tissue_and_other_measurement_constraints_are_never_dropped():
    negative = field('tissue_name', 'Islet', operator='!=')
    threshold = field('nominal_p', '0.01', operator='<=', relationship_type='PART_OF_QTL_SIGNAL')
    original = plan(step('GCLC', 'PART_OF_QTL_SIGNAL', negative, threshold))
    result, issue = compile_requested_scope('Show GCLC QTL in pancreas', grounding(), original)
    assert issue == 'conflicting_requested_scope:tissue_id'
    assert result == original
    result, issue = compile_requested_scope('Show GCLC QTL in pancreas', grounding(), plan(step('GCLC', 'PART_OF_QTL_SIGNAL', threshold)))
    assert issue is None and threshold in result['steps'][0]['constraints']


@pytest.mark.parametrize('question', ['Show GCLC QTL and NFYA QTL in pancreas',
                                     'Show QTL for GCLC in pancreas and NFYA across all tissues'])
def test_local_multi_gene_tissue_is_not_forced_on_unrestricted_other_gene(question):
    original = plan(step('GCLC'), step('NFYA'))
    result, issue = compile_requested_scope(question, grounding(), original)
    assert issue is None and result == original


def test_unavailable_or_ambiguous_tissue_is_not_filled():
    for change in ['release', 'ambiguous']:
        data = grounding()
        if change == 'release': data['identity']['graph_release'] = 'other'
        else: data['mentions'][-1]['state'] = 'ambiguous'
        original = plan(step())
        assert compile_requested_scope('Show GCLC QTL in pancreas', data, original) == (original, None)


@pytest.mark.parametrize('source', ['Reactome', 'KEGG'])
def test_explicit_verified_pathway_source_is_filled_and_validator_demands_it(source):
    q = f'Show {source} pathway annotations for GCLC.'
    original = plan(step('GCLC', 'FUNCTION_ANNOTATION'))
    assert requirements_issue(q, grounding(), original).startswith('missing_requested_source_scope:')
    result, issue = compile_requested_scope(q, grounding(), original)
    assert issue is None and requirements_issue(q, grounding(), result) is None
    assert result['steps'][0]['constraints'][-1] == field('data_source', source, relationship_type='FUNCTION_ANNOTATION', owner_kind='relationship')


def test_source_roles_stay_with_each_gene_and_preserve_other_annotations():
    q = 'Show Reactome pathways for GCLC and KEGG pathways for NFYA.'
    original = plan(step('GCLC', 'FUNCTION_ANNOTATION'), step('NFYA', 'FUNCTION_ANNOTATION'))
    result, issue = compile_requested_scope(q, grounding(), original)
    assert issue is None and requirements_issue(q, grounding(), result) is None
    assert [s['constraints'][-1]['value'] for s in result['steps']] == ['Reactome', 'KEGG']
    result['steps'][0]['constraints'][-1]['value'] = 'KEGG'
    assert requirements_issue(q, grounding(), result).startswith('missing_requested_source_scope:')


def test_both_sources_remain_both_but_generic_pathways_are_not_narrowed():
    original = plan(step('GCLC', 'FUNCTION_ANNOTATION'))
    result, issue = compile_requested_scope('Show Reactome and KEGG pathways for GCLC', grounding(), original)
    assert issue is None and json.loads(result['steps'][0]['constraints'][-1]['value']) == ['KEGG', 'Reactome']
    assert compile_requested_scope('Show pathway annotations for GCLC', grounding(), original) == (original, None)


def test_recorded_source_values_are_required_and_negative_source_is_not_rewritten():
    p = plan(step('GCLC', 'FUNCTION_ANNOTATION'))
    data = grounding(); data['schema']['categories'] = {}
    assert compile_requested_scope('Show Reactome pathways for GCLC', data, p)[1] == 'requested_source_inventory_unavailable:FUNCTION_ANNOTATION.data_source'
    p['steps'][0]['constraints'].append(field('data_source', 'KEGG', operator='!=', relationship_type='FUNCTION_ANNOTATION'))
    assert compile_requested_scope('Show Reactome pathways for GCLC', grounding(), p)[1] == 'conflicting_requested_scope:data_source'


def test_unique_disease_is_carried_to_effector_without_adding_donor_diagnosis():
    data=grounding();data['mentions'].append({'requested':'T1D','state':'resolved','candidates':[
        {'id':'MONDO_0005147','name':'type 1 diabetes','entity_type':'disease','labels':['disease']}]})
    p=plan(step('GCLC','EFFECTOR_GENE_OF'),{'id':'donors','relation_types':['HAS_SAMPLE'],'constraints':[],'depends_on':[]})
    result,issue=compile_requested_scope('Show GCLC effector evidence in T1D',data,p)
    assert issue is None
    assert result['steps'][0]['constraints'][-1]['value']=='MONDO_0005147'
    assert result['steps'][1]['constraints']==[]
    p['steps'][0]['constraints'].append(field('id','different-disease','disease'))
    assert compile_requested_scope('Show GCLC effector evidence in T1D',data,p)[1]=='conflicting_requested_scope:disease'
