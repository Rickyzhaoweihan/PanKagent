"""The benchmark manifest and evaluator must not turn missing data into success."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('workflow50',REPO/'scripts/acceptance/workflow50.py')
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_manifest_covers_every_current_example_and_parent_context():
    manifest = json.loads((REPO/'tests_vnext/fixtures/acceptance/workflow50.json').read_text())
    cases = manifest['cases']
    assert len(cases) == len({c['key'] for c in cases}) == 50
    current = json.loads((REPO/'tests_vnext/fixtures/acceptance/frontend.json').read_text())
    assert {c['question'] for c in current['cases']} == {c['question'] for c in cases if c['group']=='current_examples'}
    seen = set()
    for case in cases:
        assert not case.get('parent') or case['parent'] in seen
        assert case['source'] and case['review_requirements']
        assert case['checks'] or case.get('assessment_mode')
        seen.add(case['key'])


def test_empty_without_executed_required_relation_is_not_pass():
    case = {'checks':[{'name':'q','comparison':'exact','requires_relation':'GENE_DETECTED_IN',
                     'selector':{'mode':'nodes','entity_type':'Gene'}}]}
    frozen = {'checks':[{'ok':True,'ids':[]}]}
    assert benchmark.evaluate(case,frozen,{'plan':{'steps':[]}})['exact_reference_pass'] is False


def test_typed_edge_selector_ignores_unrequested_context():
    selector = {'mode':'edge_endpoint','relation':'GENE_DETECTED_IN','endpoint':'end_id',
                'anchor_endpoint':'start_id','anchor_id':'INS','entity_type':'anatomical_structure'}
    result = {'s':{'nodes':[{'id':'b','labels':['anatomical_structure']}, {'id':'x','labels':['Gene']}],
                   'edges':[{'type':'GENE_DETECTED_IN','start_id':'INS','end_id':'b'},
                            {'type':'GENE_DETECTED_IN','start_id':'INS','end_id':'x'}]}}
    assert benchmark.extract(selector,result) == {'b'}


def test_matched_latency_excludes_unmatched_failures_but_cost_keeps_them():
    rows = [{'case':'a','arm':arm,'ready':True,'first_preview_s':seconds,'settled_preview_s':seconds,
             'accounting':{'settled_cost_usd':1}} for arm,seconds in [('original',2),('candidate',3)]]
    rows.append({'case':'b','arm':'candidate','ready':False,'accounting':{'settled_cost_usd':2}})
    summary = benchmark.summarize(rows)
    assert summary['matched_successes']['first_preview_s']['n'] == 1
    assert summary['candidate']['cost_usd_all_attempts'] == 3
