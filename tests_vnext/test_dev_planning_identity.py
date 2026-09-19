"""Regressions from dev admission; no provider calls or live graph writes."""
import asyncio
from copy import deepcopy
import json

import pytest

from pankagent_vnext.planning_compile import compile_property_owners
from pankagent_vnext.planning_scope import scope_issue
from tests_vnext.test_preplanning_grounding import FakeGraph, make_index


GENE_ID = 'ENSG00000138031'
QUESTION = 'Which genes have recorded physical interactions with ADCY3? Use graph evidence only; no literature.'


def grounding(question=QUESTION, graph=None):
    return {'status': 'ready', 'identity': {'graph_release': 'PanKgraph_08_04'}, 'catalog_complete': True,
            'mentions': make_index(graph).match(question)}


def plan(value='ADCY3', operator='=', owner='Gene'):
    return {'clarification': None, 'steps': [
        {'id': 's1', 'relation_types': ['PHYSICAL_INTERACTION'], 'depends_on': [],
         'constraints': [{'entity_type': owner, 'property': 'hgnc_symbol',
                          'value': value, 'operator': operator}]}]}


def compile_plan(original, data=None, question=QUESTION):
    return compile_property_owners(original, data or grounding(question), question=question)


def test_primary_symbol_compiles_to_release_verified_identity_before_scope_guard():
    original = plan()
    before = deepcopy(original)
    data = grounding()
    candidate = data['mentions'][0]['candidates'][0]
    assert candidate['hgnc_symbol'] == 'ADCY3'
    compiled, issue = compile_plan(original, data)
    assert issue is None and original == before
    binding = compiled['steps'][0]['constraints'][0]
    assert (binding['entity_type'], binding['property'], binding['value']) == ('Gene', 'id', GENE_ID)
    assert compiled['steps'][0]['constraint_compilation'][0]['requested'] == before['steps'][0]['constraints'][0]
    assert scope_issue(QUESTION, data, compiled) is None


@pytest.mark.parametrize('value', ['AC3', 'CFTR', GENE_ID, 'unrecorded'])
def test_historic_alias_wrong_gene_or_arbitrary_value_cannot_be_primary_symbol(value):
    data = grounding()
    compiled, issue = compile_plan(plan(value), data)
    assert issue is None
    assert compiled['steps'][0]['constraints'][0]['property'] == 'hgnc_symbol'
    assert scope_issue(QUESTION, data, compiled).startswith('missing_requested_scope:Gene:')


@pytest.mark.parametrize('mutation', ['missing_primary_symbol', 'missing_uniqueness', 'ambiguous', 'incomplete', 'conflicting_identity', 'incomplete_catalog', 'unknown_catalog'])
def test_missing_or_uncertain_primary_symbol_proof_is_never_invented(mutation):
    data = grounding()
    mention = data['mentions'][0]
    if mutation == 'missing_primary_symbol':
        mention['candidates'][0].pop('hgnc_symbol')
    elif mutation == 'missing_uniqueness':
        mention['candidates'][0].pop('hgnc_symbol_unique')
    elif mutation == 'ambiguous':
        mention['state'] = 'ambiguous'
        mention['candidates'].append({**mention['candidates'][0], 'id': 'different-gene'})
    elif mutation == 'incomplete':
        mention['identity_complete'] = False
    elif mutation == 'incomplete_catalog':
        data['catalog_complete'] = False
    elif mutation == 'unknown_catalog':
        data.pop('catalog_complete')
    else:
        data['mentions'].append({**deepcopy(mention), 'candidates': [{**mention['candidates'][0], 'id': 'different-gene'}]})
    compiled, issue = compile_plan(plan(), data)
    assert issue is None
    assert compiled['steps'][0]['constraints'][0]['property'] == 'hgnc_symbol'


