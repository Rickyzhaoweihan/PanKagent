"""Known quick explanations never authorize new upstream literature calls."""
import asyncio
import pytest

from pankagent_vnext.definition_intent import definition_only_question, definition_only_plan
from pankagent_vnext.literature_gate import literature_gate
from tests_vnext.test_runtime import Gateway, service, wait_state, new_plan


@pytest.mark.parametrize('question', [
    'What does GENE_DETECTED_IN mean?', 'Explain the GENE_DETECTED_IN relation for ADCY3.',
    'What is PIP?', 'What does PP.H4 mean?', 'Explain PPH4=0.95 in my result.',
    'Define posterior inclusion probability.', 'How should I interpret NES?',
    'What is a credible set in fine mapping?', 'Compare PIP and PPH4.',
    'Explain one-vs-rest.', 'What does IBA mean?',
])
def test_known_definition_only(question):
    assert definition_only_question(question)
    plan={'literature':True,'steps':[{'id':'legacy_unnecessary_kg'}], 'original_question':question,'interpreted_question':'Find records for ADCY3.'}
    assert literature_gate(plan,{'steps':[{'status':'complete','nodes':[{'id':'ADCY3'}]}]},'Existing answer')==(False,'no_new_graph_requested')


@pytest.mark.parametrize('question', [
    'Explain PIP and find variants for GLIS3.',
    'Explain PIP and calculate the fraction of variants above 0.5.',
    'What does PPH4 mean? Which variants are in this locus?',
    'Define NES then retrieve enriched pathways.',
    'Explain PIP and what is CFTR expression in beta cells?',
    'What is the PIP for rs123?', 'What is a myeloid cell?', 'What causes T1D?',
    'Compare PIP values for these two variants.',
    'Explain this result.',
])
def test_mixed_or_unknown_question_remains_normal_gate(question):
    assert not definition_only_question(question)
    plan={'literature':True,'steps':[{'id':'s1'}],'original_question':question}
    assert literature_gate(plan,{'steps':[{'status':'complete','nodes':[{'id':'kept'}]}]},'Answer')[0]


def test_revised_intent_not_stale_original():
    plan={'original_question':'What is PIP?', 'interpreted_question':'Find variants for CFTR.',
          'revision_trace':{'instruction':'Now find variants for CFTR.'}}
    assert not definition_only_plan(plan)
    plan={'original_question':'Find variants for CFTR.', 'interpreted_question':'Explain PIP.',
          'revision_trace':{'instruction':'Instead just explain PIP.'}}
    assert definition_only_plan(plan)
    plan['revision_trace']['instruction']='Also find variants for CFTR.'
    assert not definition_only_plan(plan)


def test_existing_no_graph_and_failed_status_gates_preserved():
    for evidence in [{'steps':[]},{'steps':[{'status':'failed','nodes':[{'id':'bad'}]}]},
                     {'steps':[{'status':'complete','rows':[{'count':0}]}]}]:
        assert not literature_gate({'literature':True,'steps':[{'id':'s1'}]},evidence,'Answer')[0]
    assert literature_gate({'literature':False,'original_question':'What is PIP?'},{},'Answer')==(False,'not_requested')


def test_legacy_definition_plan_executes_no_literature(tmp_path):
    async def scenario():
        question='What does GENE_DETECTED_IN mean?'
        plan={'original_question':question,'interpreted_question':'Find records for ADCY3.',
              'literature':True,'steps':[{'id':'s1','question':'Find records for ADCY3.','complete':True,'depends_on':[],'constraints':[]}], 'clarification':None}
        async with service(tmp_path,gateway=Gateway(plan)) as (client,runtime,gateway,graph,literature):
            value=await new_plan(client, question)
            assert (await client.post('/v2/plans/'+value['plan_id']+'/confirm')).status_code == 202
            final=await wait_state(client,value['run_id'],{'completed','partial','failed'})
            assert final['status']=='completed'
            assert graph.calls==1 # This gate does not pretend to implement a skill-only route.
            assert gateway.syntheses==1
            assert literature.calls==0
            assert final['literature']['reason']=='no_new_graph_requested'
    asyncio.run(scenario())
