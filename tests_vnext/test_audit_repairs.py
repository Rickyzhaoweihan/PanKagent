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
                'question': 'Change the disease filter to T2D', 'revision_instruction': 'Change the disease filter to T2D', 'revision_mode': 'instruction'})
            revised = await wait_state(client, response.json()['run_id'], {'awaiting_confirmation'})
            context = gateway.history[-1]['revision_context']
            assert context['original_question'] == 'Which cell types express INS?'
            assert context['parent_plan']['steps']
            assert context['instruction'] == 'Change the disease filter to T2D'
            assert revised['plan']['literature'] is True
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


def test_scope_guard_does_not_stream_a_known_false_query_scope():
    from pankagent_vnext.scope_guard import ScopeTextFilter, broad_cell_search
    evidence = {'s': {'status': 'complete', 'queries': [{'cypher': 'validated'}], 'requested_scope': {
        'complete': True, 'relation_types': ['GENE_ENRICHED_IN'],
        'constraints': [{'entity_type': 'Gene', 'property': 'name', 'value': 'INS'}]}}}
    assert broad_cell_search(evidence)
    guard = ScopeTextFilter(evidence)
    chunks = ['Measured value 1.23 [G1].\n\n', 'No other cell types were ', 'queried here.\n\n', 'Inspect the source.']
    output = ''.join(guard.feed(chunk) for chunk in chunks) + guard.feed('', final=True)
    assert 'Measured value 1.23 [G1].' in output
    assert 'No other cell types' not in output
    assert 'all matching cell types' in output and guard.corrections == 1
    evidence['s']['status'] = 'partial'
    assert not broad_cell_search(evidence)
    assert ScopeTextFilter({}).feed('No other cell types were queried.') == 'No other cell types were queried.'


def test_revision_context_preserves_filters_and_preview_identities_without_internal_duplication():
    from pankagent_vnext.revision_context import parent_context
    parent = {'plan': {'steps': [{'id': 's1', 'constraints': [{'property': 'condition', 'value': 'T2D'}],
        'resolution_key': 'internal-signature', 'resolved_entities': [{'internal': 'details'}]}],
        'revision_trace': {'before_steps': ['redundant-history']}},
        'preview': {'status': 'complete', 'evidence': {'nodes': [{'id': 'cell'+str(i), 'labels': ['anatomical_structure'], 'properties': {'name': 'Cell '+str(i)}} for i in range(41)]}}}
    plan, preview = parent_context(parent)
    assert plan['steps'][0]['constraints'] == parent['plan']['steps'][0]['constraints']
    assert 'resolution_key' not in plan['steps'][0] and 'revision_trace' not in plan
    assert preview['entities'][0]['id'] == 'cell0' and preview['entity_list_sampled']
    assert preview['node_count'] == 41 and len(preview['entities']) == 40


def test_release_go_domain_mapping_preserves_filter_and_owner():
    from pankagent_vnext.plan_constraints import repair_step_constraints
    from pankagent_vnext.graph import validate_cypher
    step = repair_step_constraints({'question':'biological process annotations', 'relation_types':['ASSOCIATED_WITH_GO'], 'constraints':[{'property':'namespace','operator':'=','value':'biological_process','entity_type':'GO_term'}]})
    assert step['constraints'][0]['property'] == 'go_domain'
    assert step['schema_bindings'][0]['from']['property'] == 'namespace'
    query="MATCH (g:Gene)-[r:ASSOCIATED_WITH_GO]->(t:GO_term) WHERE t.go_domain = 'biological_process' RETURN g,r,t"
    assert validate_cypher(query, step) == []
    assert 'missing_required_filter:go_domain' in validate_cypher(query.replace('t.go_domain','g.go_domain'), step)


def test_t1d_context_maps_to_required_relation_without_dropping_other_diseases():
    from pankagent_vnext.plan_constraints import repair_step_constraints
    from pankagent_vnext.graph import validate_cypher
    original={'question':'T1D differential expression', 'relation_types':['T1D_DEG_IN'], 'constraints':[{'property':'id','entity_type':'disease','operator':'=','value':'MONDO_0005147'}]}
    step=repair_step_constraints(original)
    assert step['constraints'] == []
    assert step['schema_bindings'][0]['from'] == original['constraints'][0]
    assert 'missing_required_relation:T1D_DEG_IN' in validate_cypher('MATCH (g:Gene) RETURN g',step)
    assert 'measurement_endpoint_schema_mismatch' in validate_cypher('MATCH (g:Gene)-[r:T1D_DEG_IN]->(d:disease) RETURN g,r,d',step)
    assert validate_cypher('MATCH (g:Gene)-[r:T1D_DEG_IN]->(c:anatomical_structure) RETURN g,r,c',step) == []
    original['constraints'][0]['value']='MONDO_0005148'
    assert repair_step_constraints(original)['constraints'] == original['constraints']


