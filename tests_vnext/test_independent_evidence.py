from copy import deepcopy
import hashlib
import json
import pytest
from pankagent_vnext.annotation_selection import apply_default
from pankagent_vnext.evidence_context import compact_evidence
from pankagent_vnext.evidence_status import synthesis_evidence
from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.query_templates import compile_query as _compile_query
from test_query_templates import step
from test_evidence_context import evidence, node, edge


def _authorized(step):
    """Attach exact immutable-request proofs without mutating the fixture."""
    prepared = deepcopy(step)
    question = 'Independent-evidence template fixture: ' + '; '.join(
        f"{constraint.get('entity_type') or constraint.get('relationship_type') or 'node'}."
        f"{constraint.get('property')} {constraint.get('operator', '=')} "
        f"{constraint.get('value')}"
        for constraint in prepared.get('constraints') or [])
    prepared['semantic_request'] = {'source': 'user_request', 'question': question}
    digest = hashlib.sha256(question.encode()).hexdigest()
    prepared['request_filter_bindings'] = [{
        'constraint_index': index,
        'canonical_binding': deepcopy(constraint),
        'authorization_kind': 'verified_request_filter',
        'source': 'immutable_user_request',
        'request_sha256': digest,
        'graph_release': prepared.get('graph_version'),
    } for index, constraint in enumerate(prepared.get('constraints') or [])]
    return prepared


def compile_query(step):
    return _compile_query(_authorized(step))


@pytest.mark.parametrize('kind', ['FUNCTION_ANNOTATION', 'ASSOCIATED_WITH_GO'])
def test_annotation_default_retrieves_full_membership(kind):
    source = step(kind)
    selected = apply_default(source, 'What is the role of CFTR?')
    query = compile_query(selected)
    assert source['complete'] is True
    assert selected['complete'] is True
    assert 'retrieval_selection' not in selected
    assert query and 'LIMIT' not in query['cypher']
    assert query['parameters'] == {'template_0': 'ENSG00000001626'}
    assert validate_cypher(query['cypher'], _authorized(selected), query['parameters']) == []
    assert validate_cypher(query['cypher'] + ' LIMIT 10', _authorized(selected), query['parameters'])


@pytest.mark.parametrize('question', ['How many GO terms?', 'List all annotations', '全部功能注释', '有多少个注释', 'Top 20 pathways', 'What proportion?', 'Count the pathways', 'Limit 5 terms'])
def test_explicit_population_or_selection_is_not_silently_limited(question):
    source = step('ASSOCIATED_WITH_GO')
    assert apply_default(source, question) == source


def test_truncated_records_stay_in_audit_but_not_synthesis():
    source = {'good': evidence([node('good')], [edge('good', 'cell', score=4)]),
              'bad': evidence([node('bad')], [edge('bad', 'cell', score=99)],
                              status='partial', truncated=True, step_id='bad')}
    original = deepcopy(source)
    view = synthesis_evidence(source)
    assert source == original
    assert view['good'] == source['good']
    assert view['bad']['nodes'] == view['bad']['edges'] == view['bad']['rows'] == []
    assert view['bad']['status'] == 'unavailable'


def test_large_branch_does_not_erase_small_verified_measurements():
    good = evidence([node('g'), node('c')], [edge('g', 'c', score=4)])
    props = {'measurement_' + str(i): 'x' * 200 for i in range(20)}
    large = evidence([node('g'), node('c')], [edge('g', 'c', **props) for _ in range(110)])
    original = deepcopy([good, large])
    result = compact_evidence([good, large], max_bytes=12000)
    assert [good, large] == original
    assert result[0]['context_compaction'] == 'standard'
    assert result[0]['edges'][0]['properties']['score'] == 4
    assert result[1]['context_compaction'] == 'node_identity_only'
    assert [r['evidence_id'] for r in result] == ['G1', 'G2']
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) <= 12000


def test_default_is_user_owned_even_if_model_already_marked_step_partial():
    selected = apply_default(step('FUNCTION_ANNOTATION', complete=False), 'Explain the role of INS in T1D')
    assert selected['complete'] is True
    assert 'retrieval_selection' not in selected
    assert compile_query(selected)


def test_annotation_edge_source_filter_is_preserved_for_mixed_target_labels():
    source = step('FUNCTION_ANNOTATION')
    source['constraints'].append({'owner_kind': 'relationship', 'relationship_type': 'FUNCTION_ANNOTATION',
                                 'property': 'data_source', 'operator': '=', 'value': 'KEGG'})
    selected = apply_default(source, 'Explain the role of this gene')
    compiled = compile_query(selected)
    assert compiled and 'r.`data_source` = $template_1' in compiled['cypher']
    assert validate_cypher(compiled['cypher'], _authorized(selected), compiled['parameters']) == []


def test_partitioned_caps_and_unbound_gene_gwas_do_not_starve_independent_checks():
    from types import SimpleNamespace
    from pankagent_vnext.annotation_selection import allocate_independent_budgets
    source = step('FUNCTION_ANNOTATION')
    gwas = step('PART_OF_GWAS_SIGNAL', [{'entity_type':'disease', 'property':'id', 'value':'disease', 'operator':'='}], question='Variants near CFTR associated with disease')
    plan = allocate_independent_budgets({'steps':[source,gwas]}, SimpleNamespace(max_bytes=2000,max_nodes=20,max_edges=40))
    assert all(s['retrieval_budget']['max_bytes']==1000 for s in plan['steps'])
    assert gwas['gwas_scope_unavailable'] is True
    gwas['question']='All disease GWAS variants'
    allocate_independent_budgets(plan, SimpleNamespace(max_bytes=2000,max_nodes=20,max_edges=40))
    assert 'gwas_scope_unavailable' not in gwas


def test_materialization_partition_leaves_capacity_for_later_steps():
    import asyncio
    from types import SimpleNamespace
    from pankagent_vnext.graph import GraphAdapter
    from test_graph import FakeSession, FakeTransaction
    async def run():
        graph = object.__new__(GraphAdapter)
        graph.settings = SimpleNamespace(graph_timeout=1, max_nodes=20, max_edges=50, max_bytes=1000)
        graph._session = lambda: FakeSession(FakeTransaction([{'value':'x'*200} for _ in range(20)]))
        first = await graph._retrieve('RETURN 1', {}, {'max_step_bytes':300})
        assert first['truncated'] and first['materialized_bytes'] <= 300
        graph._session = lambda: FakeSession(FakeTransaction([{'value':42}]))
        second = await graph._retrieve('RETURN 42', {}, {'used_bytes':first['materialized_bytes'], 'max_step_bytes':300})
        assert not second['truncated'] and second['rows']==[{'value':42}]
    asyncio.run(run())
