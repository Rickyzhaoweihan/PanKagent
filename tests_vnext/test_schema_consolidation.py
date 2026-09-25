import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from pankagent_vnext.agent_schemas import active_pack, ROOT, SchemaPack
from pankagent_vnext.agent_schemas.profiling import profile
from pankagent_vnext.schema_tools import inspect_schema, resolve_property_values

@pytest.mark.parametrize('count', range(7))
def test_cardinality_and_tie_order(count):
    p=profile(list(reversed(range(count))))
    assert p['distinct_count']==count
    assert p['exhaustive']==(count<5)
    assert [v['value'] for v in p['examples']]==list(range(count if count<5 else 3))

def test_storage_types_missingness_lists_and_sampling():
    p=profile([None,'',[],['x','x'],['x','y'],'NA',3,'3'])
    assert p['missing_records']==1 and p['empty_string_records']==1 and p['empty_list_records']==1
    assert p['observed_numeric_range']=={'min':3,'max':3}
    assert p['stored_types']['str']==3
    assert p['elements']['examples']==[{'value':'x','frequency':2},{'value':'y','frequency':1}]
    p=profile(['A1','A2','bad'],complete=False,pattern=r'A\d+')
    assert not p['exhaustive'] and p['distinct_count'] is None
    assert p['pattern_check']['mismatching_records']==1

def test_every_bim_source_entry_retained_and_raw_bytes_verified():
    pack=active_pack();bim=pack.module('semantic_interpretation')['bim']
    manifest=json.loads((ROOT.parent/'answer_skills/manifest.json').read_text())
    assert bim['syntax_normalization']==manifest['normalization']
    assert len(bim['coverage'])==len(bim['rules'])==87
    by_source={}
    for r in bim['rules']:
        source=r['source'];by_source.setdefault(source['file'],[]).append(r)
    for filename,rules in by_source.items():
        raw=(ROOT.parent/'answer_skills/upstream'/filename).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==manifest['sha256']['upstream/'+filename]
        # Only the recorded syntax error is normalized, outside quoted strings.
        text=raw.decode(); offsets=[]
        for entry in manifest['normalization']:
            if entry['file']=='upstream/'+filename:offsets=entry['character_offsets']
        for offset in sorted(offsets,reverse=True):
            assert text[offset]==',';text=text[:offset]+text[offset+1:]
        source=json.loads(text)
        for rule in rules:
            value=source[rule['source']['section']];key=rule['source']['key']
            expected=value[int(key)] if isinstance(value,list) else value[key] if isinstance(value,dict) else value
            assert rule['content']==expected

def test_schema_inspection_and_value_lookup_share_owner_and_digest():
    result=inspect_schema(['nodes.Sample_node.properties.anatomical_structure','nodes.anatomical_structure'])
    assert result['status']=='complete' and result['schema_sha256']==active_pack().digest
    assert 'code' in result['items'][0]['definition']['description']
    graph=SimpleNamespace(settings=SimpleNamespace(graph_version='PanKgraph_08_04'),_small_query=AsyncMock(return_value=[{'value':'PLN','frequency':2}]))
    found=asyncio.run(resolve_property_values(graph,'nodes.Sample_node.properties.anatomical_structure','PLN'))
    assert found['values'][0]['value']=='PLN' and found['schema_sha256']==result['schema_sha256']
    blocked=asyncio.run(resolve_property_values(graph,'nodes.donor.properties.id',''))
    assert blocked['status']=='protected' and graph._small_query.await_count==1
    assert inspect_schema([None])['status']=='invalid_request'
    assert asyncio.run(resolve_property_values(graph,None,''))['status']=='invalid_request'
    query=graph._small_query.call_args.args[0]
    assert 'any(x IN n[$property]' in query and 'toString(value)' not in query

def test_stage_paraphrase_does_not_add_diagnosis():
    from pankagent_vnext.semantic_registry import resolve
    from test_sample_scope_recovery import VOCAB
    results=[]
    for q in ['How many T1D stage 1 donors are available in HPAP?','Count the hpap donors recorded as stage 1 of type 1 diabetes.']:
        result=resolve({'id':'donors','question':q,'relation_types':['HAS_DONOR'],'constraints':[],
            'semantic_request':{'source':'user_request','question':q}},VOCAB,'PanKgraph_08_04')
        assert not result.get('semantic_issues')
        results.append({(c['entity_type'],c['property'],c['operator'],c['value']) for c in result['constraints']})
    assert results[0]==results[1]
    assert not any(owner=='disease' or prop=='diabetes_type' for owner,prop,_,_ in results[1])

def test_invalid_combination_role_returns_actionable_preparation_diagnostic():
    from pankagent_vnext.composable_planning import normalize
    plan={'steps':[{'id':s,'question':'partners','depends_on':[]} for s in ['a','b']],
          'combine_operations':[{'id':'c','question':'shared','operator':'intersection',
            'inputs':[{'step_id':s,'entity_type':'Gene','role':'partner'} for s in ['a','b']]}]}
    with pytest.raises(ValueError,match='combination_role_requires_parent_path_spec'):normalize(plan)

def test_clinical_identifiers_never_enter_exportable_property_profiles():
    db=active_pack().module('database_schema')
    for name in ['donor','Sample_node','provenance']:
        assert db['nodes'][name]['properties']['id']['export']=='protected'
    for name in ['HAS_DONOR','HAS_SAMPLE']:
        for field in ['start_id','end_id']:
            assert db['relationships'][name]['properties'][field]['export']=='protected'

