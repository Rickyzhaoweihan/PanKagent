"""Bounded public-entity lookup shared by grounding, planning tools and preparation."""
import asyncio
import hashlib
import hmac
import json
import re
import secrets
from .grounding_inventory import PUBLIC_CATALOG_LABELS, _synonyms

VERSION = 'entity-lookup-v1'
INDEX_NAME = 'pankagent_public_entities_v1'
LABELS = tuple(PUBLIC_CATALOG_LABELS)
TOOL_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'requests': {'type': 'array', 'minItems': 1, 'maxItems': 6, 'items': {
        'type': 'object', 'additionalProperties': False, 'properties': {
            'mention': {'type': 'string', 'minLength': 1, 'maxLength': 128},
            'entity_type': {'type': 'string', 'enum': list(LABELS)},
            'fuzzy': {'type': 'boolean'}}, 'required': ['mention', 'entity_type', 'fuzzy']}}},
    'required': ['requests']}
CHOICE_SCHEMA = {'type': 'array', 'maxItems': 12, 'items': {'type': 'object', 'additionalProperties': False,
    'properties': {key: {'type': 'string'} for key in ('mention', 'entity_type', 'id', 'reason')},
    'required': ['mention', 'entity_type', 'id', 'reason']}}


def selection_token(graph, value):
    if not hasattr(graph, '_resolution_secret'):
        graph._resolution_secret = secrets.token_bytes(32)
    body = {k: v for k, v in value.items() if k != 'token'}
    return hmac.new(graph._resolution_secret, json.dumps(body, sort_keys=True, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()


def valid_selection(graph, proof, question):
    return (isinstance(proof, dict) and proof.get('graph_release') == graph.settings.graph_version
            and isinstance(proof.get('mention'), str) and bool(proof['mention'])
            and re.search(r'(?<!\w)' + re.escape(proof['mention']) + r'(?!\w)', question, re.I) is not None
            and isinstance(proof.get('token'), str)
            and hmac.compare_digest(proof['token'], selection_token(graph, proof)))


def match_fields(row, mention):
    fields = [(key, row.get(key)) for key in ('id', 'name', 'hgnc_symbol', 'hgnc_id')]
    fields += [('synonyms', value) for value in _synonyms(row.get('synonyms'))]
    return [{'field': key, 'value': value} for key, value in fields
            if isinstance(value, str) and (value == mention if key == 'id' else value.casefold() == mention.casefold())]


async def lookup(graph, mention, entity_type, fuzzy=False):
    if entity_type not in LABELS or not isinstance(mention, str) or not 0 < len(mention.strip()) <= 128:
        return {'requested': mention, 'status': 'invalid_request', 'candidates': []}
    mention = mention.strip()
    await graph._ensure_identity()
    fields = 'n.id AS id, n.name AS name, n.synonyms AS synonyms, n.hgnc_symbol AS hgnc_symbol, n.hgnc_id AS hgnc_id, labels(n) AS labels'
    # Exact lookup covers both scalar legacy synonyms and modern list properties.
    # Delimiter parsing stays in Python; the bounded inventory below handles legacy arrays.
    query = (f'MATCH (n:`{entity_type}`) WHERE n.id = $mention OR toLower(n.name) = toLower($mention) '
             'OR toLower(n.hgnc_symbol) = toLower($mention) OR n.hgnc_id = $mention '
             "OR any(s IN CASE valueType(n.synonyms) WHEN 'STRING NOT NULL' THEN split(replace(n.synonyms, '|', ';'), ';') ELSE coalesce(n.synonyms, []) END WHERE toLower(trim(s)) = toLower($mention)) RETURN " + fields + ' LIMIT 11')
    rows = await graph._small_query(query, {'mention': mention})
    method = 'exact_or_synonym'
    incomplete = len(rows) > 10
    if not rows and fuzzy:
        # Lucene punctuation is never accepted as a user-authored query expression.
        terms = re.findall(r'[^\W_]+', mention, re.UNICODE)[:8]
        expression = ' AND '.join(term if len(term) < 4 else term + '~1' for term in terms)
        if expression:
            try:
                rows = await graph._small_query(
                    'CALL db.index.fulltext.queryNodes($index, $text, {limit: 100}) YIELD node AS n, score '
                    f'WHERE n:`{entity_type}` RETURN ' + fields + ', score ORDER BY score DESC LIMIT 11',
                    {'index': INDEX_NAME, 'text': expression})
            except Exception as exc:
                return {'requested': mention, 'entity_type': entity_type, 'status': 'unavailable',
                        'error': 'fuzzy_index_unavailable', 'candidates': [], 'graph_release': graph.settings.graph_version}
            method = 'fuzzy'
            incomplete = len(rows) > 10 or len(rows) == 0
    candidates = []
    for row in rows[:10]:
        if not isinstance(row.get('id'), str) or entity_type not in row.get('labels', []):
            continue
        matches = match_fields(row, mention)
        proof = {'mention': mention, 'id': row['id'], 'name': row.get('name') or row['id'],
                 'entity_type': entity_type, 'graph_release': graph.settings.graph_version,
                 'match_method': 'recorded_alias' if matches and any(m['field'] == 'synonyms' for m in matches) else
                                 'recorded_id' if row['id'] == mention else 'recorded_name' if matches else method,
                 'matched_fields': matches}
        proof['token'] = selection_token(graph, proof)
        candidates.append({**proof, 'labels': row['labels'], 'score': row.get('score'),
                           'selection_proof': proof})
    return {'requested': mention, 'entity_type': entity_type,
            'status': 'candidates' if method == 'fuzzy' and candidates else 'resolved' if len(candidates) == 1 and not incomplete else 'ambiguous' if candidates else 'no_candidates' if method == 'fuzzy' else 'not_found',
            'graph_release': graph.settings.graph_version, 'candidates_complete': not incomplete,
            'candidates': candidates}


async def resolve_entities(graph, requests):
    if not isinstance(requests, list) or not 1 <= len(requests) <= 6:
        return {'status': 'invalid_request', 'results': []}
    async def one(request):
        try:
            return await asyncio.wait_for(lookup(graph, request.get('mention'), request.get('entity_type'), request.get('fuzzy', False)), 6)
        except Exception:
            return {'requested': request.get('mention'), 'entity_type': request.get('entity_type'),
                    'status': 'unavailable', 'error': 'entity_lookup_unavailable', 'candidates': []}
    # At most two outstanding database requests, even in a six-mention batch.
    sem = asyncio.Semaphore(2)
    async def bounded(request):
        if not isinstance(request, dict): return {'status': 'invalid_request', 'candidates': []}
        async with sem: return await one(request)
    return {'version': VERSION, 'results': await asyncio.gather(*(bounded(r) for r in requests))}
