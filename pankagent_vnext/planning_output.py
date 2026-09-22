"""Lossless recovery of one observed tool-argument encoding defect.

This is syntax recovery, never entity/scope validation. The recovered object
must still pass plan compilation, scope validation and actual query checks.
"""
from copy import deepcopy
import hashlib
import json
import re

VERSION = 'tool-argument-recovery-v1'


def matches_schema(value, schema):
    """Validate the small closed JSON-schema vocabulary used by PLAN_SCHEMA.

    Unknown schema keywords fail closed so this cannot quietly become a weaker
    validator after the public contract changes. No new dependency is needed.
    """
    if set(schema) - {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum'}:
        return False
    kinds = schema.get('type')
    kinds = kinds if isinstance(kinds, list) else [kinds]
    actual = ('null' if value is None else 'boolean' if isinstance(value, bool) else
              'object' if isinstance(value, dict) else 'array' if isinstance(value, list) else
              'string' if isinstance(value, str) else 'number' if isinstance(value, (int, float)) else None)
    if actual not in kinds or ('enum' in schema and value not in schema['enum']):
        return False
    if actual == 'object':
        props = schema.get('properties', {})
        return (all(k in value for k in schema.get('required', []))
                and (schema.get('additionalProperties') is not False or not set(value) - set(props))
                and all(matches_schema(v, props[k]) for k, v in value.items() if k in props))
    if actual == 'array':
        return all(matches_schema(v, schema['items']) for v in value)
    return True


def recover_misplaced_steps(plan, schema):
    """Recover only empty steps + null clarification + one complete JSON array.

    No recursive markup interpretation, default constraints, partial JSON,
    discarded suffix, or replacement of an existing nonempty plan is allowed.
    Original provider content stays in the existing protected proposal event.
    """
    if not isinstance(plan, dict) or plan.get('steps') != [] or plan.get('clarification') is not None:
        return plan, None
    text = plan.get('interpreted_question')
    if not isinstance(text, str) or len(text) > 64000:
        return plan, None
    marker = '</interpreted_question>\n<parameter name="steps">'
    if text.count(marker) != 1:
        return plan, None
    question, encoded = text.split(marker)
    if not question.strip() or re.search(r'</?(?:parameter|interpreted_question)\b', question):
        return plan, None
    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    try:
        steps = json.loads(encoded, object_pairs_hook=unique_pairs)
    except (ValueError, TypeError, RecursionError):
        return plan, None
    result = {**deepcopy(plan), 'interpreted_question': question.strip(), 'steps': steps}
    if not steps or not matches_schema(result, schema):
        return plan, None
    return result, {'version': VERSION, 'kind': 'misplaced_steps_argument',
                    'original_sha256': hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
                    'recovered_step_count': len(steps)}
