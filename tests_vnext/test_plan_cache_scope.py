"""Cached plans keep the same semantic checks as fresh planning."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.planning_contract import VerifiedCache


def test_cache_revalidates_filters_and_versions_before_reusing_a_plan(monkeypatch):
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
        assert len(calls) == 1
        assert await gateway.plan(question, [], grounding=grounding) == first
        assert len(calls) == 1  # A valid revisit adds no inference.

        # A corrupt cached plan must not bypass raw requested-scope checking.
        key, (expiry, saved) = next(iter(gateway.plan_cache.values.items()))
        broken = deepcopy(saved)
        broken['steps'][0]['constraints'] = broken['steps'][0]['constraints'][:1]
        gateway.plan_cache.values[key] = (expiry, broken)
        repaired = await gateway.plan(question, [], grounding=grounding)
        assert len(calls) == 2
        assert repaired['steps'][0]['constraints'][1]['value'] == 'Pancreas'

        # Validator changes invalidate the key, even with identical inputs.
        monkeypatch.setattr(scope, 'DIGEST', 'new-scope-contract')
        await gateway.plan(question, [], grounding=grounding)
        assert len(calls) == 3
    asyncio.run(check())
