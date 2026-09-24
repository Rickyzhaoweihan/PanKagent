"""Bounded public-entity lookup shared by grounding, planning tools and preparation."""
import asyncio
import hashlib
import hmac
import json
import re
import secrets
from .grounding_inventory import PUBLIC_CATALOG_LABELS, _synonyms
from .agent_schemas import active_pack, module as schema_module

VERSION = 'entity-lookup-v1'
IDENTITY = schema_module('identity')
INDEX_NAME = IDENTITY['index_name']
LABELS = tuple(PUBLIC_CATALOG_LABELS) + tuple(IDENTITY.get('exact_only_labels', []))
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


def mention_in_request(mention, question):
    # Shared deterministic lexical normalization: case, hyphens, Greek letters
    # and cell/cells. It does not invent synonym mappings.
    from .preplanning_grounding import phrase_tokens
    words, needle = phrase_tokens(question), phrase_tokens(mention)
    return bool(needle) and any(words[i:i+len(needle)] == needle
                               for i in range(len(words)-len(needle)+1))


def valid_selection(graph, proof, question):
    return (isinstance(proof, dict) and proof.get('graph_release') == graph.settings.graph_version
            and proof.get('schema_sha256', active_pack().digest) == active_pack().digest
            and isinstance(proof.get('mention'), str) and bool(proof['mention'])
            and mention_in_request(proof['mention'], question)
            and isinstance(proof.get('token'), str)
            and hmac.compare_digest(proof['token'], selection_token(graph, proof)))


def match_fields(row, mention):
    fields = [(key, row.get(key)) for key in [IDENTITY['id_field'], *IDENTITY['name_fields']]]
    fields += [(IDENTITY['synonym_field'], value) for value in _synonyms(row.get(IDENTITY['synonym_field']))]
    return [{'field': key, 'value': value} for key, value in fields
            if isinstance(value, str) and (value == mention if key == IDENTITY['id_field'] else value.casefold() == mention.casefold())]


async def lookup(graph, mention, entity_type, fuzzy=False):
    if entity_type not in LABELS or not isinstance(mention, str) or not 0 < len(mention.strip()) <= 128:
        return {'requested': mention, 'status': 'invalid_request', 'candidates': []}
    mention = mention.strip()
    if entity_type in IDENTITY.get('exact_only_labels', []):
        fuzzy = False
    await graph._ensure_identity()
    id_field, synonym = IDENTITY['id_field'], IDENTITY['synonym_field']
    names = IDENTITY['name_fields']
    columns = list(dict.fromkeys([id_field, *names, synonym]))
    fields = ', '.join(f'n.`{key}` AS `{key}`' for key in columns) + ', labels(n) AS labels'
    # Exact lookup covers both scalar legacy synonyms and modern list properties.
    # Delimiter parsing stays in Python; the bounded inventory below handles legacy arrays.
    predicates = [f'n.`{id_field}` = $mention']
    predicates += [f'toLower(n.`{key}`) = toLower($mention)' for key in names]
    predicates.append(f"any(s IN CASE valueType(n.`{synonym}`) WHEN 'STRING NOT NULL' THEN split(replace(n.`{synonym}`, '|', ';'), ';') ELSE coalesce(n.`{synonym}`, []) END WHERE toLower(trim(s)) = toLower($mention))")
    query = f'MATCH (n:`{entity_type}`) WHERE ' + ' OR '.join(predicates) + ' RETURN ' + fields + ' LIMIT 11'
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
        if not isinstance(row.get(id_field), str) or entity_type not in row.get('labels', []):
            continue
        matches = match_fields(row, mention)
        proof = {'mention': mention, 'id': row[id_field], 'name': next((row[k] for k in names if row.get(k)), row[id_field]),
                 'entity_type': entity_type, 'graph_release': graph.settings.graph_version,
                 'schema_sha256': active_pack().digest,
                 'match_method': 'recorded_alias' if matches and any(m['field'] == synonym for m in matches) else
                                 'recorded_id' if row[id_field] == mention else 'recorded_name' if matches else method,
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


def retain_grounding_proofs(graph, grounding):
    """Sign verified inventory candidates; existence does not settle ambiguity."""
    from copy import deepcopy
    result = deepcopy(grounding)
    if result.get('status') != 'ready' or result.get('identity', {}).get('graph_release') != graph.settings.graph_version:
        return result
    for mention in result.get('mentions', []):
        for candidate in mention.get('candidates', [])[:IDENTITY['max_candidates']]:
            if candidate.get('entity_type') not in LABELS or not candidate.get('id'):
                continue
            proof = {'mention': mention['requested'], 'id': candidate['id'],
                     'name': candidate.get('name') or candidate['id'], 'entity_type': candidate['entity_type'],
                     'graph_release': graph.settings.graph_version, 'schema_sha256': active_pack().digest,
                     'match_method': candidate.get('match_kind', 'recorded_alias'),
                     'matched_fields': candidate.get('matched_fields', []),
                     'source': 'verified_preplanning_inventory'}
            proof['token'] = selection_token(graph, proof)
            candidate['selection_proof'] = proof
    return result
