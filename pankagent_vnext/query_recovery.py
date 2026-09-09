"""Evidence-backed recovery wording; suggestions never alter the submitted plan."""
import re

VERSION = 'query-recovery-4'

def stage_recovery(number, vocabulary, release):
    recorded = vocabulary.get('stages')
    complete = (vocabulary.get('inventory_complete') is True
                and isinstance(recorded, list) and all(isinstance(value, str) for value in recorded))
    if not complete:
        return {'category':'stage_inventory_unavailable','title':'Stage metadata could not be verified',
                'message':'We could not verify the complete donor-stage inventory for this graph release. Your stage, tissue and assay filters have been kept. Retry the same question when the metadata check is available; this does not mean that matching donors are absent.',
                'retryable':True,'suggestions':[],
                'evidence':{'graph_release':release,'source':'unverified donor-stage inventory','inventory_complete':False}}
    stages = sorted({m[1] for v in recorded if (m := re.match(r'^Stage (\d+):', v))}, key=int)
    known_clinical = number in ('1','2','3')
    if number is None:
        message = 'Your question mentions a T1D stage without specifying which stage. Choose a recorded stage below or tell us which one you mean; your tissue, assay and other filters will be kept.'
        category = 'stage_needs_clarification'
    elif known_clinical and number not in stages:
        message = f'No donors are recorded as stage {number} in the checked {release} stage inventory. This describes the indexed release, not whether such donors exist elsewhere. You can choose a recorded stage or remove the stage restriction.'
        category = 'recorded_stage_unavailable'
    else:
        message = f'We could not uniquely resolve stage {number} to a recorded donor stage. Please specify the intended clinical stage; we have kept your other filters.'
        category = 'stage_needs_clarification'
    suggestions = [{'label':f'Use recorded stage {n}', 'instruction':f'Change only the T1D stage to stage {n}; keep every other entity, tissue, assay and cohort filter.'} for n in stages[:2]]
    return {'category':category,'title':'Review the requested stage','message':message,
            'retryable':False,'suggestions':suggestions,
            'evidence':{'graph_release':release,'source':'complete distinct donor-stage inventory','inventory_complete':True,'recorded_stages':stages}}

def plan_recovery(plan, release):
    steps = plan.get('steps', [])
    for step in steps:
        if step.get('recovery'):
            return step['recovery']
    issues = [i for step in steps for i in step.get('semantic_issues', [])]
    unresolved = [e for step in steps for e in step.get('resolved_entities', []) if e.get('state') not in ('resolved','literal_predicate')]
    names = [str(e.get('original_term') or e.get('requested',{}).get('value') or 'requested term') for e in unresolved]
    message = ' '.join(issues) if issues else ('We could not uniquely match '+', '.join(names)+f' to records in {release}.' if names else 'A requested relationship is not supported by the checked graph schema.')
    suggestions = []
    for entity in unresolved:
        term = entity.get('original_term') or entity.get('requested',{}).get('value')
        for candidate in entity.get('candidates', [])[:2]:
            if candidate.get('id') and candidate.get('name') and term:
                suggestions.append({'label':'Use '+candidate['name'], 'instruction':f'Resolve {term} as {candidate["name"]} ({candidate["id"]}); keep every other filter.'})
    return {'category':'scope_needs_clarification','title':'A detail needs your review','message':message,
            'retryable':False,'suggestions':suggestions[:2],'evidence':{'graph_release':release}}


def _records(value):
    """Older durable payloads can store steps as a mapping or list."""
    if isinstance(value, dict):
        value = list(value.values())
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _error_codes(value):
    if isinstance(value, dict):
        return [str(value[key]).lower() for key in ('category', 'code', 'error_category', 'http_status', 'status_code')
                if isinstance(value.get(key), (str, int))]
    return [value.lower()] if isinstance(value, str) else []


