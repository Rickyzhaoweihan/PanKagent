from copy import deepcopy
from pankagent_vnext.agent_schemas import active_pack
from pankagent_vnext.agent_schemas.views import temporary_comment_text


def test_lead_note_reaches_both_planners_and_formatter():
    from pankagent_vnext.llm import PLAN_SYSTEM, ANSWER_CONTRACT
    from pankagent_vnext.planning_contract import SYSTEM
    sem = active_pack().module('semantic_interpretation')
    text = temporary_comment_text(sem, 'PanKgraph_08_04')
    assert text and all(text in prompt for prompt in (PLAN_SYSTEM, SYSTEM, ANSWER_CONTRACT))
    graph = active_pack().module('graph_storage')
    for relation in ('PART_OF_QTL_SIGNAL', 'PART_OF_GWAS_SIGNAL'):
        assert text in graph['relationship_guidance'][relation]
    assert text not in graph['relationship_guidance']['GENE_DETECTED_IN']


def test_comment_does_not_leak_to_future_release_or_alter_graph_observations():
    pack = active_pack()
    sem = pack.module('semantic_interpretation')
    before = deepcopy(sem)
    assert temporary_comment_text(sem, 'future-release') == ''
    assert sem == before
    note = sem['temporary_comments'][0]
    assert note['remove_when']
    # Explicit nonlead observations remain intact, even with the import convention.
    db = pack.module('database_schema')
    import json
    assert 'nonlead' in json.dumps(db['relationships']['PART_OF_GWAS_SIGNAL'])
