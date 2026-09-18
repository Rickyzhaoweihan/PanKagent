from unittest.mock import AsyncMock
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest

from pankagent_vnext.llm import ClaudeGateway, PLAN_SCHEMA
from pankagent_vnext.plan_recovery import recover_empty_plan
from pankagent_vnext.planning_output import recover_misplaced_steps


def wrapped_plan():
    steps = [{'id':'s1', 'question':'', 'depends_on':[], 'complete':True,
              'evidence_combination':'independent', 'relation_types':['SIGNAL_COLOC_WITH'],
              'constraints':[{'property':'id', 'operator':'=', 'value':'ENSG00000138031', 'entity_type':'Gene'}]}]
    return {'interpreted_question':'Show ADCY3 colocalization.</interpreted_question>\n<parameter name="steps">'+json.dumps(steps),
            'steps':[], 'clarification':None}


def test_complete_misplaced_tool_argument_is_recovered_without_losing_constraints():
    original = wrapped_plan()
    saved = deepcopy(original)
    fixed, record = recover_misplaced_steps(original, PLAN_SCHEMA)
    assert original == saved
    assert fixed['interpreted_question'] == 'Show ADCY3 colocalization.'
    assert fixed['steps'][0]['constraints'][0]['value'] == 'ENSG00000138031'
    assert record['kind'] == 'misplaced_steps_argument'
    assert len(record['original_sha256']) == 64


def test_ambiguous_truncated_or_invalid_embedded_arguments_are_never_recovered():
    variants = []
    for suffix in [' trailing text', '</parameter>', '<parameter name="clarification">null']:
        original = wrapped_plan(); original['interpreted_question'] += suffix; variants.append(original)
    for field, value in [('steps', [{}]), ('clarification', 'Which disease?')]:
        original = wrapped_plan(); original[field] = value; variants.append(original)
    original = wrapped_plan(); original['interpreted_question'] = original['interpreted_question'][:-1]; variants.append(original)
    original = wrapped_plan(); original['interpreted_question'] = original['interpreted_question'].replace('SIGNAL_COLOC_WITH','INVENTED_EDGE'); variants.append(original)
    original = wrapped_plan(); original['interpreted_question'] = original['interpreted_question'].replace('"id": "s1"','"id": "s1", "id": "s2"'); variants.append(original)
    original = wrapped_plan(); original['interpreted_question'] = original['interpreted_question'].replace('"depends_on": []','"depends_on": "s1"'); variants.append(original)
    for original in variants:
        fixed, record = recover_misplaced_steps(original, PLAN_SCHEMA)
        assert fixed == original
        assert record is None


def test_recovered_output_uses_one_call_and_still_runs_dependency_guard():
    async def check():
        gateway = object.__new__(ClaudeGateway)
        gateway.settings = SimpleNamespace(anthropic_key='mock', model='claude-sonnet-5')
        gateway.budget = SimpleNamespace(asettle=AsyncMock(return_value=None))
        gateway._reserve = AsyncMock(return_value='mock')
        calls = []
        async def create(*args, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {}), content=[
                SimpleNamespace(type='tool_use', name='record_plan', input=wrapped_plan())])
        gateway._create = create
        plan = await gateway.plan('Show ADCY3 colocalization.', [])
        assert len(calls) == 1
        assert plan['steps'][0]['constraints'][0]['value'] == 'ENSG00000138031'
        assert plan['steps'][0]['question'] == 'Show ADCY3 colocalization.'
        from pankagent_vnext.llm import plan_structure_issue
        plan['steps'][0]['depends_on'] = ['missing']
        assert plan_structure_issue(plan) == 'invalid_plan_dependencies'
    asyncio.run(check())


def test_composed_gateway_and_outer_recovery_allow_only_two_planning_calls():
    async def check():
        gateway = object.__new__(ClaudeGateway)
        gateway.settings = SimpleNamespace(anthropic_key='mock', model='claude-sonnet-5')
        gateway.budget = SimpleNamespace(asettle=AsyncMock(return_value=None))
        gateway._reserve = AsyncMock(return_value='mock')
        calls = []
        async def create(*args, **kwargs):
            calls.append(kwargs)
            assert len(calls) <= 2
            plan = {'interpreted_question':'Show ADCY3 coloc.', 'steps':[],
                    'clarification': None if len(calls) == 1 else 'Please provide a concrete entity or graph question.'}
            return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {}), content=[
                SimpleNamespace(type='tool_use', name='record_plan', input=plan)])
        gateway._create = create
        plan = await gateway.plan('Show ADCY3 coloc.', [])
        result = await recover_empty_plan(gateway, plan, 'Show ADCY3 coloc.', [], 10)
        assert len(calls) == 2
        assert result['recovery']['category'] == 'planning_failure'
        assert result['proposal_issue'] == 'empty_executable_plan'
    asyncio.run(check())


