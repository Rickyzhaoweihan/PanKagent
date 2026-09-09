import asyncio
from copy import deepcopy
from unittest.mock import patch

from test_graph import FakeAdapter
from pankagent_vnext.audit import recorder
from pankagent_vnext import graph as graph_module

RELEASE = 'PanKgraph_08_04'
GOOD = "MATCH (g:Gene)-[r:SIGNAL_COLOC_WITH]->(d:disease) WHERE g.name='ADCY3' RETURN g,r,d"


async def emit(*args):
    pass


def adapter(batches):
    graph = FakeAdapter(batches)
    graph.settings.graph_version = RELEASE
    graph.settings.grounded_query_policy = True
    return graph


def step(graph, *, resolved=True):
    c = {'entity_type': 'Gene', 'property': 'name', 'operator': '=', 'value': 'ADCY3'}
    s = {'id': 's1', 'question': 'Show ADCY3 colocalization evidence', 'graph_version': RELEASE,
         'relation_types': ['SIGNAL_COLOC_WITH'], 'complete': True, 'constraints': [c]}
    if resolved:
        s['resolved_entities'] = [{'constraint_index': 0, 'requested': deepcopy(c), 'state': 'resolved',
            'entity_type': 'Gene', 'graph_version': RELEASE, 'labels': ['Gene'],
            'id': 'ENSG00000138031', 'name': 'ADCY3'}]
        s['entity_resolution'] = {'state': 'resolved'}
        s['resolution_key'] = graph._resolution_signature(s)
    return s


def test_template_does_not_need_gpu_prompt_or_spend():
    async def check():
        graph = adapter([])
        s = step(graph)
        with patch.object(graph_module, 'generation_request', side_effect=ValueError('generation_question_too_long')):
            result = await graph.execute(s, {}, emit)
        assert result['status'] == 'complete' and result['query_route'] == 'template'
        assert len(graph.explained) == len(graph.retrieved) == 1
        assert graph.generated == []
    asyncio.run(check())


def test_cache_can_be_revalidated_without_building_a_model_prompt():
    async def check():
        graph = adapter([[GOOD]])
        s = step(graph, resolved=False)
        first = await graph.execute(s, {}, emit)
        with patch.object(graph_module, 'generation_request', side_effect=AssertionError('No model prompt on cache hit')):
            second = await graph.execute(s, {}, emit)
        assert first['status'] == second['status'] == 'complete'
        assert second['query_route'] == 'cache' and len(graph.generated) == 1
        assert len(graph.retrieved) == 2
    asyncio.run(check())


def test_full_repair_provenance_is_private_and_event_has_no_query_or_entities():
    async def check():
        reversed_query = GOOD.replace(')-[', ')<-[').replace(']->(', ']-(')
        graph = adapter([[reversed_query]])
        events = []
        token = recorder.set(lambda kind, payload: events.append((kind, payload)))
        try:
            result = await graph.execute(step(graph, resolved=False), {}, emit)
        finally:
            recorder.reset(token)
        audit = result['validation'][0]['deterministic_repair_record']
        assert audit['original_query'] == reversed_query and audit['query'] == GOOD
        assert audit['registry_sha256'] and audit['implementation_sha256'] and audit['latency_ms'] >= 0
        assert result['validation'][0]['original_candidate_cypher'] == reversed_query
        event = next(payload for kind, payload in events if kind == 'cypher_validation')
        assert event['valid'] is True and event['cause_categories'] == []
        assert set(event) == {'cause_categories', 'route', 'latency_ms', 'valid'}
        assert 'ADCY3' not in str(event) and 'MATCH' not in str(event)
    asyncio.run(check())


def test_rejections_get_coarse_failure_category_and_original_reason():
    async def check():
        graph = adapter([[GOOD + ' LIMIT 1'], [GOOD]])
        result = await graph.execute(step(graph, resolved=False), {}, emit)
        rejected = result['validation'][0]
        assert rejected['failure_categories'] and rejected['reasons']
        assert all(':' not in value for value in rejected['failure_categories'])
        assert result['status'] == 'complete'
    asyncio.run(check())


def test_missing_telemetry_is_visible_without_blocking_valid_evidence():
    async def check():
        graph = adapter([[GOOD]])
        def broken(kind, payload):
            raise RuntimeError('recorder unavailable')
        token = recorder.set(broken)
        try:
            result = await graph.execute(step(graph, resolved=False), {}, emit)
        finally:
            recorder.reset(token)
        assert result['status'] == 'complete'
        assert result['telemetry_failures'] == [{'event': 'cypher_validation', 'category': 'RuntimeError'}]
    asyncio.run(check())


def test_oversized_full_repair_context_skips_blind_gpu_resampling():
    async def check():
        bad = GOOD + ' LIMIT 1'
        graph = adapter([[bad]])
        calls = []
        async def repair(current, question, failures, candidate):
            calls.append((question, failures, candidate))
            return [GOOD]
        graph.query_repair = repair
        prompt = 'Grounded request ' + 'x' * 3900
        with patch.object(graph_module, 'generation_request', return_value=prompt):
            result = await graph.execute(step(graph, resolved=False), {}, emit)
        assert result['status'] == 'complete'
        assert len(graph.generated) == 1
        assert len(calls) == 1 and calls[0][2] == bad and calls[0][0] == prompt
        skipped = [v for v in result['validation'] if v.get('skipped')]
        assert skipped == [{'valid': False, 'route': 'gpu_repair',
                            'reasons': ['gpu_repair_context_too_large'], 'skipped': True}]
        assert [a['route'] for a in result['generator_attempts']] == ['gpu_initial', 'claude_repair']
    asyncio.run(check())
