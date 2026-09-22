"""Pure and mocked lifecycle checks for incomplete population answers."""
import asyncio
from copy import deepcopy
import pytest

from pankagent_vnext.population_completeness import population_intent, population_recovery
from tests_vnext.test_runtime import Gateway, Graph, new_plan, service, wait_state


def run(question, **plan):
    return {'question': question, 'plan': {'interpreted_question': question, **plan}}


def evidence(status='partial', **fields):
    return {'step_id': 's1', 'purpose': 'primary', 'status': status, 'nodes': [{'id': 'kept'}],
            'edges': [], 'rows': [], 'truncated': True, **fields}


@pytest.mark.parametrize('question', [
    'What proportion of donors have islet samples?', 'What percentage of donors have islet samples?',
    'Calculate the fraction of cells in the requested population.', 'Does a majority of donors have spleen samples?',
    'Which group contains the most GWAS signals?', 'Which group has the largest number of GWAS signals?',
    'Compare percentages of donors across tissues.',
    'Give a cohort overview and calculate the percentage of donors with spleen samples.',
])
def test_incomplete_explicit_population_requests_guarded(question):
    before={'s1':evidence()};original=deepcopy(before)
    issue=population_recovery(run(question),before)
    assert issue['category']=='population_evidence_incomplete'
    assert issue['evidence']['denominator_status']=='not_verified_complete'
    assert len(issue['suggestions'])==2
    assert before==original
    assert 'no data' not in issue['message'].lower()


@pytest.mark.parametrize('question', [
    'Show the top 5 upregulated genes.', 'Show 10 most significant GWAS signals.',
    'Give a comprehensive gene profile of CFTR.',
    'Give a CFTR expression profile, including the fraction of cells expressing it.', 'Explore pathways linked to ADCY3.',
    'What does percentage mean?', 'Define the fraction detected statistic.',
])
def test_ordinary_rankings_profiles_and_definitions_not_guarded(question):
    assert population_recovery(run(question),[evidence()]) is None


def test_original_population_request_cannot_be_dropped_by_root_planner():
    value=run('What fraction of donors has spleen samples?',interpreted_question='Find spleen samples.')
    assert population_recovery(value,[evidence()])


def test_confirmed_revision_controls_current_scope():
    revised=run('Only show the top 5 records.', interpreted_question='Show the top 5 samples.',
        original_question='What proportion of donors has spleen samples?',
        revision_trace={'instruction':'Only show the top 5 records.','original_question':'What proportion of donors has spleen samples?'})
    assert population_recovery(revised,[evidence()]) is None
    added=run('Now give the percentage.',interpreted_question='What percentage of donors has spleen samples?',
        original_question='Find spleen samples.',revision_trace={'instruction':'Now give the percentage.'})
    assert population_recovery(added,[evidence()])


def test_legacy_missing_interpretation_preserves_short_revision_scope_conservatively():
    value=run('Use spleen instead.',interpreted_question='',original_question='What fraction of donors has islet samples?',
        revision_trace={'instruction':'Use spleen instead.'})
    assert population_recovery(value,[evidence()])
    value['plan']['revision_trace']['instruction']='Instead just list records without a percentage.'
    assert population_recovery(value,[evidence()]) is None


@pytest.mark.parametrize('step',[
    evidence('complete',truncated=False,rows=[{'count':0}],nodes=[]),
    evidence('empty',truncated=False,nodes=[]),
    evidence('partial',truncated=False,rows=[{'count':0}],nodes=[]),
    evidence('complete',truncated=False,context_sampled=True,display_omitted=20),
    evidence('partial',truncated=False),
])
def test_no_guard_for_successful_zero_or_nontruncated_primary(step):
    assert population_recovery(run('What fraction of donors has spleen samples?'),[step]) is None


def test_context_failure_or_truncation_does_not_block_complete_primary():
    for ctx in [evidence(purpose='context'),evidence('failed',purpose='context')]:
        assert population_recovery(run('What fraction of donors has spleen samples?'),[
            evidence('complete',truncated=False,rows=[{'count':0}]),ctx]) is None


def test_related_context_alone_cannot_supply_population_answer():
    issue=population_recovery(run('What fraction of donors has spleen samples?'),[evidence('complete',purpose='context',truncated=False)])
    assert issue['evidence']['primary_evidence_missing']
    assert issue['suggestions']==[]


