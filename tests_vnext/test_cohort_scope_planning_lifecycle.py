"""Offline API lifecycle for an over-specified cohort path.

Only model output, graph metadata/read records, and EXPLAIN are fixtures. The
production planner finalizer, semantic/identity resolution, query template,
validation, materialization, preview, confirmation, and answer facts run here.
This is a representative proposal, not a replay of a saved model response.
"""
import asyncio
from copy import deepcopy
import time

from neo4j.graph import Graph, Node
import pytest

from pankagent_vnext.answer_facts import build_answer_facts
from pankagent_vnext.config import Settings
from pankagent_vnext.evidence_coverage import coverage_for_answer
from pankagent_vnext.graph import GraphAdapter, validate_cypher
from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.release_schema import REGISTRY
from pankagent_vnext.semantic_registry import STAGES
from test_graph import FakeSession, FakeTransaction
from test_runtime import Gateway, new_plan, service, wait_state
from test_template_path_normalization import QUESTION, proposal


TISSUE = {'id': 'UBERON_0015865',
          'name': 'pancreaticosplenic lymph node (proxy for "pancreatic LN")',
          'labels': ['anatomical_structure']}
VOCABULARY = {
    'stages': list(STAGES.values()), 'sources': ['HPAP'],
    'donor_sources': ['HPAP'], 'sample_sources': ['fixture-provider'],
    'modalities': ['scRNA-seq', 'snMultiomics'], 'tissues': [TISSUE],
    'inventory_complete': True, 'inventory_sha256': 'fixture-verified-inventory',
    'modality_links_verified': True,
}


def driver_rows(empty=False, assay='scRNA-seq'):
    if empty:
        return [{'nodes': [], 'edges': []}]
    graph = Graph()
    donor = Node(graph, 'fixture-donor', 1, ['donor'], {
        'id': 'fixture-donor', 'data_source': 'HPAP', 't1d_stage': STAGES['3']})
    tissue = Node(graph, 'fixture-tissue', 2, ['anatomical_structure'], {
        key: value for key, value in TISSUE.items() if key != 'labels'})
    nodes, edges = [donor, tissue], []
    relationship = graph.relationship_type('HAS_SAMPLE')
    for index in (1, 2):
        sample = Node(graph, f'fixture-sample-{index}', index + 2,
                      ['Sample_node'], {'id': f'fixture-sample-{index}',
                                        'data_modality': assay})
        nodes.append(sample)
        for offset, source in enumerate((donor, tissue)):
            edge = relationship(graph, f'fixture-edge-{index}-{offset}',
                                10 + index * 2 + offset, {})
            edge._start_node, edge._end_node = source, sample
            edges.append(edge)
    # Repeated joins/returned identities must not inflate the Python count.
    return [{'nodes': nodes + nodes, 'edges': edges + edges}]


class FixtureGraph(GraphAdapter):
    def __init__(self, settings, *, empty=False, assay='scRNA-seq'):
        # Deliberately do not construct HTTP/Neo4j clients in offline tests.
        self.settings = settings
        self.identity_verified = True
        self.identity_check_time = time.monotonic()
        self.release_labels = set(REGISTRY['nodes'])
        self.release_relations = {'HAS_SAMPLE', 'HAS_DONOR'}
        self.records = driver_rows(empty, assay)
        self.reads, self.explains, self.generation_calls = [], [], 0
        self.query_repair = None

    async def ground_question(self, question):
        # Exercise normal post-planning live identity resolution even when the
        # large preplanning index is unavailable; no injected identity proof.
        return {'status': 'unavailable', 'diagnostic': 'offline-index-fixture'}

    async def semantic_vocabulary(self):
        return deepcopy(VOCABULARY)

    async def _small_query(self, query, params=None):
        assert query == ('MATCH (n:anatomical_structure) RETURN n.id AS id,'
                         'n.name AS name,labels(n) AS labels')
        return [deepcopy(TISSUE)]

    async def _explain(self, query, parameters):
        self.explains.append((query, deepcopy(parameters)))
        return []

    def _session(self):
        owner = self

        class ReadTransaction(FakeTransaction):
            async def run(self, query, parameters):
                owner.reads.append((query, deepcopy(parameters)))
                return await super().run(query, parameters)

        return FakeSession(ReadTransaction(self.records))

    async def _generate(self, *args, **kwargs):
        self.generation_calls += 1
        raise AssertionError('A verified cohort template must not call a model')

    async def probe(self):
        return {'state': 'healthy', 'identity_verified': True,
                'graph_version': self.settings.graph_version}

    async def probe_cypher(self):
        return {'state': 'healthy', 'replicas': 1}

    async def close(self):
        pass


class FixtureGateway(Gateway):
    api_key = 'offline-fixture-only'

    def __init__(self, settings):
        super().__init__(delay=0)
        self.settings = settings
        self.answer_facts = []

    async def plan(self, question, history, grounding=None, **kwargs):
        self.plans += 1
        return await ClaudeGateway.plan(self, question, history,
                                       grounding=grounding, **kwargs)

    async def synthesize(self, question, evidence):
        self.syntheses += 1
        assert len(evidence) == 1
        item = next(iter(evidence.values()))
        facts = build_answer_facts(item, coverage=coverage_for_answer(item))
        self.answer_facts.append(facts)
        assert facts['complete_for_executed_scope']
        count = facts['sample_counts']['unique_retrieved_samples']
        yield f'{count} matching samples. [{item["evidence_id"]}]'


