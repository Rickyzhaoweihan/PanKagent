import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import pytest
from pankagent_vnext.entity_lookup import lookup, resolve_entities, valid_selection, selection_token
from pankagent_vnext.graph import GraphAdapter
from pankagent_vnext.planning_session import run, initial_assessment


def graph(rows):
    g = object.__new__(GraphAdapter)
    g.settings = SimpleNamespace(graph_version='PanKgraph_08_04', graph_timeout=3)
    g._ensure_identity = AsyncMock()
    g._small_query = AsyncMock(return_value=rows)
    g.preview_identity = lambda: {'graph_version': g.settings.graph_version}
    return g


def test_recorded_synonyms_casefold_collision_and_no_implicit_fuzzy_choice():
    async def check():
        g = graph([{'id': 'g1', 'name': 'TSPAN6', 'synonyms': ['TM4SF6'], 'labels': ['Gene']}])
        found = await lookup(g, 'tm4sf6', 'Gene')
        assert found['status'] == 'resolved'
        proof = found['candidates'][0]['selection_proof']
        assert proof['match_method'] == 'recorded_alias'
        assert valid_selection(g, proof, 'Find tm4sf6 expression')
        assert not valid_selection(g, {**proof, 'id': 'g2'}, 'Find tm4sf6 expression')
        assert not valid_selection(g, proof, 'Find INS expression')
        g._small_query.return_value.append({'id': 'g2', 'name': 'OTHER', 'synonyms': ['TM4SF6'], 'labels': ['Gene']})
        assert (await lookup(g, 'tm4sf6', 'Gene'))['status'] == 'ambiguous'
        g._small_query.side_effect = [[], [{'id': 'g1', 'name': 'TSPAN6', 'labels': ['Gene'], 'score': 2}]]
        fuzzy = await lookup(g, 'TSPNA6', 'Gene', True)
        assert fuzzy['status'] == 'candidates'
        assert fuzzy['candidates'][0]['match_method'] == 'fuzzy'
    asyncio.run(check())


def test_service_errors_are_unavailable_not_empty():
    async def check():
        g = graph([]); g._small_query.side_effect = RuntimeError('offline')
        result = await resolve_entities(g, [{'mention': 'INS', 'entity_type': 'Gene', 'fuzzy': False}])
        assert result['results'][0]['status'] == 'unavailable'
        assert initial_assessment({'status': 'unavailable'}) == {'status': 'unavailable', 'diagnostic': 'E01', 'blocking': False}
    asyncio.run(check())


def test_signed_selection_survives_routing_flag_but_not_scope_mutation():
    async def check():
        g = graph([{'id': 'g1', 'name': 'TSPAN6', 'labels': ['Gene']}])
        proof = {'id': 'g1', 'name': 'TSPAN6', 'mention': 'TM4SF6', 'entity_type': 'Gene',
                 'match_method': 'recorded_alias', 'graph_release': g.settings.graph_version}
        proof['token'] = selection_token(g, proof)
        step = {'semantic_request': {'source': 'user_request', 'question': 'Find TM4SF6'},
                'entity_selection_proofs': [proof]}
        c = {'entity_type': 'Gene', 'property': 'id', 'operator': '=', 'value': 'g1'}
        out = await g._resolve_constraint(c, 0, step)
        assert out['original_requested']['value'] == 'TM4SF6'
        step.update(graph_version=g.settings.graph_version, resolved_entities=[out], constraints=[c])
        step['resolution_key'] = g._resolution_signature(step)
        step['gpu_participation_required'] = True
        assert g._resolution_verified(step)
        assert await g._prepare_step(step, AsyncMock()) == step
        step['constraints'][0]['value'] = 'wrong'
        assert not g._resolution_verified(step)
    asyncio.run(check())


def gateway(replies):
    g = SimpleNamespace(settings=SimpleNamespace(model='mock'), budget=SimpleNamespace(asettle=AsyncMock()),
                        _reserve=AsyncMock(return_value='reservation'), _options=lambda: {})
    calls = []
    async def create(*args, **kwargs):
        calls.append(kwargs)
        name, payload = replies[min(len(calls)-1, len(replies)-1)]
        return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {}), content=[
            SimpleNamespace(type='tool_use', name=name, input=deepcopy(payload), id=str(len(calls)))])
    g._create = create
    return g, calls


SCHEMA = {'type': 'object', 'properties': {}, 'required': []}
PLAN = {'interpreted_question': 'Find TM4SF6', 'steps': [{'id': 'q1', 'question': 'Find TM4SF6'}], 'clarification': None}