def test_failed_primary_does_not_become_absence_or_unrequested_scope_change():
    issue=population_recovery(run('Which group has most GWAS signals?'),[
        evidence('failed',truncated=False,nodes=[],validation=[{'valid':False,'reasons':['missing_required_filter:tissue']}]),
        evidence('complete',purpose='context',truncated=False)])
    assert issue['evidence']['underlying_category']=='query_validation'
    assert issue['suggestions']==[]
    assert issue['retryable']


def test_operator_failure_does_not_offer_biological_changes():
    issue=population_recovery(run('What percentage of donors has spleen samples?'),[
        evidence(),evidence('failed',step_id='s2',truncated=False,error={'category':'authentication'})])
    assert issue['suggestions']==[]
    assert not issue['retryable']
    assert 'operator' in issue['message']


@pytest.mark.parametrize('question,fail,truncated,expected_calls',[
    ('What fraction of donors has spleen samples?',False,True,0),
    ('Which group has most GWAS signals?',True,False,0),
    ('Show the top 5 most significant GWAS signals.',False,True,0),
    ('What fraction of donors has spleen samples?',False,False,1),
])
def test_guarded_lifecycle_preserves_evidence_and_skips_synthesis_and_literature(tmp_path,question,fail,truncated,expected_calls):
    class FixtureGraph(Graph):
        async def execute(self,step,previous,emit):
            result=await super().execute(step,previous,emit)
            result.update(status='failed' if fail else 'partial' if truncated else 'complete',truncated=truncated)
            if fail:
                result['validation']=[{'valid':False,'reasons':['missing_required_filter:tissue']}]
            return result
    plan={'interpreted_question':question,'steps':[{'id':'s1','question':question,'constraints':[], 'depends_on':[], 'complete':True}], 'literature':True,'clarification':None}
    async def scenario():
        async with service(tmp_path,gateway=Gateway(plan),graph=FixtureGraph()) as (client,runtime,gateway,graph,literature):
            # Current lifecycle validates every primary query before confirmation.
            # Truncated or failed retrieval is blocked before any population answer.
            value=await new_plan(client, question, expected_status='failed' if fail or truncated else 'awaiting_confirmation')
            response=await client.post('/v2/plans/'+value['plan_id']+'/confirm')
            assert response.status_code == (409 if fail or truncated else 202)
            final=await wait_state(client,value['run_id'],{'completed','partial','failed'})
            assert gateway.syntheses==expected_calls
            data=final['preview']['evidence'] if fail or truncated else final['evidence']
            assert data['nodes'][0]['id']=='INS'
            if not expected_calls:
                assert final['status']=='failed'
                assert final['preview']['confirmation_eligible'] is False
                assert final['graph_answer'] is None
                assert final['error']['recovery']['retryable']
                assert literature.calls==0
                assert graph.calls==1
                again=(await client.get('/v2/runs/'+value['run_id'])).json()
                assert again['error']==final['error']
                assert gateway.syntheses==0
    asyncio.run(scenario())


@pytest.mark.parametrize('question', [
    'Show genes detected in more than 10 percent of beta cells.',
    'Find genes with pct_cells_expressing above 10 percent.',
    'Show genes with recorded detection percentage greater than 10.',
    'Which variants have PIP greater than 50 percent?',
    'Show associations with p-value below one percent.',
    'Show p-values as percentages.',
    'Read the existing percentage of beta cells expressing CFTR.',
    'What percentage of beta cells expressing CFTR is recorded?',
    'What is the reported detection percentage for CFTR?',
    'Compare recorded detection percentages for CFTR between cell types.',
    'Report the fraction of expressing cells already recorded for CFTR.',
])
def test_existing_percentage_statistics_and_filters_do_not_require_new_denominator(question):
    assert population_intent(run(question)) is None
    assert population_recovery(run(question),[evidence()]) is None


@pytest.mark.parametrize('question', [
    'Explain PIP and calculate the fraction of donors with spleen samples.',
    'Define PPH4, then compare percentages of donors across tissues.',
    'Explain the existing detection percentage and determine what fraction of donors has samples.',
    'Explain PIP; which group has most GWAS signals?',
])
def test_mixed_definition_and_calculation_cannot_bypass_guard(question):
    assert population_recovery(run(question),[evidence()])
