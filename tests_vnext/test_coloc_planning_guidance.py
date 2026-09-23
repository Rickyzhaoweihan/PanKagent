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
