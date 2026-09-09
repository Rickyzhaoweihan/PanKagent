"""Cached plans keep the same semantic checks as fresh planning."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.planning_contract import VerifiedCache


def test_cache_revalidates_filters_and_versions_before_reusing_a_plan(monkeypatch):
    events = []
    monkeypatch.setattr('pankagent_vnext.llm.provider_event', lambda name, data: events.append((name, data)))
    async def check():
        import pankagent_vnext.planning_scope as scope
        gateway = object.__new__(ClaudeGateway)
        gateway.settings = SimpleNamespace(anthropic_key='mock', model='claude-sonnet-5')
        gateway.budget = SimpleNamespace(settle=lambda *_: None)
        gateway._reserve = lambda *_: 'mock'
        gateway.plan_cache = VerifiedCache()
        calls = []
        question = 'Show QTL evidence for GCLC in pancreas.'
        grounding = {'status':'ready', 'identity':{'graph_release':'PanKgraph_08_04'}, 'mentions':[
            {'requested':'GCLC', 'state':'resolved', 'candidates':[{'id':'ENSG00000001084', 'name':'GCLC', 'entity_type':'Gene'}]},
            {'requested':'pancreas', 'state':'resolved', 'candidates':[{'id':'UBERON_0001264', 'name':'pancreas', 'entity_type':'anatomical_structure'}]}]}
        proposal = {'interpreted_question':question, 'clarification':None, 'steps':[
            {'id':'s1', 'question':'', 'depends_on':[], 'complete':True, 'evidence_combination':'independent',
             'relation_types':['PART_OF_QTL_SIGNAL'], 'constraints':[
                 {'property':'id', 'operator':'=', 'value':'ENSG00000001084', 'entity_type':'Gene'},
                 {'property':'tissue', 'operator':'=', 'value':'Pancreas', 'entity_type':None}]}]}

        async def create(*args, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {}), content=[
                SimpleNamespace(type='tool_use', name='record_plan', input=deepcopy(proposal))])
        gateway._create = create
        first = await gateway.plan(question, [], grounding=grounding)
        assert len(calls) == 0  # This fully grounded lookup has a deterministic plan.
        assert await gateway.plan(question, [], grounding=grounding) == first
        assert len(calls) == 0  # A valid revisit adds no inference.
        assert any(name == 'planning_cache' and data['hit'] for name, data in events)

        def assert_scope(plan):
            assert len(plan['steps']) == 1
            step = plan['steps'][0]
            assert step['relation_types'] == ['PART_OF_QTL_SIGNAL']
            assert step['complete'] is True
            constraints = [{key:c.get(key) for key in ('property', 'operator', 'value', 'entity_type')} for c in step['constraints']]
            assert {'property':'id', 'operator':'=', 'value':'ENSG00000001084', 'entity_type':'Gene'} in constraints
            assert {'property':'tissue_id', 'operator':'=', 'value':'UBERON_0001264', 'entity_type':None} in constraints
            assert len(step['constraints']) == 2
            assert scope.scope_issue(question, grounding, plan) is None
        assert_scope(first)

        # A corrupt cached plan must not bypass raw requested-scope checking.
        key, (expiry, saved) = next(iter(gateway.plan_cache.values.items()))
        broken = deepcopy(saved)
        broken['steps'][0]['constraints'] = [c for c in broken['steps'][0]['constraints'] if c['entity_type'] == 'Gene']
        gateway.plan_cache.values[key] = (expiry, broken)
        repaired = await gateway.plan(question, [], grounding=grounding)
        assert len(calls) == 0
        assert_scope(repaired)
        assert repaired['steps'][0]['requested_scope_compilation']

        # An unrelated cached identity cannot be legitimized by filling tissue.
        wrong_identity = deepcopy(saved)
        next(c for c in wrong_identity['steps'][0]['constraints'] if c['entity_type'] == 'Gene')['value'] = 'ENSG00000001626'
        gateway.plan_cache.values[key] = (expiry, wrong_identity)
        rebuilt = await gateway.plan(question, [], grounding=grounding)
        assert_scope(rebuilt)
        assert any(name == 'planning_cache_rejected' for name, _ in events)
        assert len(calls) == 0

        # Validator changes invalidate the key, even with identical inputs.
        monkeypatch.setattr(scope, 'DIGEST', 'new-scope-contract')
        updated = await gateway.plan(question, [], grounding=grounding)
        assert_scope(updated)
        assert set(gateway.plan_cache.values) > {key}
        assert len(gateway.plan_cache.values) == 2
        assert len(calls) == 0
    asyncio.run(check())
