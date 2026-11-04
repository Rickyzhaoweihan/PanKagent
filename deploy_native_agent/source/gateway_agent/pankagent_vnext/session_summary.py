"""Explicit summaries reuse recorded session evidence without new searches."""
import re
from .answer_blocks import catalogue
from .evidence_status import synthesis_evidence

VERSION = 'session-evidence-summary-v2'


def requested(question):
    return bool(re.fullmatch(r'(?:please\s+)?(?:summari[sz]e\s+(?:that|this|the above|these results|what (?:you|we) (?:have )?found)|give (?:me )?a summary of (?:that|this|the above))(?:\s+in\s+(?:one|a single|\d+)\s+sentences?)?[.!]?', question.strip(), re.I))


def plan(question, prior):
    if not prior or not requested(question): return None
    result={'interpreted_question':question, 'steps':[], 'clarification':None,
            'plan_mode':'session_summary', 'session_summary':{'version':VERSION,'source_run_id':prior['run_id']},
            'literature':False, 'planning_route':{'kind':'session_summary','claude_calls':0}}
    scope=(prior.get('plan') or {}).get('output_scope')
    if scope: result['output_scope']=scope
    return result


def answer(prior):
    from .output_scope import enabled, project
    if enabled(prior): prior=project(prior)
    steps=((prior.get('evidence') or {}).get('steps') or [])
    facts=catalogue(synthesis_evidence({s.get('step_id',str(i)):s for i,s in enumerate(steps)}))
    details=[f for f in facts if f['kind'] in {'relationship','comparison','cohort','contributors','source_classification'}]
    if not details:
        details=[f for f in facts if f['mandatory']]
    if not details:
        return 'The previous result has no graph evidence available to summarize; its saved literature and status remain available in the earlier result.'
    selected=details[:3]
    # Keep sentence boundaries out of the fragments while preserving decimal
    # values and source URLs; the returned summary is one compound sentence.
    fragments=[re.sub(r'[.!?](?=\s|$)', ';', f['text'].rstrip('.')) + ' ['+f['evidence_id']+']' for f in selected]
    prefix='From the earlier recorded evidence'
    if prior.get('status')!='completed': prefix+=' (partial result)'
    return prefix + ': ' + '; '.join(fragments) + '; no new search was performed.'
