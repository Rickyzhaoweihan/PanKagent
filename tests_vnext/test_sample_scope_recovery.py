from copy import deepcopy
import json

import pytest

from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.planning_compile import compile_property_owners
from pankagent_vnext.semantic_registry import (resolve, semantic_intent, dataset_source_owner,
                                              generation_guidance, STAGES)
from test_planning_compile import context, field, plan


TISSUES = [('UBERON_0002106', 'spleen'), ('UBERON_0001264', 'pancreas'),
           ('UBERON_0015865', 'pancreaticosplenic lymph node (proxy for "pancreatic LN")')]
VOCAB = {'stages': [STAGES['1'], 'Stage 2: recorded study category', STAGES['3']],
         'sources': ['HPAP', 'StudyA'], 'modalities': ['scRNA-seq', 'snRNA-seq', 'scATAC-seq', 'snMultiomics', 'BCR-seq', 'TCR-seq'],
         'tissues': [{'id': identifier, 'name': name} for identifier, name in TISSUES],
         'modality_links_verified': True, 'assay_donor_sources': {'snMultiomics': ['HPAP']}}


def sample_step(question, identifier=TISSUES[0][0], **extra):
    return {'id': 's1', 'question': question, 'complete': True, 'depends_on': [], 'relation_types': ['HAS_SAMPLE'],
            'constraints': [{'entity_type': 'anatomical_structure', 'property': 'id', 'operator': '=', 'value': identifier}], **extra}


def resolved(question, identifier=TISSUES[0][0], **extra):
    return resolve(sample_step(question, identifier, **extra), VOCAB, 'PanKgraph_08_04')


@pytest.mark.parametrize('identifier,tissue', TISSUES)
@pytest.mark.parametrize('assay', ['scRNA-seq', 'snRNA-seq', 'scATAC-seq', 'snMultiomics', 'BCR-seq', 'TCR-seq'])
def test_tissue_only_explicit_label_steps_keep_exact_positive_assay_without_donor(identifier, tissue, assay):
    question = f'How many samples labeled with the {assay} assay are recorded for {tissue}?'
    raw = sample_step(question, identifier)
    assert semantic_intent(raw)
    p = resolve(raw, VOCAB, 'PanKgraph_08_04')
    assert not p['semantic_issues']
    assert p['sample_requirements']['modality_groups'] == [[assay]]
    assert p['semantic_registry']['donor_required'] is False
    assert not any(c['entity_type'] in {'donor', 'disease'} for c in p['constraints'])
    query = ('MATCH (t:anatomical_structure)-[r:HAS_SAMPLE]->(s:Sample_node) WHERE t.id = '
             + json.dumps(identifier) + ' AND s.data_modality = ' + json.dumps(assay) + ' RETURN t,r,s')
    assert validate_cypher(query, p) == []
    assert validate_cypher(query.replace(' AND s.data_modality = ' + json.dumps(assay), ''), p)
    assert validate_cypher(query.replace(' WHERE ', ' MATCH (d:donor)-[:HAS_SAMPLE]->(s) WHERE '), p)
    assert 'SAMPLE-ONLY lookup' in generation_guidance(p)


def test_tissue_only_assay_and_tissue_must_filter_the_same_sample():
    p = resolved('Find spleen samples labeled with BCR-seq.')
    wrong = ('MATCH (t:anatomical_structure)-[r:HAS_SAMPLE]->(other:Sample_node), (s:Sample_node) '
             'WHERE t.id="UBERON_0002106" AND s.data_modality="BCR-seq" RETURN t,r,other,s')
    assert 'missing_same_tissue_modality_sample_path' in validate_cypher(wrong, p)


def test_tissue_only_general_rna_capability_keeps_broad_scope_without_forcing_a_cohort():
    p = resolved('Count spleen single-cell RNA-seq samples, including documented RNA components of multiome. Keep original assay labels.')
    assert p['sample_requirements']['modality_groups'] == [['scRNA-seq', 'snMultiomics']]
    assert p['semantic_registry']['donor_required'] is False
    assert not any(c['property'] == 'data_source' for c in p['constraints'])


@pytest.mark.parametrize('assay', ['BCR-seq', 'TCR-seq'])
def test_non_rna_assay_literals_are_also_resolved_without_a_donor_keyword(assay):
    assert resolved(f'Find spleen {assay} samples.')['sample_requirements']['modality_groups'] == [[assay]]