def test_measurement_endpoint_check_allows_undirected_gene_cell_match():
    from pankagent_vnext.graph import validate_cypher
    assert validate_cypher('MATCH (g:Gene)-[r:T1D_DEG_IN]-(c:anatomical_structure) RETURN g,r,c', {'relation_types':['T1D_DEG_IN']}) == []


def test_repair_timing_excludes_user_confirmation_wait():
    from ux_audit.summarize_repairs import stage_times
    events=[{'type':'progress','status':'planning','elapsed_ms':1}, {'type':'plan_ready','elapsed_ms':10000}, {'type':'progress','status':'queued','elapsed_ms':90000}, {'type':'graph_answer','elapsed_ms':93000}, {'type':'graph_answer','elapsed_ms':95000}]
    result=stage_times(events)
    assert result['plan_ready_s']==10
    assert result['graph_last_chunk_after_confirmation_s']==5
    assert result['graph_first_chunk_after_confirmation_s']==3


def test_model_plan_structure_reports_specific_issue():
    from pankagent_vnext.llm import plan_structure_issue
    assert plan_structure_issue({'steps':[{'id':'a','depends_on':['b']}]}) == 'invalid_plan_dependencies'
    assert plan_structure_issue({'steps':[{'id':str(i),'depends_on':[]} for i in range(4)]}) == 'plan_too_large'
    assert plan_structure_issue({'steps':[{'id':'a','depends_on':[]},{'id':'b','depends_on':['a']}]}) is None


def test_additive_revision_does_not_silently_replace_old_investigations():
    from pankagent_vnext.revision_guard import preserve_additive_scope
    parent={'steps':[{'id':'a','relation_types':['FUNCTION_ANNOTATION'],'constraints':[{'property':'name','operator':'=','value':'HLA-DRA'}]}]}
    changed={'steps':[{'id':'b','relation_types':['PART_OF_GWAS_SIGNAL'],'constraints':[{'property':'name','operator':'=','value':'HLA-DRA'}]}]}
    guarded=preserve_additive_scope(changed,parent,'add in human genetics data')
    assert guarded['steps']==parent['steps'] and guarded['clarification']
    assert guarded['proposal_issue']=='additive_revision_scope_loss'
    repeated=preserve_additive_scope(changed,guarded,'did you add the requested evidence?')
    assert repeated['steps']==parent['steps'] and repeated['clarification']
    assert preserve_additive_scope(changed,parent,'replace pathways with human genetics')==changed


def test_additive_scope_guard_preserves_verified_name_id_equivalence():
    from pankagent_vnext.revision_guard import preserve_additive_scope
    parent={'steps':[{'id':'a','relation_types':['FUNCTION_ANNOTATION'],'constraints':[{'property':'name','value':'TEST','operator':'='}], 'resolved_entities':[{'state':'resolved','id':'ID1','name':'TEST','entity_type':'Gene'}]}]}
    changed={'steps':[{'id':'a','relation_types':['FUNCTION_ANNOTATION'],'constraints':[{'property':'id','value':'ID1','operator':'='}]}]}
    assert preserve_additive_scope(changed,parent,'also check genetics')==changed


def test_additive_scope_failure_retains_preview_and_blocks_confirmation(tmp_path):
    class DroppingGateway(Gateway):
        async def plan(self,question,history):
            if question.startswith('add'):
                return {**copy.deepcopy(PLAN),'steps':[{'id':'different','question':'new scope','relation_types':['PHYSICAL_INTERACTION'],'constraints':[],'depends_on':[]}]}
            p=copy.deepcopy(PLAN);p['steps'][0]['relation_types']=['GENE_ENRICHED_IN'];return p
    async def scenario():
        async with service(tmp_path,gateway=DroppingGateway()) as (client,runtime,*_):
            created=await new_plan(client,'Is INS enriched in beta cells?')
            parent=runtime.store.get(created['run_id'])
            res=await client.post(f'/v2/plans/{created["plan_id"]}/revise',json={'question':'add genetics','revision_instruction':'add genetics','revision_mode':'instruction'})
            child=await wait_state(client,res.json()['run_id'],{'awaiting_confirmation'})
            assert child['plan']['proposal_issue']=='additive_revision_scope_loss'
            assert child['preview']['evidence']['nodes']==parent['preview']['evidence']['nodes']
            assert (await client.post(f'/v2/plans/{child["plan_id"]}/confirm',json={})).status_code==409
    asyncio.run(scenario())
