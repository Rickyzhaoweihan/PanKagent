"""Retain Claude's scope choice separately from Python's lexical assessment."""
from copy import deepcopy
import hashlib
import json
import re


PHRASE_ROLE_SCHEMA = {'type': 'array', 'items': {
    'type': 'object', 'additionalProperties': False,
    'properties': {'text': {'type': 'string'},
                   'role': {'type': 'string', 'enum': ['anchor', 'filter', 'example', 'background', 'output']},
                   'occurrence_index': {'type': 'integer'}},
    'required': ['text', 'role']}}


def phrase_roles(question, proposals):
    """Resolve model-selected literal phrases to server-owned request spans."""
    if not isinstance(proposals, list) or len(proposals) > 40:
        raise ValueError('invalid_request_phrase_roles')
    result = []
    for item in proposals:
        if (not isinstance(item, dict) or set(item) - {'text', 'role', 'occurrence_index'}
                or not isinstance(item.get('text'), str) or not item['text'].strip()
                or item.get('role') not in {'anchor', 'filter', 'example', 'background', 'output'}):
            raise ValueError('invalid_request_phrase_role')
        matches = list(re.finditer(re.escape(item['text']), question))
        index = item.get('occurrence_index')
        if index is None:
            if len(matches) != 1:
                raise ValueError('ambiguous_request_phrase_occurrence')
            index = 0
        if type(index) is not int or not 0 <= index < len(matches):
            raise ValueError('invalid_request_phrase_occurrence')
        match = matches[index]
        if any(match.start() < old['end'] and old['start'] < match.end() for old in result):
            raise ValueError('overlapping_request_phrase_roles')
        if item['role'] == 'example':
            # Do not erase an explicit restriction by relabeling its operand.
            # Illustrative markers reset this immediate syntactic position.
            prefix = question[max(0, match.start()-80):match.start()]
            if (re.search(r'\b(?:only|excluding|exclude|except|without|not|from|within|with|in)[\s:=]+$', prefix, re.I)
                    or re.search(r'\b(?:only|excluding|exclude|except|without|not|from|within|with|in)\b|[<>]=?|!=', item['text'], re.I)):
                raise ValueError('explicit_filter_cannot_be_nonrestrictive')
            # Model interpretation may cancel illustrative wording, not an
            # arbitrary membership operand. A cue is required as supporting
            # request evidence, including when the model copies the cue itself.
            context = prefix + item['text']
            cues = list(re.finditer(r'\b(?:like|such\s+as|for\s+example|for\s+instance|as\s+an?\s+example)\b|\be\.g\.|[,(:]\s*say\s*,?\s+', context, re.I))
            if not cues:
                raise ValueError('example_role_requires_illustrative_context')
            cue = cues[-1]
            # A previous sentence's example marker cannot excuse a current
            # filter; nor can a restriction be hidden behind an example cue.
            after_cue = context[cue.end():]
            # A copied trailing ellipsis belongs to the illustrative phrase.
            # Interior punctuation still ends its scope, so the same cue cannot
            # mask a later sentence or an actual constraint after the ellipsis.
            after_cue = re.sub(r'(?:\.{2,}|…)(?=\s*[)\]]*\s*$)', '', after_cue)
            before_cue = re.split(r'[.!?;,()]', context[:cue.start()])[-1]
            suffix = question[match.end():match.end() + 80]
            if (re.search(r'[.!?;]', after_cue)
                    or re.search(r'\b(?:only|excluding|exclude|except|without|not)\b', before_cue, re.I)
                    or re.match(r'\s+(?:[A-Za-z-]+\s+){0,2}only\b', suffix, re.I)):
                raise ValueError('explicit_filter_cannot_be_nonrestrictive')
        result.append({'text': item['text'], 'role': item['role'], 'start': match.start(), 'end': match.end()})
    return result


def _valid_decision(step, question):
    decision = step.get('model_scope_decision') or {}
    return (decision if decision.get('source') == 'claude_record_plan'
            and decision.get('step_id') == step.get('id')
            and decision.get('request_sha256') == hashlib.sha256(question.encode()).hexdigest() else {})


def scope_text(step, question):
    """Mask only retained nonrestrictive spans; never alter raw request storage."""
    roles = _valid_decision(step, question).get('request_phrase_roles', [])
    text = question
    for item in roles:
        a, b = item['start'], item['end']
        if item['role'] == 'example' and question[a:b] == item['text']:
            text = text[:a] + ' ' * (b-a) + text[b:]
    return text


def effective_scope(question, plan_or_step):
    """Compiler view only; background context remains available to planning."""
    if 'steps' not in plan_or_step:
        return scope_text(plan_or_step, question)
    steps = plan_or_step.get('steps') or []
    texts = [scope_text(step, question) for step in steps if _valid_decision(step, question)]
    if not texts:
        return question
    if len(set(texts)) != 1:
        raise ValueError('inconsistent_request_phrase_roles')
    return texts[0]


