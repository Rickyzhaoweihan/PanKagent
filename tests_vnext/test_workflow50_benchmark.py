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


def test_followup_enables_context_and_requires_the_named_parent():
    import pytest
    case = {'question':'For those donors?', 'parent':'stage-case'}
    with pytest.raises(ValueError):
        benchmark.plan_request(case)
    request = benchmark.plan_request(case,{'session_id':'parent-session'})
    assert request['session_id'] == 'parent-session'
    assert request['include_context'] is True
    assert benchmark.plan_request({'question':'Independent question'})['include_context'] is False


def test_predeclared_clarification_is_not_a_failed_membership_query():
    case = {'checks':[], 'expected_clarification':True}
    result = benchmark.evaluate(case,{'checks':[]},{'plan':{'steps':[],'clarification':'Did you mean HPAP?'}})
    assert result['exact_reference_pass'] is None
    assert result['expected_clarification_pass'] is True
    failed = {'plan':{'steps':[],'clarification':'We could not prepare executable search steps.'},
              'error':{'category':'planning_failure'}}
    assert benchmark.evaluate(case,{'checks':[]},failed)['expected_clarification_pass'] is False


def test_typed_edge_selector_ignores_unrequested_context():
    selector = {'mode':'edge_endpoint','relation':'GENE_DETECTED_IN','endpoint':'end_id',
                'anchor_endpoint':'start_id','anchor_id':'INS','entity_type':'anatomical_structure'}
    result = {'s':{'nodes':[{'id':'b','labels':['anatomical_structure']}, {'id':'x','labels':['Gene']}],
                   'edges':[{'type':'GENE_DETECTED_IN','start_id':'INS','end_id':'b'},
                            {'type':'GENE_DETECTED_IN','start_id':'INS','end_id':'x'}]}}
    assert benchmark.extract(selector,result) == {'b'}


def test_matched_latency_excludes_unmatched_failures_but_cost_keeps_them():
    rows = [{'case':'a','arm':arm,'ready':True,'settled_eligible':True,'first_preview_s':seconds,'settled_preview_s':seconds,
             'accounting':{'settled_cost_usd':1}} for arm,seconds in [('original',2),('candidate',3)]]
    rows.append({'case':'b','arm':'candidate','ready':False,'accounting':{'settled_cost_usd':2}})
    summary = benchmark.summarize(rows)
    assert summary['matched_successes']['first_preview_s']['n'] == 1
    assert summary['candidate']['cost_usd_all_attempts'] == 3


def test_report_reconciles_failed_confirmation_without_rewriting_raw_artifact(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules,'workflow50',benchmark)
    report_spec = importlib.util.spec_from_file_location('workflow50_report',REPO/'scripts/acceptance/workflow50_report.py')
    reporter = importlib.util.module_from_spec(report_spec)
    report_spec.loader.exec_module(reporter)
    case = {'key':'q','question':'question','checks':[],'group':'test','review_requirements':[]}
    benchmark.dump(tmp_path/'run-manifest.json',{'manifest':{'cases':[case]}})
    benchmark.dump(tmp_path/'references.json',{'cases':{'q':{'checks':[]}}})
    benchmark.dump(tmp_path/'summary.json',{'budget':{}})
    row = {'case':'q','arm':'candidate','ready':True,'settled_eligible':True,
           'confirm_http_status':409,'status':'awaiting_confirmation','first_preview_s':2,'settled_preview_s':3}
    (tmp_path/'report.jsonl').write_text(json.dumps(row)+'\n')
    (tmp_path/'candidate').mkdir()
    artifact = {'run':{'plan':{'steps':[]},'status':'awaiting_confirmation'},
                'events':[{'type':'terminal','payload':{'status':'failed','error':{'category':'preview_revalidation_required'}}}]}
    benchmark.dump(tmp_path/'candidate/q.json',artifact)
    assert reporter.build(tmp_path,tmp_path/'comparison.html') == 1
    summary = json.loads((tmp_path/'scored-summary.json').read_text())['arms']
    assert summary['candidate']['ready'] == 1
    assert summary['candidate']['settled_eligible'] == 0
    assert summary['matched_successes']['first_preview_s']['n'] == 0
    assert json.loads((tmp_path/'scored-cases.json').read_text())[0]['status'] == 'failed'
    assert json.loads((tmp_path/'candidate/q.json').read_text()) == artifact
