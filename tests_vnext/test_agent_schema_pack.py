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
    path = tmp_path / 'graph_storage.json'; data = json.loads(path.read_text())
    data['registry']['relations']['SIGNAL_COLOC_WITH']['paths'][0]['target'] = ['Invented']
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='endpoints'): SchemaPack(tmp_path)
    assert before.module('graph_storage')['registry']['relations']['SIGNAL_COLOC_WITH']['paths'][0]['target'] != ['Invented']


def test_frontend_fixture_covers_every_entry_without_unbound_slots():
    fixture = json.loads((Path(__file__).parent / 'fixtures/acceptance/frontend.json').read_text())
    assert fixture['displayed_entries'] == 16
    assert len(fixture['cases']) == 14
    assert all('[GENE]' not in c['question'] and '[SNP]' not in c['question'] for c in fixture['cases'])
    assert sum(len(c['sources']) for c in fixture['cases']) == 18