def test_lookup_then_model_choice_verified_preparation_and_warning():
    async def check():
        db = graph([{'id': 'g1', 'name': 'TSPAN6', 'synonyms': ['TM4SF6'], 'labels': ['Gene']}])
        request = {'requests': [{'mention': 'TM4SF6', 'entity_type': 'Gene', 'fuzzy': True}]}
        proposal = {**PLAN, 'entity_choices': [{'mention': 'TM4SF6', 'entity_type': 'Gene', 'id': 'g1', 'reason': 'Recorded gene synonym'}]}
        g, calls = gateway([('resolve_entities', request), ('record_plan', proposal)])
        prep = AsyncMock(side_effect=lambda p: p)
        result = await run(g, 'Find TM4SF6', '{}', 'test', SCHEMA, 500, lambda p,c: p,
                           resolver=lambda r: resolve_entities(db, r), preparer=prep)
        assert len(calls) == 2 and prep.await_count == 1
        assert result['interpretation_warnings'] and result['entity_selection_proofs']
        assert 'token' not in calls[1]['messages'][-1]['content'][0]['content']
        assert result['planning_route']['lookup_batches'] == 1
    asyncio.run(check())


def test_three_turn_ceiling_and_two_lookup_batch_ceiling():
    async def check():
        request = {'requests': [{'mention': 'INS', 'entity_type': 'Gene', 'fuzzy': False}]}
        g, calls = gateway([('resolve_entities', request)])
        resolver = AsyncMock(return_value={'results': []})
        result = await run(g, 'INS', '{}', 'test', SCHEMA, 500, lambda p,c: p, resolver=resolver)
        assert len(calls) == 3 and resolver.await_count == 2
        assert calls[-1]['tool_choice'] == {'type': 'tool', 'name': 'record_plan'}
        assert result['planning_route']['claude_calls'] == 3
    asyncio.run(check())


def test_preparation_diagnostics_return_to_same_session():
    async def check():
        g, calls = gateway([('record_plan', PLAN)])
        prep = AsyncMock(side_effect=[{**PLAN, 'clarification': 'ambiguous owner'}, PLAN])
        result = await run(g, 'Find TM4SF6', '{}', 'test', SCHEMA, 500, lambda p,c: p, preparer=prep)
        assert len(calls) == 2 and not result.get('clarification')
        assert 'ambiguous owner' in calls[1]['messages'][-1]['content'][0]['content']
    asyncio.run(check())


def test_unverified_model_identity_repaired_never_executed():
    async def check():
        g, calls = gateway([('record_plan', {**PLAN, 'entity_choices': [{'mention': 'TM4SF6', 'entity_type': 'Gene', 'id': 'invented', 'reason': 'guess'}]})])
        prep = AsyncMock()
        result = await run(g, 'Find TM4SF6', '{}', 'test', SCHEMA, 500, lambda p,c: p, preparer=prep)
        assert len(calls) == 3 and prep.await_count == 0
        assert 'entity_choice_requires' in result['proposal_issue']
    asyncio.run(check())

@pytest.mark.parametrize('source', ['hpap', 'Hpap', 'HPAP'])
def test_cohort_case_variants_preserve_stage_and_source(source):
    from pankagent_vnext.semantic_registry import resolve
    from test_sample_scope_recovery import VOCAB
    question = f'How many T1D stage 1 donors are available in {source}?'
    step = {'id':'donors','question':question,'relation_types':['HAS_DONOR'], 'constraints':[],
            'semantic_request':{'source':'user_request','question':question}}
    result = resolve(step, VOCAB, 'PanKgraph_08_04')
    assert not result.get('semantic_issues')
    assert {'data_source','t1d_stage'} <= {c['property'] for c in result['constraints']}
    assert next(c['value'] for c in result['constraints'] if c['property']=='data_source') == 'HPAP'


def test_nonexistent_lookup_and_targeted_clarification_do_not_execute():
    async def check():
        db = graph([])
        assert (await lookup(db, 'NOT_A_GENE', 'Gene'))['status'] == 'not_found'
        clarification = {'steps': [], 'clarification': 'Does ABC refer to gene ABC1 or ABC2?',
                         'interpreted_question': 'Find ABC expression'}
        g, calls = gateway([('record_plan', clarification)])
        prep = AsyncMock()
        result = await run(g, 'Find ABC expression', '{}', 'test', SCHEMA, 500, lambda p,c: p, preparer=prep)
        assert result['clarification'] == clarification['clarification']
        assert len(calls) == 1 and prep.await_count == 0
    asyncio.run(check())