@pytest.mark.parametrize('stage', VOCAB['stages'])
def test_recorded_stage_description_is_not_an_additional_diagnosis_request(stage):
    q = 'How many HPAP donors have a recorded T1D stage of ' + stage.replace(':', ' (', 1) + ')?'
    p = resolve({'question': q, 'relation_types': ['HAS_DONOR'], 'constraints': []}, VOCAB, 'PanKgraph_08_04')
    assert next(c['value'] for c in p['constraints'] if c['property'] == 't1d_stage') == stage
    assert not any(c['entity_type'] == 'disease' or c['property'] == 'diabetes_type' for c in p['constraints'])
    assert p['semantic_registry']['diagnosis_intent']['explicit'] is False


def test_original_user_stage_intent_overrides_generated_diagnosis_wording():
    p = resolve({'question': 'Find HPAP donors diagnosed with T1D in stage 3.', 'relation_types': ['HAS_DONOR'],
                 'constraints': [], 'semantic_request': {'source': 'user_request', 'question': 'How many HPAP donors have recorded stage-3 T1D?'}}, VOCAB, 'PanKgraph_08_04')
    assert not any(c['entity_type'] == 'disease' for c in p['constraints'])
    assert p['semantic_registry']['diagnosis_intent'] == {'explicit': False, 'source': 'original_user_request'}


def test_explicit_user_diagnosis_filter_is_not_lost_after_stage_normalization():
    raw = 'Find HPAP donors diagnosed with T1D who have recorded stage 3.'
    p = resolve({'question': 'Find HPAP donors with T1D stage 3.', 'relation_types': ['HAS_DONOR'], 'constraints': [],
                 'semantic_request': {'source': 'user_request', 'question': raw}}, VOCAB, 'PanKgraph_08_04')
    assert any(c['entity_type'] == 'disease' and c['property'] == 'name'
               and c['value'].casefold() == 't1d' for c in p['constraints'])


@pytest.mark.parametrize('value', ['spleen', 'Spleen', 'UBERON_0002106'])
def test_ownerless_tissue_name_and_id_compile_to_one_verified_identity(value):
    raw = plan(field('anatomical_structure', value), field('data_modality', 'scRNA-seq'),
               field('data_modality', 'multiome', operator='!='))
    g = context(('anatomical_structure', 'UBERON_0002106', 'spleen'))
    before = deepcopy(raw)
    compiled, issue = compile_property_owners(raw, g)
    assert issue is None and raw == before
    assert compiled['steps'][0]['constraints'][0]['value'] == 'UBERON_0002106'
    assert compiled['steps'][0]['constraints'][0]['entity_type'] == 'anatomical_structure'
    assert compiled['steps'][0]['constraints'][-1]['operator'] == '!='
    compiled['steps'][0]['question'] = 'Find HPAP spleen standalone scRNA-seq samples excluding multiome.'
    p = resolve(compiled['steps'][0], VOCAB, 'PanKgraph_08_04')
    assert not p['semantic_issues']
    assert any(c['entity_type'] == 'donor' and c['property'] == 'data_source' and c['value'] == 'HPAP' for c in p['constraints'])
    assert p['sample_requirements']['modality_groups'] == [['scRNA-seq']]
    assert next(c for c in p['constraints'] if c['operator'] == '!=')['value'] == 'snMultiomics'


def test_tissue_name_collision_or_qualification_does_not_choose_an_identity():
    g = context(('anatomical_structure', 'tissue_a', 'Same tissue'), ('anatomical_structure', 'tissue_b', 'Same tissue'))
    assert compile_property_owners(plan(field('anatomical_structure', 'Same tissue')), g)[1].startswith('ambiguous_property_owner:')
    g = context(('anatomical_structure', 'tissue_a', 'Same tissue'))
    g['mentions'][0]['identity_complete'] = False
    assert compile_property_owners(plan(field('anatomical_structure', 'Same tissue')), g)[1]


@pytest.mark.parametrize('source', ['HPAP', 'StudyA'])
@pytest.mark.parametrize('operator', ['=', '!=', '<>'])
def test_cohort_source_role_corrects_model_sample_owner_without_changing_operator(source, operator):
    question = f'Count {source} stage3 spleen samples excluding multiome.'
    raw = plan(field('data_source', source, 'Sample_node', operator))
    g = context()
    g['sample_terminology']['sources'] = VOCAB['sources']
    result, issue = compile_property_owners(raw, g, question=question)
    assert issue is None
    c = result['steps'][0]['constraints'][0]
    assert c['entity_type'] == 'donor' and c['operator'] == operator and c['value'] == source
    assert result['steps'][0]['constraint_compilation'][0]['requested']['entity_type'] == 'Sample_node'


