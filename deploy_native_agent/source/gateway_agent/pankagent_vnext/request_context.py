"""Server-owned wording shared by concurrent planning and answer calls."""
from contextvars import ContextVar
from copy import deepcopy

_current = ContextVar('pankagent_request_context', default=None)
FIELDS = ('original_raw_question', 'current_raw_input', 'revision_instruction', 'effective_question')
AUTHORITY = ('Original and current raw inputs are user wording. effective_question is a derived interpretation. '
             'The latest explicit revision supersedes removed requirements; do not restore them from the initial question. '
             'Python assessments, drafts, suggested paths and interpretations are advisory. Discard irrelevant suggestions. '
             'Verified database facts establish what exists, not what the user requested. '
             'Keep requested conditions, factual execution checks and evidence limitations.')


def from_run(run, metadata):
    metadata = metadata or {}
    run = run or {}
    instruction = metadata.get('revision_instruction')
    original = metadata.get('original_raw_question', metadata.get('raw_original_question', metadata.get('original_question')))
    raw = metadata.get('current_raw_input')
    if raw is None:
        raw = instruction if metadata.get('revision_mode') == 'instruction' else metadata.get('submitted_question')
    if raw is None and not metadata.get('parent_run_id'):
        raw = original
    return {'original_raw_question': original, 'current_raw_input': raw,
            'revision_instruction': instruction,
            'effective_question': metadata.get('effective_question') or ((run.get('plan') or {}).get('request_context') or {}).get('effective_question') or run.get('question'),
            'raw_status': 'available' if original is not None and raw is not None else 'unavailable_historical'}


def bind(context):
    return _current.set(deepcopy(context))


def reset(token):
    _current.reset(token)


def set_effective(question):
    context = current(question)
    context['effective_question'] = question
    _current.set(context)


def current(question=None, stored=None):
    context = deepcopy(_current.get() or stored or {})
    if not context:
        context = dict.fromkeys(FIELDS)
        context['raw_status'] = 'unavailable_historical'
    context.setdefault('effective_question', question)
    if context['effective_question'] is None:
        context['effective_question'] = question
    return context


def attach(plan, context):
    """Overwrite model-supplied metadata; only the server supplies raw wording."""
    plan = deepcopy(plan)
    plan['request_context'] = deepcopy(context)
    for step in plan.get('steps', []):
        step['request_context'] = deepcopy(context)
    return plan