def test_performance_gate_excludes_failed_baselines_but_blocks_lost_successes():
    from scripts.acceptance.compare_performance import compare
    def row(key,ok,t=10,c=.1):return {'case':key,'membership_match':ok,'retrieval_complete':ok,
       'stream_match':True,'answer_chars':100,'preview_s':t,'elapsed_s':t,'cost_settled':True,'settled_cost_usd':c}
    before=[row('control-0',True),row('failed-0',False,1,.001)]
    after=[row('control-0',True,11,.11),row('failed-0',True,20,.2)]
    report=compare(before,after)
    assert report['pass'] and report['cost']['candidate_mean']==.11 and report['new_successes']==['failed-0']
    assert not compare(before,[row('control-0',False)])['pass']
    assert not compare(before,[row('control-0',True,13,.1)])['pass']

def test_read_only_extractor_preserves_reviewed_facts_and_profiles_absent_values(tmp_path):
    from scripts.extract_database_schema import extract
    db=active_pack().module('database_schema')
    db['nodes']={'disease':db['nodes']['disease']};db['relationships']={}
    db['nodes']['disease']['properties']={k:v for k,v in db['nodes']['disease']['properties'].items() if k in {'id','name'}}
    class Rows:
        def __init__(self,rows):self.rows=rows
        async def data(self):return self.rows
        def __aiter__(self):
            async def it():
                for r in self.rows:yield r
            return it()
    class Driver:
        def session(self,**kwargs):
            assert kwargs['default_access_mode']=='READ';return self
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def run(self,q,p):
            assert not any(token in q for token in ['CREATE ','SET ','DELETE ','apoc.'])
            if q.startswith('CALL db.labels'):rows=[{'label':'disease'}]
            elif q.startswith('CALL db.relationshipTypes'):rows=[]
            elif 'count(n) AS n' in q:rows=[{'n':3}]
            elif 'UNWIND keys' in q:rows=[{'k':k} for k in ['id','name']]
            elif ' AS frequency' in q:rows=[{'value':v,'frequency':1} for v in (['D1','D2','D3'] if p['key']=='id' else ['A','B',None])]
            elif ' AS lo' in q:rows=[{'lo':None,'hi':None}]
            else:rows=[{'properties':{'id':f'D{i}','name':str(i)}} for i in range(3)]
            return Rows(rows)
    out=asyncio.run(extract(Driver(),'synthetic',db,checkpoint=tmp_path/'checkpoint.json'))
    assert out['observations']['status']=='complete'
    prop=out['nodes']['disease']['properties']['name']
    assert prop['description']==db['nodes']['disease']['properties']['name']['description']
    assert prop['observations']['missing_records']==1 and prop['observations']['exhaustive']
    assert out['nodes']['disease']['observations']['prototypes_exhaustive']
    assert json.loads((tmp_path/'checkpoint.json').read_text())['observations']['status']=='in_progress'

def test_pln_duplicate_display_name_is_reconciled_without_losing_scope():
    from pankagent_vnext.semantic_registry import resolve
    from test_sample_scope_recovery import VOCAB,TISSUES
    q='How many exact scRNA-seq PLN samples are available for HPAP donors with T1D stage 3?'
    bad={'entity_type':'Sample_node','property':'anatomical_structure','operator':'=','value':TISSUES[2][1]}
    step={'id':'samples','question':q,'relation_types':['HAS_SAMPLE'],'constraints':[bad],
          'semantic_request':{'source':'user_request','question':q}}
    out=resolve(step,VOCAB,'PanKgraph_08_04')
    assert not out.get('semantic_issues')
    assert bad not in out['constraints']
    assert any(c['entity_type']=='anatomical_structure' and c['value']==TISSUES[2][0] for c in out['constraints'])
    assert any(c['property']=='data_modality' and c['value']=='scRNA-seq' for c in out['constraints'])
    assert {'t1d_stage','data_source'} <= {c['property'] for c in out['constraints']}
    assert 'duplicate_tissue_owner_reconciled' in str(out)
    # An independently requested actual sample code must not be erased.
    step['constraints'][0]['value']='PLN_B'
    out=resolve(step,VOCAB,'PanKgraph_08_04')
    assert any(c['property']=='anatomical_structure' and c['value']=='PLN_B' for c in out['constraints'])

def test_minimal_populated_prototype_satisfies_formal_contract():
    from jsonschema import Draft202012Validator
    data=json.loads((ROOT/'examples/database_schema.synthetic.json').read_text())
    schema=json.loads((ROOT/'contracts/database_schema.schema.json').read_text())
    Draft202012Validator(schema).validate(data)
    assert data['observations']['source']=='synthetic_fixture'
    assert all(spec['observations']['prototypes_exhaustive'] for spec in data['nodes'].values())

def test_export_audit_finds_protected_ids_even_in_public_relationship_annotations():
    from pankagent_vnext.agent_schemas.export_privacy import audit_export
    data=json.loads((ROOT/'examples/database_schema.synthetic.json').read_text())
    assert audit_export(data,['PRIVATE_DONOR_1'])['passed']
    relation=next(iter(data['relationships'].values()))
    relation['description']='unexpected reference to PRIVATE_DONOR_1'
    result=audit_export(data,['PRIVATE_DONOR_1'])
    assert not result['passed'] and result['identifier_matches']==1
    assert 'PRIVATE_DONOR_1' not in json.dumps(result)
