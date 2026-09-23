from pankagent_vnext.coloc_planning_guidance import GUIDANCE
from pankagent_vnext.llm import PLAN_SYSTEM
from pankagent_vnext.planning_contract import SYSTEM


def test_grounded_and_timeout_fallback_share_coloc_contract():
    for prompt in (PLAN_SYSTEM, SYSTEM):
        assert GUIDANCE in prompt
        assert 'It is never Gene-to-variant' in prompt
        assert 'qtl_signal_id matches QTL credible_set' in prompt
        assert 'coloc_dataset-specific data_source and tissue_id' in prompt
        assert 'do not add gwas_lead_vars/qtl_lead_vars filters' in prompt


async def test_signal_alias_resolves_live_id_and_preserves_user_literal():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pankagent_vnext.graph import GraphAdapter
    graph = object.__new__(GraphAdapter)
    graph.settings = SimpleNamespace(graph_version='PanKgraph_08_04', graph_timeout=3)
    graph.release_labels = {'disease'}
    graph.preview_identity = lambda: {'release': 'PanKgraph_08_04'}
    graph._small_query = AsyncMock(return_value=[{'id': 'MONDO_0005147',
                                  'name': 'type 1 diabetes', 'labels': ['disease']}])
    constraint = {'entity_type': 'disease', 'property': 'name', 'operator': '=', 'value': 'T1D'}
    resolved = await graph._resolve_constraint(constraint, 0, {'relation_types': ['SIGNAL_COLOC_WITH']})
    assert resolved['state'] == 'resolved'
    assert resolved['id'] == 'MONDO_0005147'
    assert resolved['requested'] == constraint
    assert resolved['verified_signal_alias'] is True
    assert graph._small_query.call_args.args[1] == {'value': 'MONDO_0005147'}