@pytest.mark.parametrize('operator', ['!=', '<>', 'NOT IN', 'CONTAINS'])
def test_nonpositive_primary_symbol_predicates_retain_their_literal_meaning(operator):
    value = ['ADCY3'] if operator == 'NOT IN' else 'ADCY3'
    compiled, issue = compile_plan(plan(value, operator))
    assert issue is None
    actual = compiled['steps'][0]['constraints'][0]
    assert (actual['property'], actual['value'], actual['operator']) == ('hgnc_symbol', value, operator)
    assert scope_issue(QUESTION, grounding(), compiled).startswith('missing_requested_scope:Gene:')


@pytest.mark.parametrize('value', [['ADCY3'], '["ADCY3"]'])
def test_positive_set_preserves_operator_and_encoding(value):
    compiled, issue = compile_plan(plan(value, 'IN'))
    actual = compiled['steps'][0]['constraints'][0]
    assert issue is None and actual['operator'] == 'IN' and actual['property'] == 'id'
    assert (json.loads(actual['value']) if isinstance(value, str) else actual['value']) == [GENE_ID]
    assert scope_issue(QUESTION, grounding(), compiled) is None


def test_mixed_verified_and_unverified_symbol_set_is_not_partially_rewritten():
    compiled, issue = compile_plan(plan(['ADCY3', 'unrecorded'], 'IN'))
    assert issue is None
    assert compiled['steps'][0]['constraints'][0]['property'] == 'hgnc_symbol'


def test_raw_field_request_and_wrong_owner_are_not_reinterpreted_as_gene_identity():
    question = 'Find physical interactions where Gene.hgnc_symbol is ADCY3.'
    compiled, issue = compile_plan(plan(), grounding(question), question)
    assert issue is None and compiled['steps'][0]['constraints'][0]['property'] == 'hgnc_symbol'


def test_duplicate_primary_symbol_anywhere_in_catalog_cannot_be_hidden_by_display_name_preference():
    graph = FakeGraph()
    graph.rows['Gene'].append({'id': 'duplicate-symbol-gene', 'name': 'OTHERNAME', 'hgnc_symbol': 'ADCY3', 'labels': ['Gene']})
    data = grounding(graph=graph)
    assert len(data['mentions'][0]['candidates']) == 1  # Canonical name still identifies the requested record.
    assert data['mentions'][0]['candidates'][0]['hgnc_symbol_unique'] is False
    compiled, issue = compile_plan(plan(), data)
    assert issue is None and compiled['steps'][0]['constraints'][0]['property'] == 'hgnc_symbol'
    assert scope_issue(QUESTION, data, compiled).startswith('missing_requested_scope:Gene:')
    _, issue = compile_plan(plan(owner='disease'))
    assert issue == 'invalid_property_owner:s1:disease.hgnc_symbol'


def test_same_type_interaction_endpoints_cannot_lose_a_second_requested_gene():
    question = 'Show physical interaction evidence between ADCY3 and CFTR.'
    data = grounding(question)
    compiled, issue = compile_plan(plan(), data, question)
    assert issue is None
    assert scope_issue(question, data, compiled) == 'missing_requested_scope:Gene:cftr'
    question = 'Show physical interactions and QTL evidence for ADCY3.'
    data = grounding(question)
    original = plan()
    original['steps'].append({'id': 'qtl', 'relation_types': ['PART_OF_QTL_SIGNAL'],
                              'depends_on': [], 'constraints': []})
    compiled, issue = compile_plan(original, data, question)
    assert issue is None
    assert scope_issue(question, data, compiled) == 'missing_requested_scope:Gene:adcy3:qtl'


@pytest.mark.parametrize('quantifier', ['all', 'any', 'every'])
def test_lowercase_quantifiers_do_not_add_incidental_gene_anchors(quantifier):
    graph = FakeGraph()
    graph.rows['Gene'].append({'id': 'synthetic-homonym', 'name': 'HOMONYM', 'labels': ['Gene'], 'synonyms': [quantifier.upper()]})
    question = f'Show {quantifier} recorded PHYSICAL_INTERACTION partners of the gene with Gene.id {GENE_ID} (ADCY3).'
    data = grounding(question, graph)
    assert {c['id'] for m in data['mentions'] for c in m['candidates']} == {GENE_ID}
    original = plan()
    original['steps'][0]['constraints'][0].update(property='id', value=GENE_ID)
    assert scope_issue(question, data, original) is None
    # Explicit symbols and typed identities still retain the recorded alias.
    for explicit in (f'Show interactions of gene {quantifier}.', f'Show interactions of {quantifier.upper()}.'):
        matches = make_index(graph).match(explicit)
        assert any(c['id'] == 'synthetic-homonym' for m in matches for c in m['candidates'])


