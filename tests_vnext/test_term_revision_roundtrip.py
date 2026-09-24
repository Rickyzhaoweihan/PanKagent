"""The deployed failed-plan dialog submits a legacy composite request."""
import asyncio
import pytest
from copy import deepcopy
from tests_vnext.test_runtime import service, Graph, Gateway, wait_state
from pankagent_vnext.terminal_retry import accepted_term_correction, SEPARATOR
from pankagent_vnext.term_clarification import recovery
from tests_vnext.test_term_clarification import V


@pytest.mark.parametrize("manual", [False, True])
def test_apply_suggestion_reaches_planner_with_one_question(tmp_path, manual):
    class Grounded(Graph):
        async def ground_question(self,q):return {'sample_terminology':deepcopy(V)}
    class Capture(Gateway):
        async def interpret_revision(self, question, instruction, parent):
            assert instruction == 'Change only nPAP to HPAP.'
            return {'new_question':question.replace('nPAP','HPAP'), 'execution':'parallel_extend',
                    'reason':'Confirmed source spelling', 'recommended_question':''}
        async def plan(self,question,history):
            self.received=question
            self.plans+=1
            return {'interpreted_question':question,'steps':[], 'clarification':'Do you mean HPAP?' if 'nPAP' in question else 'A separate stage-inventory check is required.'}
    async def scenario():
        gateway=Capture()
        async with service(tmp_path,graph=Grounded(),gateway=gateway) as (client,runtime,*_):
            original='How many T1D stage 2 donors are available in nPAP?'
            created=(await client.post('/v2/plans',json={'question':original,'include_context':False})).json()
            old=await wait_state(client,created['run_id'],{'failed'})
            instruction='Change only nPAP to HPAP.' if manual else old['plan']['recovery']['suggestions'][0]['instruction']
            payload={'question':old['question']+SEPARATOR+instruction,'session_id':old['session_id'],'include_context':False}
            created=(await client.post('/v2/plans',json=payload)).json()
            new=await wait_state(client,created['run_id'],{'failed','awaiting_confirmation'})
            expected=original.replace('nPAP','HPAP')
            assert new['question']==gateway.received==expected
            assert new['plan'].get('recovery',{}).get('category')!='term_clarification'
            audit=runtime.store.audit_metadata(new['run_id'])
            assert audit.get('raw_original_question',audit.get('original_question'))==original
            assert audit['retry_submitted_text']==payload['question']
            assert gateway.plans==2
    asyncio.run(scenario())


def test_existing_repeated_loop_is_recoverable():
    original='How many stage 2 donors in nPAP?'
    corrected=original.replace('nPAP','HPAP')
    polluted=original+(SEPARATOR+'Use this corrected question: '+corrected)*3
    r=recovery(polluted,V,'r')
    old={'question':polluted,'status':'failed','run_id':'r','plan_id':'p','plan':{'recovery':r}}
    submitted=polluted+SEPARATOR+r['suggestions'][0]['instruction']
    assert accepted_term_correction(old,submitted)['question']==corrected
    assert accepted_term_correction(old,submitted+' Also exclude stage 1') is None


def test_different_change_cannot_be_discarded():
    q='Count donors in nPAP?'+SEPARATOR+'Also restrict to female donors.'
    r=recovery(q,V,'r')
    old={'question':q,'status':'failed','run_id':'r','plan_id':'p','plan':{'recovery':r}}
    assert accepted_term_correction(old,q+SEPARATOR+r['suggestions'][0]['instruction']) is None