@pytest.mark.parametrize('question', [
    'Find spleen samples with Sample_node.data_source = HPAP.',
    'Find spleen samples with sample source HPAP.',
    'Find spleen samples with HPAP as the sample source.',
])
def test_explicit_sample_source_is_preserved_without_an_extra_donor_source(question):
    assert dataset_source_owner(question, 'HPAP') == 'Sample_node'
    raw = sample_step(question, constraints=[field('data_source', 'HPAP', 'Sample_node')])
    p = resolve(raw, VOCAB, 'PanKgraph_08_04')
    assert [c['entity_type'] for c in p['constraints'] if c['property'] == 'data_source'] == ['Sample_node']
    assert not p['semantic_registry']['donor_required']


def test_source_owner_requires_recorded_source_and_actual_raw_request():
    raw = plan(field('data_source', 'HPAP', 'Sample_node'))
    g = context()
    for question in ['Find spleen samples.', 'Find gene HPAP in sample evidence.']:
        result, issue = compile_property_owners(raw, g, question=question)
        assert issue is None and result['steps'][0]['constraints'][0]['entity_type'] == 'Sample_node'
    assert dataset_source_owner('Compare donor source HPAP with sample source HPAP.', 'HPAP') is None


def test_positive_source_is_not_substituted_for_a_typed_exclusion():
    raw = sample_step('Find samples excluding HPAP.', constraints=[field('data_source', 'HPAP', 'donor', '!=')])
    p = resolve(raw, VOCAB, 'PanKgraph_08_04')
    assert next(c for c in p['constraints'] if c['property'] == 'data_source')['operator'] == '!='
    raw['constraints'] = []
    rebuilt = resolve(raw, VOCAB, 'PanKgraph_08_04')
    assert not rebuilt['semantic_issues']
    assert next(c for c in rebuilt['constraints'] if c['property'] == 'data_source')['operator'] == '!='


def test_molecular_questions_do_not_trigger_sample_inventory_semantics():
    assert not semantic_intent({'question': 'Which genes are enriched in single-cell RNA-seq samples?',
                                'relation_types': ['GENE_ENRICHED_IN'], 'constraints': []})


@pytest.mark.parametrize('phrase', ['excluding multiome', 'without snMultiomics', 'do not include multiomics'])
def test_raw_deterministic_draft_compiles_direct_recorded_exclusion(phrase):
    q = 'Count HPAP stage3 spleen samples ' + phrase
    result = resolved(q, semantic_request={'source': 'user_request', 'question': q})
    assert not result['semantic_issues']
    assert result['sample_requirements']['modality_groups'] == []
    assert result['sample_requirements']['excluded_modality_constraints'] == [
        {'entity_type': 'Sample_node', 'property': 'data_modality', 'operator': '!=', 'value': 'snMultiomics'}]
    assert any(c['entity_type']=='donor' and c['property']=='data_source' and c['value']=='HPAP' for c in result['constraints'])
    assert any(m['match_kind']=='raw_request_excluded_assay_literal' for m in result['resolved_constraints'])


@pytest.mark.parametrize('provenance', [{}, {'source': 'generated', 'question': 'Count spleen samples excluding multiome'},
                                      {'source': 'user_request', 'question': 'Find spleen and pancreatic samples excluding multiome'}])
def test_generated_step_cannot_fill_an_omitted_exclusion_from_unrelated_raw_question(provenance):
    result = resolved('Count spleen samples excluding multiome', semantic_request=provenance)
    assert result['semantic_issues']
    assert not result['sample_requirements']['excluded_modality_constraints']


def test_raw_draft_unknown_exclusion_is_not_guessed_from_available_assays():
    q='Count spleen samples without unknown assay'
    result=resolved(q, semantic_request={'source':'user_request','question':q})
    assert not result['sample_requirements']['excluded_modality_constraints']


@pytest.mark.parametrize('raw', ['Find CFTR Reactome pathways', 'Find KEGG pathway annotations for CFTR'])
def test_recorded_annotation_resource_disambiguates_only_ownerless_source(raw):
    resource = 'Reactome' if 'Reactome' in raw else 'KEGG'
    ctx=context(schema={'categories': {'FUNCTION_ANNOTATION.data_source': ['KEGG','Reactome']}})
    proposal=plan(field('data_source', resource), relation='FUNCTION_ANNOTATION')
    compiled,issue=compile_property_owners(proposal,ctx,question=raw)
    assert issue is None
    predicate=compiled['steps'][0]['constraints'][0]
    assert predicate['relationship_type']=='FUNCTION_ANNOTATION'
    assert predicate['owner_kind']=='relationship'
    assert compiled['steps'][0]['constraint_compilation'][0]['requested']==proposal['steps'][0]['constraints'][0]