def test_catalog_primary_symbol_is_not_synthesized_from_display_name_or_synonym():
    graph = FakeGraph()
    graph.rows['Gene'][0].pop('hgnc_symbol')
    candidate = grounding(graph=graph)['mentions'][0]['candidates'][0]
    assert 'hgnc_symbol' not in candidate
    graph.rows['Gene'][0]['hgnc_symbol'] = 'PRIMARY3'
    candidate = grounding(graph=graph)['mentions'][0]['candidates'][0]
    assert candidate['name'] == 'ADCY3' and candidate['hgnc_symbol'] == 'PRIMARY3'
    compiled, issue = compile_plan(plan(), grounding(graph=graph))
    assert issue is None and compiled['steps'][0]['constraints'][0]['property'] == 'hgnc_symbol'


def test_new_inventory_rebuilds_old_fieldless_cache_using_only_metadata_reads(tmp_path):
    from pankagent_vnext.grounding_inventory import build_inventory, stable_digest, write_inventory, load_inventory, inventory_identity
    from pankagent_vnext.preplanning_grounding import Grounder
    async def scenario():
        graph = FakeGraph()
        old = await build_inventory(graph)
        old['identity']['version'] = 'grounding-inventory-4'
        for record in old['records']:
            record.pop('hgnc_symbol', None)
        old['content_digest'] = stable_digest({k: v for k, v in old.items() if k not in {'built_at', 'content_digest'}})
        path = tmp_path / 'public-catalog.json'
        write_inventory(path, old)
        graph.calls.clear()
        grounder = Grounder(graph, path)
        index = await grounder.warm()
        assert index.identity['version'] == 'grounding-inventory-5'
        assert index.match('ADCY3')[0]['candidates'][0]['hgnc_symbol'] == 'ADCY3'
        assert graph.calls and all(query.startswith('MATCH ') and ' RETURN ' in query for query, _ in graph.calls)
        count = len(graph.calls)
        assert await grounder.warm() is index and len(graph.calls) == count
        assert load_inventory(path, inventory_identity(graph))['identity']['version'] == 'grounding-inventory-5'
    asyncio.run(scenario())


def test_old_persisted_preview_remains_readable_but_cannot_be_reused_after_contract_change(tmp_path, monkeypatch):
    from pankagent_vnext import app as app_module
    from test_plan_preview import PreviewGraph
    from test_runtime import service, new_plan
    async def scenario():
        current_contract = app_module.CONTRACT_DIGEST
        monkeypatch.setattr(app_module, 'CONTRACT_DIGEST', 'previous-planning-contract')
        async with service(tmp_path, graph=PreviewGraph()) as (client, runtime, gateway, graph, literature):
            created = await new_plan(client)
            saved = deepcopy(runtime.store.get(created['run_id']))
            old_identity = runtime.preview_identity(saved['plan'])
            counts = (gateway.plans, gateway.syntheses, graph.calls, literature.calls)
            monkeypatch.setattr(app_module, 'CONTRACT_DIGEST', current_contract)
            assert runtime.preview_identity(saved['plan']) != old_identity
            response = await client.get(f"/v2/runs/{created['run_id']}")
            assert response.status_code == 200 and response.json()['rerun_advisory']
            assert runtime.store.get(created['run_id']) == saved
            assert (gateway.plans, gateway.syntheses, graph.calls, literature.calls) == counts
            response = await client.post(f"/v2/plans/{created['plan_id']}/confirm")
            assert response.status_code == 409
            assert (gateway.plans, gateway.syntheses, graph.calls, literature.calls) == counts
    asyncio.run(scenario())
