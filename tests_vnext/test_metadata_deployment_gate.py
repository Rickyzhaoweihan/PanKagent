"""Network-free regression gate for the audited donor false-zero query."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pankagent_vnext.graph import GraphAdapter, validate_cypher
from pankagent_vnext.metadata_guard import recovery, RELEASE
from test_runtime import service, new_plan

AUDITED = 'How many Stage 3 T1D male HPAP donors are age 18–35 with BMI <28?'


def step(question=AUDITED, prop='bmi', value='28', operator='<'):
    return {'id': 's1', 'question': question, 'graph_version': RELEASE,
            'constraints': [{'entity_type': 'donor', 'property': prop,
                             'operator': operator, 'value': value}],
            'complete': True, 'depends_on': [], 'relation_types': []}


@pytest.mark.parametrize('query', [
    'MATCH (d:donor) WHERE d.bmi < 28 RETURN d',
    'MATCH (d:donor) WHERE toFloat(d.bmi) < 28 RETURN d',
    "MATCH (d:donor) WHERE d.bmi < '28' RETURN d",
    'MATCH (d:donor) WHERE coalesce(toFloat(d.bmi), 0) < 28 RETURN d',
])
def test_audited_bmi_cannot_validate_raw_cast_or_unknown_default(query):
    assert validate_cypher(query, step()) == ['unsupported_donor_bmi_filter']


@pytest.mark.parametrize('question, category', [
    (AUDITED, 'unsupported_donor_bmi_filter'),
    ('Count HPAP donors with body mass index below 28', 'unsupported_donor_bmi_filter'),
    ('Count male HPAP stage 3 donors', 'donor_sex_gender_needs_clarification'),
    ('Count female donors', 'donor_sex_gender_needs_clarification'),
    ('Count HPAP donors with recorded sex at birth Male', 'unsupported_donor_sex_at_birth_coverage'),
])
def test_actionable_request_gate_preserves_request_and_makes_no_zero_claim(question, category):
    source = {'question': question}
    before = deepcopy(source)
    issue = recovery(source, RELEASE)
    assert issue['category'] == category
    assert issue['evidence']['matching_records_checked'] is False
    assert issue['retryable'] is False
    assert source == before


@pytest.mark.parametrize('question', [
    'Which cell types express INS?', 'What pathways contain BMI1?',
    'Show genes associated with male infertility',
    'Count HPAP stage 3 donors', 'Count donors with recorded gender Male',
    'Show donor age values', 'List the recorded BMI values of HPAP donors',
])
def test_non_donor_and_supported_donor_requests_remain_available(question):
    assert recovery({'question': question}, RELEASE) is None


def test_guard_does_not_claim_a_contract_for_other_releases():
    assert recovery(step(), 'other-release') is None


def test_model_rephrasing_cannot_resolve_user_gender_ambiguity():
    source = step('Count donors with recorded gender Male', 'gender', 'Male', '=')
    source['semantic_request'] = {'source': 'user_request', 'question': 'Count male HPAP donors'}
    assert recovery(source, RELEASE)['category'] == 'donor_sex_gender_needs_clarification'
    source['semantic_request']['revision_instruction'] = 'Use recorded gender Male.'
    assert recovery(source, RELEASE) is None


def test_explicit_gender_remains_a_valid_filter():
    source = step('Count donors with recorded gender Male', 'gender', 'Male', '=')
    assert validate_cypher("MATCH (d:donor) WHERE d.gender = 'Male' RETURN d", source) == []


@pytest.mark.parametrize('prop, category', [
    ('bmi', 'unsupported_donor_bmi_filter'),
    ('sex_at_birth', 'unsupported_donor_sex_at_birth_coverage'),
])
def test_structured_filters_cannot_bypass_guard_when_question_omits_field(prop, category):
    source = step('Count HPAP donors', prop, 'Male' if prop == 'sex_at_birth' else '28')
    assert recovery(source, RELEASE)['category'] == category


class NoNetworkGraph(GraphAdapter):
    def __init__(self):
        self.settings = SimpleNamespace(graph_version=RELEASE)

    def _resolution_signature(self, current):
        return 'synthetic-resolution-signature'

    async def _small_query(self, *args, **kwargs):
        raise AssertionError('The guarded request must not query Neo4j')

    async def probe(self):
        raise AssertionError('The guarded request must not probe the graph')


async def emit(*args):
    pass


def test_prepare_and_execute_reject_before_graph_or_provider_io():
    async def check():
        graph = NoNetworkGraph()
        source = step()
        before = deepcopy(source)
        prepared = await graph._prepare_step(source, emit)
        assert prepared['entity_resolution']['state'] == 'needs_clarification'
        assert prepared['recovery']['category'] == 'unsupported_donor_bmi_filter'
        assert source == before
        result = await graph.execute(source, {}, emit)
        assert result['status'] == 'failed'
        assert result['queries'] == [] and result['generator_attempts'] == []
        assert result['nodes'] == [] and result['rows'] == []
        assert result['validation'] == [{'valid': False, 'reasons': ['unsupported_donor_bmi_filter']}]
    asyncio.run(check())


@pytest.mark.parametrize('question, category', [
    (AUDITED, 'unsupported_donor_bmi_filter'),
    ('Count male HPAP donors', 'donor_sex_gender_needs_clarification'),
    ('Count HPAP donors with sex_at_birth Male', 'unsupported_donor_sex_at_birth_coverage'),
])
def test_public_runtime_returns_typed_failure_before_planning_or_retrieval(tmp_path, question, category):
    async def check():
        async with service(tmp_path, graph_version=RELEASE) as (client, runtime, gateway, graph, literature):
            created = await new_plan(client, question, expected_status='failed')
            result = (await client.get('/v2/runs/' + created['run_id'])).json()
            assert result['status'] == 'failed'
            assert result['error']['category'] == category
            assert result['error']['recovery']['evidence']['matching_records_checked'] is False
            assert gateway.plans == gateway.syntheses == graph.calls == literature.calls == 0
            assert result.get('answer') is None
    asyncio.run(check())
