from unittest.mock import AsyncMock
import asyncio
from copy import deepcopy
from types import SimpleNamespace

from pankagent_vnext.llm import ClaudeGateway, PLAN_SCHEMA
from pankagent_vnext.planning_contract import VerifiedCache


QUESTION = 'Find HPAP spleen BCR samples.'
GROUNDING = {'status': 'ready', 'identity': {'graph_release': 'PanKgraph_08_04'},
    'sample_terminology': {'sources': ['HPAP'], 'modalities': ['BCR']},
    'mentions': [{'requested': 'spleen', 'state': 'resolved', 'candidates': [{
        'id': 'UBERON_0002106', 'name': 'spleen', 'entity_type': 'anatomical_structure'}]}]}


def proposal():
    return {'interpreted_question': QUESTION, 'clarification': None, 'steps': [{
        'id': 's1', 'question': '', 'relation_types': ['HAS_DONOR', 'HAS_SAMPLE'],
        'depends_on': [], 'complete': True, 'evidence_combination': 'independent',
        'constraints': [
            {'property': 'anatomical_structure', 'operator': '=', 'value': 'UBERON_0002106', 'entity_type': None},
            {'property': 'data_modality', 'operator': '=', 'value': 'BCR', 'entity_type': None},
            {'property': 'data_source', 'operator': '=', 'value': 'HPAP', 'entity_type': 'donor'}]}]}


def gateway_for(factory):
    gateway = object.__new__(ClaudeGateway)
    gateway.settings = SimpleNamespace(anthropic_key='mock', model='claude-sonnet-5')
    gateway.budget = SimpleNamespace(asettle=AsyncMock(return_value=None))
    gateway._reserve = AsyncMock(return_value='mock')
    gateway.plan_cache = VerifiedCache()
    calls = []
    async def create(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {}), content=[
            SimpleNamespace(type='tool_use', name='record_plan', input=factory(len(calls)))])
    gateway._create = create
    return gateway, calls


def test_verified_owner_compiles_before_scope_guard_without_repair_or_cache_inference():
    async def check():
        raw = proposal()
        gateway, calls = gateway_for(lambda _: deepcopy(raw))
        result = await gateway.plan(QUESTION, [], grounding=GROUNDING)
        assert len(calls) == 1 and not result.get('proposal_issue')
        fields = result['steps'][0]['constraints']
        assert fields[0]['entity_type'] == 'anatomical_structure' and fields[0]['property'] == 'id'
        assert fields[1]['entity_type'] == 'Sample_node'
        assert result['steps'][0]['constraint_compilation'][0]['requested'] == raw['steps'][0]['constraints'][0]
        assert await gateway.plan(QUESTION, [], grounding=GROUNDING) == result
        assert len(calls) == 1
        assert raw == proposal()
    asyncio.run(check())


def test_ambiguous_owner_gets_one_precise_repair_and_no_false_plan():
    async def check():
        def invalid(_):
            raw = proposal()
            raw['steps'][0]['constraints'][2]['entity_type'] = None
            raw['steps'][0]['constraints'][2]['property'] = 'data_version'
            raw['steps'][0]['constraints'][2]['value'] = 'unscoped release'
            return raw
        gateway, calls = gateway_for(invalid)
        result = await gateway.plan(QUESTION, [], grounding=GROUNDING)
        assert len(calls) == 2
        assert 'ambiguous_property_owner' in calls[1]['messages'][0]['content']
        assert result['proposal_issue'].startswith('ambiguous_property_owner:')
        assert result['recovery']['category'] == 'planning_failure'
        assert not gateway.plan_cache.values
    asyncio.run(check())


def test_cached_plan_with_wrong_owner_is_revalidated_before_reuse():
    async def check():
        gateway, calls = gateway_for(lambda _: proposal())
        original = await gateway.plan(QUESTION, [], grounding=GROUNDING)
        key = next(iter(gateway.plan_cache.values))
        corrupted = deepcopy(original)
        corrupted['steps'][0]['constraints'][0]['entity_type'] = 'disease'
        gateway.plan_cache.put(key, corrupted)
        result = await gateway.plan(QUESTION, [], grounding=GROUNDING)
        assert len(calls) == 2
        assert result['steps'][0]['constraints'][0]['entity_type'] == 'anatomical_structure'
    asyncio.run(check())


def test_strict_planner_supports_scalar_exclusion_without_general_not():
    operators = PLAN_SCHEMA['properties']['steps']['items']['properties']['constraints']['items']['properties']['operator']['enum']
    assert {'!=', '<>'} <= set(operators)
    assert 'NOT IN' not in operators and 'NOT' not in operators
