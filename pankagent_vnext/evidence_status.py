"""Deterministic outcome wording; retrieval failure is never biological absence."""

def outcome_message(evidence):
    steps = list(evidence.values()) if isinstance(evidence, dict) else list(evidence)
    primary = [s for s in steps if s.get('purpose') != 'context'] or steps
    usable = [s for s in primary if s.get('status') in ('complete', 'partial')
              and any(s.get(k) for k in ('nodes', 'edges', 'rows'))]
    if usable:
        return None
    failed = any(s.get('status') not in ('complete', 'empty') for s in primary)
    if failed or not primary:
        return ('I couldn’t retrieve the graph evidence needed to answer this question. '
                'This is a retrieval failure, so it does not establish whether the biological relationship is present or absent. '
                'The step outcomes below identify what could not be checked.')
    return ('No matching records were found in the checked graph for the requested entities and filters. '
            'This describes the checked dataset and scope; it does not establish biological absence. '
            'Inspect the executed query and sources below before drawing a broader conclusion.')


def confirmation_eligible(plan, preview):
    if plan.get('clarification') or not preview:
        return False
    steps = (preview.get('evidence') or {}).get('steps') or []
    primary = [s for s in steps if s.get('purpose') != 'context']
    return bool(primary) and any(s.get('status') in ('complete', 'empty') or
        (s.get('status') == 'partial' and any(s.get(k) for k in ('nodes', 'edges', 'rows'))) for s in primary)