@pytest.mark.parametrize('use_model', [False, True])
def test_grounded_tissue_omission_is_filled_without_an_extra_model_call(monkeypatch, use_model):
    if use_model:
        # Exercise compilation of a general planner proposal as well as the
        # fully recognized no-inference lookup path.
        monkeypatch.setattr('pankagent_vnext.pattern_planning.compile_signal_plan', lambda *_: None)
        monkeypatch.setattr('pankagent_vnext.schema_drafting.compile_schema_draft', lambda *_: None)
    async def check():
        from pankagent_vnext.planning_contract import VerifiedCache
        gateway = object.__new__(ClaudeGateway)
        gateway.settings = SimpleNamespace(anthropic_key='mock', model='claude-sonnet-5')
        gateway.budget = SimpleNamespace(asettle=AsyncMock(return_value=None))
        gateway._reserve = AsyncMock(return_value='mock')
        gateway.plan_cache = VerifiedCache()
        calls = []
        grounding = {'status':'ready', 'identity':{'graph_release':'PanKgraph_08_04'}, 'mentions':[
            {'requested':'GCLC', 'state':'resolved', 'candidates':[{'id':'ENSG00000001084', 'name':'GCLC', 'entity_type':'Gene', 'match_kind':'recorded_name'}]},
            {'requested':'pancreas', 'state':'resolved', 'candidates':[{'id':'UBERON_0001264', 'name':'pancreas', 'entity_type':'anatomical_structure', 'match_kind':'recorded_name'}]}]}
        async def create(*args, **kwargs):
            calls.append(kwargs)
            assert len(calls) == 1  # Missing verified tissue needs no resampling.
            current = {'id':'s1', 'question':'', 'depends_on':[], 'complete':True, 'evidence_combination':'independent',
                       'relation_types':['PART_OF_QTL_SIGNAL'], 'constraints':[
                           {'property':'id', 'operator':'=', 'value':'ENSG00000001084', 'entity_type':'Gene'}]}
            plan = {'interpreted_question':'Show all QTL evidence for GCLC in pancreas.', 'steps':[current], 'clarification':None}
            return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {}), content=[
                SimpleNamespace(type='tool_use', name='record_plan', input=plan)])
        gateway._create = create
        plan = await gateway.plan('Show all QTL evidence for GCLC in pancreas.', [], grounding=grounding)
        assert len(calls) == int(use_model)
        assert not plan.get('clarification')
        assert len(plan['steps']) == 1
        step = plan['steps'][0]
        assert step['complete'] is True
        assert step['relation_types'] == ['PART_OF_QTL_SIGNAL']
        constraints = [{key:c.get(key) for key in ('property', 'operator', 'value', 'entity_type')} for c in step['constraints']]
        assert {'property':'id', 'operator':'=', 'value':'ENSG00000001084', 'entity_type':'Gene'} in constraints
        assert {'property':'tissue_id', 'operator':'=', 'value':'UBERON_0001264', 'entity_type':None} in constraints
        assert len(step['constraints']) == 2
        if use_model:
            assert step['requested_scope_compilation']  # Deterministic fill retains its proof.
        from pankagent_vnext.planning_scope import scope_issue
        assert scope_issue('Show all QTL evidence for GCLC in pancreas.', grounding, plan) is None
    asyncio.run(check())


def test_unrequested_within_cell_rank_is_omitted_only_from_model_excerpt(monkeypatch, tmp_path):
    from tests_vnext.test_answer_synthesis import gateway_with_mock, detection_evidence
    gateway, fake, _ = gateway_with_mock(monkeypatch, tmp_path, [])
    evidence = detection_evidence()
    edge = evidence['s1']['edges'][0]
    edge['type'] = 'GENE_ENRICHED_IN'
    edge['properties'] = {'rank_in_cell_type':1236, 'log2_fold_change':1.228773, 'condition':'ND'}
    original = deepcopy(evidence)
    prepared = gateway.prepare_answer('Is INS enriched in beta cells?', evidence)
    assert 'rank_in_cell_type' not in prepared.body
    assert 'log2_fold_change' in prepared.body
    assert evidence == original
    assert prepared.profile['model_context']['omitted_unrequested_fields'] == ['rank_in_cell_type']
    requested = gateway.prepare_answer('What is the rank of INS within beta cells?', evidence)
    assert json.loads(requested.body)['evidence'][0]['edges'][0]['properties']['rank_in_cell_type'] == 1236
    assert evidence == original
    assert fake.stream_calls == fake.create_calls == []