@pytest.mark.parametrize('question,categories,extra', [
    ('Find pathways for CFTR', ['Reactome'], {}),
    ('Find Reactome pathways for CFTR', [], {}),
    ('Find Reactome pathways for CFTR', ['Reactome'], {'owner_kind':'node'})])
def test_annotation_resource_needs_explicit_raw_role_recorded_value_and_no_conflicting_owner(question,categories,extra):
    ctx=context(schema={'categories':{'FUNCTION_ANNOTATION.data_source':categories}})
    _,issue=compile_property_owners(plan(field('data_source','Reactome',**extra),relation='FUNCTION_ANNOTATION'),ctx,question=question)
    assert issue


def test_explicit_revision_adds_clinical_filter_without_loosening_recorded_stage():
    q='Find HPAP donors with recorded stage 3.'
    result=resolve({'question':q,'relation_types':['HAS_DONOR'],'constraints':[],
        'semantic_request':{'source':'user_request','question':q,'revision_instruction':'Also require recorded diagnosed T1D.'}},VOCAB,'PanKgraph_08_04')
    assert not result['semantic_issues']
    assert any(c['entity_type']=='disease' and c['property']=='name'
               and c['value'].casefold()=='t1d' for c in result['constraints'])
    assert any(c['property']=='t1d_stage' and c['value']==STAGES['3'] for c in result['constraints'])
    assert result['semantic_registry']['diagnosis_intent']=={'explicit':True,'source':'revision_add_clinical_filter'}


@pytest.mark.parametrize('instruction',['Remove the clinical-disease filter.', 'Drop the diagnosed T1D filter but keep stage 3.'])
def test_explicit_revision_removes_clinical_filter_even_when_old_question_mentions_diagnosis(instruction):
    q='Find HPAP donors diagnosed with T1D at stage 3.'
    result=resolve({'question':q,'relation_types':['HAS_DONOR'],'constraints':[field('diabetes_type','Diabetes (Type I)','donor')],
        'semantic_request':{'source':'user_request','question':q,'revision_instruction':instruction}},VOCAB,'PanKgraph_08_04')
    assert not result['semantic_issues']
    assert not any(c['entity_type']=='disease' or c['property']=='diabetes_type' for c in result['constraints'])
    assert any(c['property']=='t1d_stage' for c in result['constraints'])


def test_ambiguous_clinical_revision_cannot_silently_modify_typed_filter():
    q='Find HPAP T1D stage 3 donors.'
    disease=field('id','MONDO_0005147','disease')
    result=resolve({'question':q,'relation_types':['HAS_DONOR'],'constraints':[disease],
        'semantic_request':{'source':'user_request','question':q,'revision_instruction':'Change the diagnosed disease filter.'}},VOCAB,'PanKgraph_08_04')
    assert any('ambiguous' in issue for issue in result['semantic_issues'])
    assert disease in result['constraints']


@pytest.mark.parametrize('question',[
    'Count HPAP stage3 spleen samples excluding multiome.',
    'Count HPAP stage3 spleen standalone scRNA-seq samples excluding multiome.'])
def test_schema_draft_reuses_raw_negative_compiler_before_any_model_call(question):
    from test_pattern_planning import grounded
    from pankagent_vnext.schema_drafting import compile_schema_draft
    payload=grounded(question)
    payload['sample_terminology']=dict(VOCAB,inventory_complete=True)
    result=compile_schema_draft(question,payload)
    assert result is not None
    constraints=result['steps'][0]['constraints']
    assert any(c['property']=='data_modality' and c['operator']=='!=' and c['value']=='snMultiomics' for c in constraints)
    assert any(c['entity_type']=='donor' and c['property']=='data_source' and c['value']=='HPAP' for c in constraints)
    assert not any(c['entity_type']=='Sample_node' and c['property']=='data_source' for c in constraints)
    positives=[c['value'] for c in constraints if c['property']=='data_modality' and c['operator']=='=']
    assert positives==(['scRNA-seq'] if 'standalone' in question else [])
    assert not any(c['entity_type']=='disease' for c in constraints)


@pytest.mark.parametrize('operator,value,expected',[('=','hpap','HPAP'),('!=','hpap','HPAP'),('IN','["hpap"]',['HPAP'])])
def test_source_role_binding_preserves_operator_and_recorded_case(operator,value,expected):
    ctx=context(sample_terminology=VOCAB)
    result,issue=compile_property_owners(plan(field('data_source',value,'Sample_node',operator)),ctx,question='Count HPAP samples.')
    assert issue is None
    predicate=result['steps'][0]['constraints'][0]
    assert (predicate['entity_type'],predicate['operator'],predicate['value'])==('donor',operator,expected)