def _literal_value(text, value):
    """Allow spelling separators, never substrings of another identifier/value."""
    if not isinstance(value, str) or not value.strip():
        return False
    words = re.findall(r'[A-Za-z0-9]+', value)
    if not words:
        return False
    pattern = r'(?<![A-Za-z0-9])' + r'[\W_]*'.join(map(re.escape, words)) + r'(?![A-Za-z0-9])'
    return bool(re.search(pattern, text, re.I))


def _constraint_values(constraint):
    values = constraint.get('value')
    if str(constraint.get('operator', '=')).upper() in {'IN', 'NOT IN'} and isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            return []
    return values if isinstance(values, list) else [values]


def _independent_binding(step, constraint, active):
    """Retain aliases traced to real request wording by existing preparation."""
    values = _constraint_values(constraint)
    if values and all(_literal_value(active, value) for value in values):
        return True
    if str(constraint.get('operator', '=')).upper() == '=':
        for proof in step.get('request_phrase_identity_facts', []):
            if (proof.get('entity_type') == constraint.get('entity_type')
                    and constraint.get('property') in {'id', 'name'}
                    and proof.get(constraint['property']) == constraint.get('value')
                    and _literal_value(active, proof.get('mention'))):
                return True
        for proof in step.get('preparation_trace', []):
            if (proof.get('rule') == 'unique_requested_identity_binding'
                    and proof.get('entity_type') == constraint.get('entity_type')
                    and constraint.get('property') == 'id'
                    and proof.get('id') == constraint.get('value')
                    and _literal_value(active, proof.get('mention'))):
                return True
    for change in step.get('constraint_compilation', []):
        if change.get('canonical_binding') != constraint:
            continue
        original = _constraint_values(change.get('requested') or {})
        if original and all(_literal_value(active, value) for value in original):
            return True
    # These records already bind canonical predicates to reviewed request terms
    # and release-specific rules. Revalidate against the example-masked text.
    if step.get('requested_scope_compilation'):
        from .semantic_registry import _verified_requested_scope_derivation
        if _verified_requested_scope_derivation(constraint, step, active):
            return True
    return False


def excluded_constraint(step, constraint, question):
    """An example-only literal cannot become an executable predicate."""
    roles = _valid_decision(step, question).get('request_phrase_roles', [])
    values = _constraint_values(constraint)
    active = scope_text(step, question)
    excluded = [r['text'] for r in roles if r['role'] == 'example']
    if (not values or not all(any(_literal_value(text, value) for text in excluded)
                             for value in values)
            or _independent_binding(step, constraint, active)):
        return False
    # A canonical alias can differ completely from its real filter/anchor.
    # Do not guess which phrase owns it when existing facts cannot disambiguate.
    # Other predicates/facts may account for unrelated real roles (e.g. tissue
    # plus an illustrative assay), which still permits example-only removal.
    for role in roles:
        if role['role'] not in {'anchor', 'filter'}:
            continue
        if not any(_independent_binding(step, other, role['text'])
                   for other in step.get('constraints', []) if other != constraint):
            raise ValueError('ambiguous_example_constraint_ownership')
    return True


def apply_phrase_roles(step, question):
    result = deepcopy(step)
    kept, removed = [], []
    for constraint in result.get('constraints', []):
        (removed if excluded_constraint(result, constraint, question) else kept).append(constraint)
    result['constraints'] = kept
    if removed:
        result.setdefault('request_scope_changes', []).append({'rule': 'nonrestrictive_request_phrases',
            'removed_constraints': removed, 'request_sha256': hashlib.sha256(question.encode()).hexdigest()})
    return result


def record(step, question, roles=None):
    return {'source': 'claude_record_plan', 'request_sha256': hashlib.sha256(question.encode()).hexdigest(),
            'step_id': step['id'], 'constraints': deepcopy(step.get('constraints', [])),
            'request_phrase_roles': deepcopy(roles or [])}


def covers(step, index, constraint, question):
    if excluded_constraint(step, constraint, question):
        return False
    decision = step.get('model_scope_decision') or {}
    if (decision.get('source') != 'claude_record_plan'
            or decision.get('step_id') != step.get('id')
            or decision.get('request_sha256') != hashlib.sha256(question.encode()).hexdigest()):
        return False
    # A preparation canonicalization may add owner metadata. Its recorded input
    # must still be an actual model-selected predicate, not new helper scope.
    originals = [constraint] + [change.get('requested') for change in step.get('constraint_compilation', [])
        if change.get('constraint_index') == index and change.get('canonical_binding') == constraint]
    def same_predicate(selected, prepared):
        if not isinstance(selected, dict) or not isinstance(prepared, dict):
            return False
        defaults = {'operator': '=', 'entity_type': None}
        if any(selected.get(k, defaults.get(k)) != prepared.get(k, defaults.get(k))
               for k in ('property', 'operator', 'value', 'entity_type')):
            return False
        # A verified owner normalizer may fill absent metadata without changing
        # the selected predicate. It may not replace an owner Claude specified.
        return all(k not in selected or selected[k] == prepared.get(k)
                   for k in ('owner_role', 'owner_kind', 'relationship_type'))
    return any(same_predicate(selected, original)
               for selected in decision.get('constraints', []) for original in originals)
