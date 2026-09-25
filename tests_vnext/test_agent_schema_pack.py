from copy import deepcopy
import json
from pathlib import Path
import pytest
from pankagent_vnext.agent_schemas import SchemaPack, ROOT, active_pack


def test_extracted_release_inventory_and_patterns_have_exact_parity():
    root = ROOT.parent
    pack = active_pack()
    assert pack.module('graph_storage')['registry'] == json.loads((root / 'release_schema.json').read_text())
    assert pack.module('query_patterns')['library'] == json.loads((root / 'graph_patterns.json').read_text())
    assert pack.module('semantics_modalities')['modalities'] == json.loads((root / 'prompts/planning/modalities.json').read_text())


def test_snapshot_cannot_be_mutated_by_consumers():
    pack = active_pack(); value = pack.module('identity'); value['public_labels'].clear()
    assert pack.module('identity')['public_labels']
    identity = pack.identity(); identity['standard']['branch'] = 'wrong'
    assert pack.identity()['standard']['branch'] == 'kg-standard'


def test_pack_validates_relationship_endpoints_and_pins_digest(tmp_path):
    source = ROOT / 'packs/pankgraph'
    for path in source.glob('*.json'): (tmp_path / path.name).write_bytes(path.read_bytes())
    before = SchemaPack(tmp_path)
    path = tmp_path / 'database_schema.json'; data = json.loads(path.read_text())
    data['relationships']['SIGNAL_COLOC_WITH']['endpoints'][0]['target'] = ['Invented']
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='endpoints'): SchemaPack(tmp_path)
    assert before.module('graph_storage')['registry']['relations']['SIGNAL_COLOC_WITH']['paths'][0]['target'] != ['Invented']


def test_frontend_fixture_covers_every_entry_without_unbound_slots():
    fixture = json.loads((Path(__file__).parent / 'fixtures/acceptance/frontend.json').read_text())
    assert fixture['displayed_entries'] == 16
    assert len(fixture['cases']) == 14
    assert all('[GENE]' not in c['question'] and '[SNP]' not in c['question'] for c in fixture['cases'])
    assert sum(len(c['sources']) for c in fixture['cases']) == 18


def test_renamed_types_and_property_use_same_path_and_identity_operators(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from pankagent_vnext import bounded_paths, query_templates, entity_lookup
    registry={'release':'renamed-fixture','nodes':{'Thing':['id','title','aliases'], 'Category':['id','title']},
              'relations':{'BELONGS_TO':{'paths':[{'source':['Thing'],'target':['Category']}],
                                        'properties':['source']}}}
    monkeypatch.setattr(bounded_paths,'REGISTRY',registry)
    monkeypatch.setattr(query_templates,'REGISTRY',registry)
    identity={**active_pack().module('identity'),'name_fields':['title'],'synonym_field':'aliases'}
    monkeypatch.setattr(entity_lookup,'IDENTITY',identity)
    monkeypatch.setattr(entity_lookup,'LABELS',('Thing','Category'))
    class Fixture:
        settings=SimpleNamespace(graph_version='renamed-fixture')
        async def _ensure_identity(self):pass
        async def _small_query(self,query,parameters):
            assert 'n.`title`' in query and 'n.`aliases`' in query
            assert 'hgnc_symbol' not in query and parameters=={'mention':'alternate'}
            return [{'id':'thing-1','title':'Canonical','aliases':['alternate'],'labels':['Thing']}]
    lookup=asyncio.run(entity_lookup.lookup(Fixture(),'alternate','Thing'))
    assert lookup['status']=='resolved' and lookup['candidates'][0]['match_method']=='recorded_alias'
    constraint={'owner_role':'focus','entity_type':'Thing','property':'id','operator':'=','value':'thing-1'}
    step={'id':'one','question':'Categories for alternate','graph_version':'renamed-fixture',
          'relation_types':['BELONGS_TO'],'depends_on':[],'constraints':[constraint],
          'semantic_request':{'source':'user_request','question':'Categories for alternate'},
          'resolved_entities':[{'constraint_index':0,'requested':deepcopy(constraint),'state':'resolved',
              'entity_type':'Thing','labels':['Thing'],'id':'thing-1','graph_version':'renamed-fixture'}],
          'request_filter_bindings':[{'constraint_index':0,'canonical_binding':deepcopy(constraint),
              'source':'immutable_user_request','authorization_kind':'verified_test_request_filter','request_sha256':__import__('hashlib').sha256(b'Categories for alternate').hexdigest(),'graph_release':'renamed-fixture'}],
          'evidence_combination':'cooccurrence','path_spec':{'version':'bounded-path-v1',
              'nodes':[{'role':'focus','entity_types':['Thing']},{'role':'category','entity_types':['Category']}],
              'edges':[{'role':'membership','from':'focus','to':'category','types_any':['BELONGS_TO'],'direction':'out'}]}}
    result=bounded_paths.compile_query(step)
    assert '`Thing`' in result['cypher'] and '`Category`' in result['cypher'] and '`BELONGS_TO`' in result['cypher']
    assert result['parameters']['path_0']=='thing-1'
    assert 'Gene' not in result['cypher']