@pytest.mark.parametrize('expanded_wording', [False, True])
@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize(('requested_assay', 'recorded_assay'), [
    ('scRNAseq', 'scRNA-seq'),
    ('scRNA-seq', 'scRNA-seq'),
    ('snMultiomics', 'snMultiomics'),
])
def test_cohort_plan_preview_confirmation_preserves_scope_and_verified_count(
        tmp_path, monkeypatch, empty, requested_assay, recorded_assay, expanded_wording):
    original_question = (f'How many distinct {requested_assay} samples from pancreatic lymph node (PLN) are available from HPAP donors with T1D stage 3?' if expanded_wording else QUESTION.replace('scRNAseq', requested_assay))

    async def fixed_model_session(gateway, question, user, system, schema,
                                  output_limit, finalize, **kwargs):
        assert question == original_question
        candidate = proposal()
        candidate['interpreted_question'] = original_question
        candidate['steps'][0]['question'] = original_question
        for constraint in candidate['steps'][0]['constraints']:
            if constraint['property'] == 'data_modality':
                constraint['value'] = recorded_assay
        return finalize(candidate, [])

    monkeypatch.setattr('pankagent_vnext.planning_session.run', fixed_model_session)

    async def scenario():
        settings = Settings(state_dir=tmp_path)
        graph = FixtureGraph(settings, empty=empty, assay=recorded_assay)
        gateway = FixtureGateway(settings)
        async with service(tmp_path, graph=graph, gateway=gateway) as (client, runtime, _, _, _):
            graph.settings = gateway.settings = runtime.settings
            created = await new_plan(client, original_question, include_context=False)
            ready = (await client.get(created['plan_url'])).json()
            step = ready['plan']['steps'][0]
            assert 'path_spec' not in step
            assert step['semantic_request']['source'] == 'user_request'
            assert step['semantic_request']['question'] == original_question
            assert not step.get('semantic_issues')
            assert step['sample_requirements']['modality_groups'] == [[recorded_assay]]
            assert step['sample_requirements']['paired'] is False
            assert step['sample_requirements']['separate_bindings'] is False
            assert {(c['entity_type'], c['property'], c['operator'], c['value'])
                    for c in step['constraints']} == {
                ('donor', 'data_source', '=', 'HPAP'),
                ('donor', 't1d_stage', '=', STAGES['3']),
                ('Sample_node', 'data_modality', '=', recorded_assay),
                ('anatomical_structure', 'id', '=', TISSUE['id']),
            }
            result = ready['preview']['evidence']['steps'][0]
            assert result['status'] == ('empty' if empty else 'complete')
            if expanded_wording:
                from pankagent_vnext.evidence_context import compact_evidence
                assert step['scope_language_advice']
                assert result['requested_scope']['interpretation_warnings'] == step['interpretation_warnings']
                compact = compact_evidence({'G1': result})
                assert compact[0]['requested_scope']['interpretation_warnings'] == step['interpretation_warnings']
            assert result['query_route'] == 'template'
            assert result['query_template']['template_id'] == 'donor_tissue_same_sample_records'
            assert result['truncated'] is False
            assert result['retrieval_execution']['completed']
            assert result['retrieval_execution']['cursor_exhausted']
            query = result['queries'][0]
            assert validate_cypher(query['cypher'], step, query['parameters']) == []
            for parameter in query['parameters']:
                wrong = {**query['parameters'], parameter: 'unrequested-value'}
                assert validate_cypher(query['cypher'], step, wrong)
            disconnected = query['cypher'].replace(
                '<-[rt:`HAS_SAMPLE`]-(t:`anatomical_structure`)',
                ' MATCH (other:Sample_node)<-[rt:`HAS_SAMPLE`]-(t:`anatomical_structure`)')
            assert disconnected != query['cypher']
            assert validate_cypher(disconnected, step, query['parameters'])
            assert len(graph.reads) == len(graph.explains) == 1
            assert graph.generation_calls == gateway.syntheses == 0
            assert len([node for node in result['nodes']
                        if 'Sample_node' in node['labels']]) == (0 if empty else 2)
            assert len(result['edges']) == (0 if empty else 4)
            facts = build_answer_facts(result, coverage=coverage_for_answer(result))
            assert facts['complete_for_executed_scope']
            assert facts['sample_counts']['unique_retrieved_samples'] == (0 if empty else 2)
            confirmed = await client.post(f'/v2/plans/{ready["plan_id"]}/confirm')
            assert confirmed.status_code == 202
            done = await wait_state(client, created['run_id'], {'completed', 'partial'})
            assert done['evidence']['preview_reuse']['reused_step_ids'] == ['s1']
            assert done['evidence']['preview_reuse']['retrieved_step_ids'] == []
            assert len(graph.reads) == 1
            assert graph.generation_calls == 0
            if empty and not expanded_wording:
                assert gateway.syntheses == 0
                assert 'no matching' in done['graph_answer'].casefold()
                assert done['evidence']['completeness'] == 'empty'
            else:
                assert gateway.syntheses == 1
                assert done['graph_answer'] == f'{0 if empty else 2} matching samples. [G1]'
                if empty:
                    return
                counts = gateway.answer_facts[0]['sample_counts']
                assert counts['unique_retrieved_samples'] == 2
                assert counts['by_recorded_assay'] == [{
                    'assay': {'value': recorded_assay, 'state': 'recorded'},
                    'unique_samples': 2, 'unique_linked_donors': 1}]
                assert counts['donor_sample_distribution']['distinct_donor_sample_pairs'] == 2

    asyncio.run(scenario())
