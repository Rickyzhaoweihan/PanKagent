"""Prevent population conclusions from incomplete primary retrieval.

This is a synthesis gate, not a denominator resolver. It makes no query, changes
no filters, and does not declare a requested analysis unsupported by the graph.
"""
import re

VERSION = 'population-completeness-2'

_FRACTION = r'(?:proportions?|percentages?|percent|fractions?)'
_CALCULATION = re.compile(r'\b(?:calculate|compute|estimate|determine)\b[^.;!?]{0,90}\b' + _FRACTION + r'\s+(?:of|among|across|within)\b', re.I)
_FRACTION_QUESTION = re.compile(r'\b(?:what|which)\s+(?:(?:is|are)\s+)?(?:the\s+)?' + _FRACTION + r'\s+(?:of|among)\b|\b(?:compare|report|give|show|find)\s+(?:me\s+)?(?:the\s+)?' + _FRACTION + r'\s+(?:of|among|across|within)\b', re.I)
_MAJORITY_QUESTION = re.compile(r'\b(?:is|are|do|does|did|has|have)\s+(?:a\s+|the\s+)?majority\b|\b(?:determine|calculate|check|test)\b[^.;!?]{0,50}\bmajority\b', re.I)
_EXISTING_STATISTIC = re.compile(r'\b(?:recorded|reported|stored|existing)\s+(?:detection\s+|expression\s+)?' + _FRACTION + r'\b|\b' + _FRACTION + r'\b[^.;!?]{0,100}\b(?:recorded|reported|stored)\b', re.I)
_SIGNALS = re.compile(r'\b(?:most\s+(?:\w+\s+){0,4}signals|(?:largest|highest|greatest)\s+number\s+of\s+(?:\w+\s+){0,3}signals)\b', re.I)
_QUALITY_RANK = re.compile(r'\bmost\s+(?:statistically\s+)?(?:significant|likely|strongly|promising|relevant|important)\b', re.I)
_DEFINITION = re.compile(r'^\s*(?:please\s+)?(?:explain|define|what\s+(?:does|do)\b.*\bmean|what\s+is\s+(?:a|the)\s+(?:proportion|percentage|fraction)\b)', re.I)
_ACTION_SPLIT = re.compile(r'[;.!?]|(?:,|\b(?:and|but|then)\b)(?=\s+(?:please\s+)?(?:calculate|compute|estimate|determine|compare|report|give|show|find|which|what|is|are|do|does|check|test)\b)', re.I)
_NEGATED_ACTION = re.compile(r'^\s*(?:please\s+)?(?:do\s+not|don.t|without|skip|no\s+need\s+to)\b', re.I)
_SCOPE_REMOVAL = re.compile(r'\b(?:instead|only|just)\b.{0,45}\b(?:list|show|inspect|top\s*\d+)\b|\b(?:do\s+not|don.t|stop|skip|without|no)\b.{0,30}\b(?:percentage|proportion|fraction|majority|population|ranking|rank)\b', re.I)


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def population_intent(run):
    """Use the confirmed standalone revised question, never stale original scope."""
    plan = run.get('plan') if isinstance(run.get('plan'), dict) else {}
    interpreted = _text(plan.get('interpreted_question'))
    submitted = _text(run.get('question'))
    revision = plan.get('revision_trace') if isinstance(plan.get('revision_trace'), dict) else {}
    # The current interpreted question is the execution/synthesis contract. A
    # root question is also checked so a planner cannot simply omit its scope.
    texts = [interpreted]
    if not revision:
        texts.append(submitted or _text(plan.get('original_question')))
    elif not interpreted:
        instruction = _text(revision.get('instruction')) or submitted
        if not _SCOPE_REMOVAL.search(instruction):
            texts.append(instruction)
            texts.append(_text(revision.get('original_question')) or _text(plan.get('original_question')))
    for text in texts:
        for clause in _ACTION_SPLIT.split(text):
            if not clause.strip() or _NEGATED_ACTION.search(clause):
                continue
            # A calculation can coexist with a definition in the same request.
            # Bare percentage fields, thresholds, PIP and p-values do not ask
            # for a newly calculated population denominator.
            if _CALCULATION.search(clause):
                return 'population_fraction'
            if _DEFINITION.search(clause):
                continue
            if ((_FRACTION_QUESTION.search(clause) or _MAJORITY_QUESTION.search(clause))
                    and not _EXISTING_STATISTIC.search(clause)):
                return 'population_fraction'
            if _SIGNALS.search(clause) and not _QUALITY_RANK.search(clause):
                return 'signal_frequency_ranking'
    return None


def _steps(value):
    if isinstance(value, dict):
        value = list(value.values())
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def population_recovery(run, previous):
    """Return recovery only for explicit population intent and real missing scope."""
    intent = population_intent(run)
    if not intent:
        return None
    steps = _steps(previous)
    primary = [step for step in steps if step.get('purpose') != 'context']
    failed = [step for step in primary if step.get('status') in {'failed', 'unavailable', 'blocked'}]
    truncated = [step for step in primary if step.get('truncated') is True]
    context_only = bool(steps) and not primary
    if not failed and not truncated and not context_only:
        # Includes valid scalar zero, empty primaries, deliberately bounded
        # top-N records, model-context reduction, and context-only truncation.
        return None
    from .query_recovery import retrieval_recovery
    underlying = retrieval_recovery({'status': 'failed', 'preparation_complete': True,
                                      'evidence': {'steps': failed}}) if failed else None
    operator_problem = underlying and underlying['category'] in {
        'graph_release_mismatch', 'budget_exhausted', 'authentication', 'billing'}
    result_phrase = ('a reliable percentage or majority for the requested population'
                     if intent == 'population_fraction' else 'which group has the most independent signals')
    reasons = []
    if failed:
        reasons.append('one or more required evidence checks did not finish successfully')
    if truncated:
        reasons.append('some required records were cut off by the retrieval limit')
    if context_only:
        reasons.append('only related context was retrieved, rather than the primary evidence this comparison needs')
    message = (f'I cannot determine {result_phrase} because ' + ' and '.join(reasons) + '. '
               'The retrieved evidence is preserved below, but it does not establish a complete population result. '
               'This incomplete search does not show that matching records are absent.')
    if underlying:
        message += '\n\n' + underlying['message']
    elif truncated:
        message += '\n\nYou can explicitly narrow the comparison or inspect the available records without a population claim.'
    suggestions = []
    if truncated and not operator_problem:
        suggestions = [
            {'label': 'Choose a narrower comparison',
             'instruction': 'Help me choose a narrower population or one recorded study for this comparison. Keep all existing filters unless I explicitly change them, and do not calculate the comparison until all required records are available.'},
            {'label': 'Inspect available records only',
             'instruction': 'Show the available records for my current filters without calculating a population percentage, majority, or ranking. Label the evidence as incomplete and keep all other requirements.'},
        ]
    return {'category': 'population_evidence_incomplete', 'title': 'The evidence is incomplete for this comparison',
            'message': message, 'retryable': underlying['retryable'] if underlying else not context_only,
            'suggestions': suggestions,
            'evidence': {'policy_version': VERSION, 'requested_comparison': intent,
                         'denominator_status': 'not_verified_complete',
                         'failed_primary_step_ids': [step.get('step_id') for step in failed],
                         'truncated_primary_step_ids': [step.get('step_id') for step in truncated],
                         'primary_evidence_missing': context_only,
                         'underlying_category': underlying['category'] if underlying else None}}