def retrieval_recovery(preview):
    """Classify exhausted retrieval, preserving zero results and usable evidence.

    Unpreviewed checks are not the same as active preview work. The final preview
    can be partial because further checks await confirmation, yet still need a
    recovery action when every attempted primary check failed.
    """
    if not isinstance(preview, dict) or preview.get('preparation_complete') is False:
        return None
    if preview.get('status') not in ('failed', 'partial'):
        return None
    evidence = preview.get('evidence') or {}
    evidence = evidence if isinstance(evidence, dict) else {}
    steps = _records(evidence.get('steps'))
    primary = [step for step in steps if step.get('purpose') != 'context']
    if any(step.get('status') in ('complete', 'empty') or
           (step.get('status') == 'partial' and any(step.get(key) for key in ('nodes', 'edges', 'rows')))
           for step in primary):
        return None
    failed = [step for step in primary if step.get('status') in ('failed', 'unavailable', 'blocked', 'partial')]
    if not failed and not preview.get('error'):
        return None

    # Use structured categories and the final failed attempt, never scan raw
    # error prose or biological filter values for coincidental "401"/"connect".
    codes = _error_codes(preview.get('error'))
    for step in failed:
        codes.extend(_error_codes(step.get('error')))
        validations = _records(step.get('validation'))
        if validations:
            reasons = validations[-1].get('reasons', [])
            reasons = [reasons] if isinstance(reasons, str) else reasons
            for reason in reasons if isinstance(reasons, list) else []:
                if isinstance(reason, dict):
                    codes.extend(_error_codes(reason))
                elif isinstance(reason, str):
                    head, _, suffix = reason.lower().partition(':')
                    codes.append(head)
                    if head in ('generation_unavailable', 'graph_execution_failed'):
                        codes.append(suffix.split(':', 1)[0])
        attempts = _records(step.get('generator_attempts'))
        if attempts and attempts[-1].get('status') in ('failed', 'timeout'):
            codes.extend(_error_codes(attempts[-1]))
    if any(code.startswith(('graph_identity', 'graph_schema_identity', 'graph_anchor_identity', 'graph_release',
                            'graph_contract_relationship_mismatch', 'invalid_identity_anchor')) for code in codes):
        category, title, message, retryable = ('graph_release_mismatch', 'The graph service needs attention',
            'The service could not verify that its query rules and database belong to the same release. Your question has been kept; changing it will not resolve this service problem.', False)
    elif any('budget' in code for code in codes):
        category, title, message, retryable = ('budget_exhausted', 'The demo budget is unavailable',
            'The demo cannot start another model request within its remaining budget. Your question and available evidence are saved. An operator needs to review the budget.', False)
    elif any(code in ('401', '403', 'authentication', 'authorization', 'authenticationerror', 'permissionerror') for code in codes):
        category, title, message, retryable = ('authentication', 'The query service needs attention',
            'A required service did not accept the configured access credentials. Your question has been kept; an operator needs to restore access.', False)
    elif any(code in ('402', 'billing', 'billingerror') for code in codes):
        category, title, message, retryable = ('billing', 'The query service needs attention',
            'A required model service could not accept a request because of its billing status. Your question has been kept; an operator needs to restore access.', False)
    elif any(code in ('429', 'rate_limited', 'ratelimiterror') for code in codes):
        category, title, message, retryable = ('rate_limited', 'The query service is busy',
            'A required service temporarily limited requests. Wait briefly, then retry the same question. Your entities, tissue and assay filters will be kept.', True)
    elif any(code.startswith(('run_graph_materialization_limit', 'response_size', 'materialization_limit')) for code in codes):
        category, title, message, retryable = ('retrieval_limit', 'The search reached its retrieval limit',
            'The search reached a resource limit before returning usable evidence. This does not establish that matching records are absent. Your filters are saved; the operator may need to review the retrieval limit.', True)
    elif any(code.startswith('graph_execution_failed') for code in codes):
        category, title, message, retryable = ('graph_execution_failed', 'The database search could not finish',
            'The generated search passed validation, but the database could not finish executing it. No conclusion about matching records can be drawn. Retry the same question without removing its filters.', True)
    elif any(code in ('generation_unavailable', 'graph_unavailable', 'dependency_unavailable', 'connection', 'connectionerror',
                      'connecterror', 'readerror', 'writeerror', 'remoteprotocolerror', 'serviceunavailable', '502', '503', '504')
             or code.endswith('timeout') or code.endswith('timeouterror') for code in codes):
        category, title, message, retryable = ('retrieval_unavailable', 'The search service could not finish',
            'A required query service was unavailable or exceeded its time limit. This does not establish that matching data is absent. Retry will keep your original entities and filters.', True)
    else:
        category, title, message, retryable = ('query_validation', 'The generated search could not be verified',
            'PanKgraph could not verify a generated query that preserved all requested filters and relationships after its bounded alternatives. No reliable result was returned. You can retry the same question without removing your requirements.', True)
    return {'category': category, 'title': title, 'message': message, 'retryable': retryable, 'suggestions': [],
            'evidence': {'graph_release': evidence.get('graph_version'),
                         'failed_step_ids': [step.get('step_id') for step in failed]}}
