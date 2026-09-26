import importlib.util
from pathlib import Path

path=Path(__file__).resolve().parents[1]/'scripts/acceptance/workflow57.py'
spec=importlib.util.spec_from_file_location('workflow57',path)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def reference():
    return {'core':{'nodes':[{'id':'g'},{'id':'v'}],
        'edges':[{'type':'PART_OF_QTL_SIGNAL','start_id':'v','end_id':'g',
                  'properties':{'credible_set':'correct'}}]},
        'extra':{'nodes':[{'id':'optional'}],'edges':[]}}


def result(signal='correct', extra=True):
    return {'s':{'nodes':[{'id':'g'},{'id':'v'}]+([{'id':'optional'},{'id':'other'}] if extra else []),
        'edges':[{'type':'PART_OF_QTL_SIGNAL','start_id':'v','end_id':'g',
                  'properties':{'credible_set':signal}}]}}


def test_supported_supersets_are_not_exact_set_failures():
    scored=module.coverage(reference(),result())
    assert scored['core_covered']
    assert scored['extra_nodes_retrieved']==1
    assert scored['unlisted_nodes']==1


def test_wrong_signal_does_not_pass_matching_endpoints():
    assert not module.coverage(reference(),result('different'))['core_covered']


def test_missing_edge_cannot_pass_with_nodes_only():
    actual=result();actual['s']['edges']=[]
    assert not module.coverage(reference(),actual)['core_covered']


def test_empty_reference_is_not_automatic_success():
    ref={'core':{'nodes':[],'edges':[]},'extra':{'nodes':[],'edges':[]}}
    assert not module.coverage(ref,{})['core_covered']
