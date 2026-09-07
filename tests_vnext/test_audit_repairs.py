import asyncio
import copy
from pankagent_vnext.evidence_status import outcome_message, confirmation_eligible
from pankagent_vnext.graph_contract import generation_request
from pankagent_vnext.literature_policy import apply_literature_policy, preserve_revision_preference
from tests_vnext.test_runtime import Gateway, PLAN, new_plan, service, wait_state


def test_failure_is_not_biological_absence_or_justified_by_context():
    failed = {'status': 'failed', 'nodes': [], 'purpose': 'primary'}
    context = {'status': 'complete', 'nodes': [{'id': 'context'}], 'purpose': 'context'}
    assert 'retrieval failure' in outcome_message({'a': failed, 'b': context})
    assert not confirmation_eligible({}, {'evidence': {'steps': [failed, context]}})
    assert 'No matching records' in outcome_message({'a': {'status': 'empty'}})
    assert confirmation_eligible({}, {'evidence': {'steps': [{'status': 'empty'}]}})


def test_generation_preserves_unrecognized_modifiers_and_explicit_filters():
    question = 'Compare ductal subclasses in T2D, excluding the MUC5B subset.'
    step = {'relation_types': ['GENE_DETECTED_IN'], 'constraints': [
        {'entity_type': 'Gene', 'property': 'name', 'operator': '=', 'value': 'CFTR'},
        {'property': 'condition', 'operator': '=', 'value': 'T2D'}], 'complete': True}
    generated = generation_request(step, question)
    assert question in generated and 'T2D' in generated and 'GENE_DETECTED_IN' in generated
    assert 'without LIMIT' in generated


def test_explicit_literature_opt_out_survives_unrelated_revision():
    parent = apply_literature_policy({'literature': True}, 'Disable literature')
    assert parent['literature'] is False
    revised = preserve_revision_preference({'literature': True}, parent, 'Use T2D instead')
    assert revised['literature'] is False
    assert preserve_revision_preference(revised, parent, 'Enable literature')['literature'] is True


def test_unconfirmed_parent_is_available_to_revision_planner(tmp_path):
    class RecordingGateway(Gateway):
        async def plan(self, question, history):
            self.history = copy.deepcopy(history)
            return await super().plan(question, history)
    async def scenario():
        gateway = RecordingGateway(plan=copy.deepcopy(PLAN))
        async with service(tmp_path, gateway=gateway) as (client, runtime, *_):
            created = await new_plan(client, 'Which cell types express INS?')
            response = await client.post(f'/v2/plans/{created["plan_id"]}/revise', json={
                'question': 'Disable literature', 'revision_instruction': 'Disable literature', 'revision_mode': 'instruction'})
            revised = await wait_state(client, response.json()['run_id'], {'awaiting_confirmation'})
            context = gateway.history[-1]['revision_context']
            assert context['original_question'] == 'Which cell types express INS?'
            assert context['parent_plan']['steps']
            assert context['instruction'] == 'Disable literature'
            assert revised['plan']['literature'] is False
            assert revised['plan']['original_question'] == context['original_question']
    asyncio.run(scenario())


def test_independent_expression_measurements_cannot_be_inner_joined():
    from pankagent_vnext.graph_contract import independent_measurement_steps
    from pankagent_vnext.graph import validate_cypher
    plan = {'steps': [{'id': 's1', 'question': 'Inspect detection and enrichment across cells',
                      'relation_types': ['GENE_ENRICHED_IN', 'GENE_DETECTED_IN'], 'constraints': [], 'complete': True}]}
    split = independent_measurement_steps(plan)
    assert len(split['steps']) == 2
    assert [s['relation_types'] for s in split['steps']] == [['GENE_ENRICHED_IN'], ['GENE_DETECTED_IN']]
    query = 'MATCH (g:Gene)-[e:GENE_ENRICHED_IN]->(c:anatomical_structure), (g)-[d:GENE_DETECTED_IN]->(c) RETURN g,e,d,c'
    assert 'independent_measurements_require_separate_steps' in validate_cypher(query, plan['steps'][0])
    plan['steps'][0]['evidence_combination'] = 'cooccurrence'
    assert len(independent_measurement_steps(plan)['steps']) == 1


def test_followups_only_describe_returned_measurement_types():
    from pankagent_vnext.answer_router import followup_questions
    assert followup_questions({'s': {'status': 'failed', 'edges': [{'type': 'GENE_ENRICHED_IN'}]}}) == []
    questions = followup_questions({'s': {'status': 'complete', 'nodes': [{'labels': ['Gene'], 'properties': {'name': 'INS'}}], 'edges': [{'type': 'GENE_ENRICHED_IN'}]}})
    assert questions and all('INS' in q for q in questions)
    assert not any('colocalization' in q for q in questions)
