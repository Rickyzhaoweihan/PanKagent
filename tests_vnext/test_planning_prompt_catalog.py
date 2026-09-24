from pankagent_vnext.planning_prompt_catalog import MODULES, MODALITIES, GUIDANCE
from pankagent_vnext.llm import PLAN_SYSTEM
from pankagent_vnext.planning_contract import SYSTEM


def test_both_planning_routes_load_same_modules():
    assert {'colocalization', 'donor_samples', 'sample_modalities', 'identity_and_preparation', 'cohort_clinical_fields'} == {name for name, _ in MODULES}
    assert GUIDANCE in PLAN_SYSTEM and GUIDANCE in SYSTEM
    assert '"target_role":"source"' in GUIDANCE
    assert 'Count DISTINCT s.id' in GUIDANCE
    assert 'Formatter samples' in GUIDANCE


def test_modalities_are_hints_with_runtime_validation():
    assert next(m for m in MODALITIES if m['label']=='scRNA-seq')['aliases'][0]=='scRNAseq'
    assert 'not authorization or inventory proof' in GUIDANCE
    assert 'separately reviewed semantic rule' in GUIDANCE
