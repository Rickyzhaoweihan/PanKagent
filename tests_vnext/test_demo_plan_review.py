"""Plan preparation, evidence preview, and revision use the actual browser script."""

import json
from html.parser import HTMLParser
import re
import subprocess

import pytest

from test_demo_status import DOM, HTML, NODE, COMPLETE, EMPTY, FAILED, PARTIAL


pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is needed for source-driven demo tests")


def exercise(actions, responses=()):
    script = re.search(r"<script>(.*?)</script>", HTML.read_text(), re.S).group(1)
    end = script.rfind("})();")
    script = script[:end] + "globalThis.demo={handle,renderSnapshot,resetView,connect};\n" + script[end:]
    harness = r"""
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8')),calls=[],streams=[],responses=[...input.responses];
globalThis.fetch=async(path,options)=>{calls.push({path,...options,body:options.body?JSON.parse(options.body):undefined});const response=responses.shift();if(!response)throw new Error('Unexpected mocked request: '+path);return {ok:response.ok!==false,status:response.status||200,json:async()=>response.value};};
globalThis.EventSource=class{constructor(url){this.url=url;this.closed=false;this.listeners={};streams.push(this);}addEventListener(type,callback){this.listeners[type]=callback;}close(){this.closed=true;}};
const ids=['stage','steps','constraints','resolved-entities','entity-resolution-note','preview-badge','preview-notice','preview-counts','preview-progress','preview-observations','preview-findings','preview-findings-table','preview-context','preview-evidence','preview-json','graph-answer','graph-evidence','graph-badge','review-controls','revision-question','revision-context','revise-button','confirm-button','replacement-link','error'];
const links=item=>[...(item.tag==='A'?[{text:item.textContent,href:item.href,rel:item.rel}]:[]),...item.children.flatMap(links)];
const read=id=>{const item=document.getElementById(id);return {text:item.textContent,className:item.className,hidden:item.hidden,value:item.value,disabled:!!item.disabled,checked:item.checked,href:item.href,links:links(item)};};
(async()=>{const views=[];for(const action of input.actions){
 if(action.kind==='reset')demo.resetView();
 if(action.kind==='snapshot')demo.renderSnapshot(action.value);
 if(action.kind==='event')demo.handle({type:action.value.type,data:JSON.stringify(action.value)});
 if(action.kind==='input'){const field=document.getElementById(action.id);field.value=action.value;await field.dispatch('input');}
 if(action.kind==='context'){const field=document.getElementById(action.id);field.checked=action.value;await field.dispatch('change');}
 if(action.kind==='click')await document.getElementById(action.id).dispatch('click');
 if(action.kind==='submit')await document.getElementById(action.id).dispatch('submit');
 if(action.kind==='connect')demo.connect();
 if(action.kind==='double-click')await Promise.all([document.getElementById(action.id).dispatch('click'),document.getElementById(action.id).dispatch('click')]);
 await Promise.resolve();views.push(Object.fromEntries(ids.map(id=>[id,read(id)])));
 }process.stdout.write(JSON.stringify({views,calls,streams:streams.map(stream=>({url:stream.url,closed:stream.closed}))}));})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
"""
    result = subprocess.run([NODE, "-e", DOM + script + harness],
                            input=json.dumps({"actions": actions, "responses": list(responses)}),
                            text=True, capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


NODES = [
    {"id": "ENSG00000001626", "labels": ["Gene"], "properties": {"name": "CFTR"}},
    {"id": "CL_0002079", "labels": ["anatomical_structure"], "properties": {"name": "ductal cell"}},
]
EDGE = {"start_id": "ENSG00000001626", "end_id": "CL_0002079", "type": "GENE_ENRICHED_IN", "properties": {
    "log2_fold_change": 7.62294, "padj": 2.7e-195, "rank_in_cell_type": 1,
    "median_pct_cells_expressing": 87.3122, "condition": "ND", "data_source": "https://example.test/source.pdf",
}}
EVIDENCE = {"completeness": "complete", "nodes": NODES, "edges": [EDGE], "steps": [
    {"step_id": "s1", "status": "complete", "nodes": NODES, "edges": [EDGE], "rows": [], "validation": [{"valid": True}]},
]}
PLAN = {"interpreted_question": "Is CFTR enriched in ductal cells?", "include_context": True,
        "steps": [{"id": "s1", "title": "Check CFTR enrichment in ductal cells", "rationale": "Inspect the recorded enrichment and adjusted P value.",
                   "question": "Is CFTR (gene) specifically enriched in ductal cells (GENE_ENRICHED_IN relation)?",
                   "constraints": [{"property": "name", "operator": "=", "value": "CFTR"}, {"property": "name", "operator": "=", "value": "ductal cell"}],
                   "resolved_entities": [{"state": "resolved", "id": node["id"], "name": node["properties"]["name"], "labels": node["labels"]} for node in NODES]}],
        "literature": False, "clarification": None}


def snapshot(plan=PLAN, evidence=EVIDENCE, **changes):
    return {"run_id": "old-run", "plan_id": "old-plan", "session_id": "same-session", "question": "Is CFTR enriched in ductal cells?",
            "status": "awaiting_confirmation", "plan": plan,
            "preview": {"status": "complete", "evidence": evidence, "created_at": "2026-01-01T00:00:00Z"}, **changes}


def test_initial_preview_has_actual_measurements_sources_names_and_separate_final_answer():
    result = exercise([{"kind": "snapshot", "value": snapshot()}])
    view = result["views"][0]
    assert "Check CFTR enrichment in ductal cells" in view["steps"]["text"]
    assert "Inspect the recorded enrichment" in view["steps"]["text"]
    assert "Focus on ductal cell" in view["steps"]["text"] and '"property"' not in view["steps"]["text"]
    assert "CFTR" in view["resolved-entities"]["text"] and "CL_0002079" in view["resolved-entities"]["text"]
    finding = view["preview-findings-table"]["text"]
    for value in ("CFTR", "ductal cell", "Enrichment", "Non-diabetic", "Log₂ fold change: 7.623", "Adjusted P value: 2.7e-195", "Rank among genes in this cell type: 1", "87.31"):
        assert value in finding
    assert view["preview-findings-table"]["links"] == [{"text": "Source", "href": "https://example.test/source.pdf", "rel": "noopener noreferrer"}]
    assert "7.62294" in view["preview-json"]["text"] and "87.3122" in view["preview-json"]["text"]
    assert "Main question" in finding
    assert view["preview-badge"]["text"] == "Evidence available"
    assert view["preview-evidence"]["hidden"] is False
    assert view["graph-answer"]["text"] == ""
    assert view["review-controls"]["hidden"] is False
    assert view["revision-question"]["value"] == "Is CFTR enriched in ductal cells?"
    assert result["calls"] == []


@pytest.mark.parametrize("evidence,expected", [(FAILED, "Retrieval failed"), (EMPTY, "No matching evidence"), (PARTIAL, "Partial evidence")])
def test_failed_empty_and_partial_previews_remain_visible_and_can_be_revised(evidence, expected):
    view = exercise([{"kind": "snapshot", "value": snapshot(evidence=evidence)}])["views"][0]
    assert view["preview-badge"]["text"] == expected
    assert view["preview-notice"]["hidden"] is False
    assert view["preview-evidence"]["hidden"] is False
    assert view["review-controls"]["hidden"] is False
    assert view["revise-button"]["disabled"] is False


def test_progressive_preview_does_not_claim_plan_is_ready_or_enable_confirmation():
    views = exercise([
        {"kind": "reset"},
        {"kind": "event", "value": {"type": "preview_step", "status": "planning", "stage": "querying_graph", "sequence": 1,
         "payload": {"step_id": "s1", "evidence": EVIDENCE["steps"][0], "preview": {"status": "partial", "evidence": EVIDENCE}}}},
        {"kind": "event", "value": {"type": "plan_ready", "status": "awaiting_confirmation", "sequence": 2,
         "payload": {"plan_id": "old-plan", "plan": PLAN, "preview": {"status": "complete", "evidence": EVIDENCE}}}},
        {"kind": "snapshot", "value": snapshot(status="cancelled", preview={"status": "partial", "evidence": PARTIAL, "pending_step_ids": ["s2"]})},
    ])["views"]
    assert "still running" in views[1]["preview-progress"]["text"]
    assert views[1]["review-controls"]["hidden"] is True
    assert views[2]["review-controls"]["hidden"] is False
    assert "Main question" in views[2]["preview-findings-table"]["text"]
    assert "did not finish" in views[3]["preview-progress"]["text"]
    assert views[3]["review-controls"]["hidden"] is True


def test_context_and_ambiguous_entities_are_not_presented_as_primary_verified_evidence():
    context = {**PLAN["steps"][0], "id": "context1", "purpose": "context", "context_for": "s1", "title": "Inspect related detection evidence", "rationale": "Compare a nearby evidence type without expanding the main question.",
               "resolved_entities": [{"state": "ambiguous", "requested": {"value": "cell"}, "candidates": [{"name": "alpha cell"}, {"name": "beta cell"}]}]}
    plan = {**PLAN, "steps": [PLAN["steps"][0], context], "clarification": "Please specify the cell type."}
    evidence = {**EVIDENCE, "steps": [EVIDENCE["steps"][0], {**EVIDENCE["steps"][0], "step_id": "context1"}]}
    view = exercise([{"kind": "snapshot", "value": snapshot(plan, evidence)}])["views"][0]
    assert "RELATED CONTEXT" in view["steps"]["text"]
    assert "Related context" in view["preview-findings-table"]["text"]
    assert "preliminary context, not proof of a mechanism" in view["preview-context"]["text"]
    assert "alpha cell" not in view["resolved-entities"]["text"]
    assert "alpha cell" in view["entity-resolution-note"]["text"]
    assert view["confirm-button"]["hidden"] is True
    assert view["revise-button"]["disabled"] is False


@pytest.mark.parametrize("state", ["ambiguous", "not_found"])
def test_unresolved_entity_requires_clarification_without_claiming_empty_biological_evidence(state):
    unresolved = {"state": state, "requested": {"value": "ductal"}, "candidates": [
        {"name": "ductal cell", "id": "CL_0002079"},
    ] if state == "ambiguous" else []}
    plan = {**PLAN, "clarification": "Specify the intended cell type.", "steps": [
        {**PLAN["steps"][0], "resolved_entities": [unresolved]},
    ]}
    view = exercise([{"kind": "snapshot", "value": snapshot(plan=plan, preview={"status": "not_requested", "evidence": {}})}])["views"][0]
    assert view["preview-badge"]["text"] == "Clarification needed"
    assert "Resolve the indicated entity before checking graph evidence" in view["preview-notice"]["text"]
    assert "has not been checked" in view["preview-progress"]["text"]
    assert view["confirm-button"]["hidden"] is True
    assert view["revise-button"]["disabled"] is False
    assert view["preview-evidence"]["hidden"] is True
    if state == "ambiguous":
        assert "ductal cell (CL_0002079)" in view["entity-resolution-note"]["text"]


def test_revision_posts_once_keeps_session_and_ignores_old_run_events():
    revised = "Is CFTR detected in beta cells?"
    new = snapshot(run_id="new-run", plan_id="new-plan", question=revised, plan=None, preview=None, status="planning", include_context=False)
    result = exercise([
        {"kind": "snapshot", "value": snapshot()},
        {"kind": "input", "id": "revision-question", "value": revised},
        {"kind": "context", "id": "revision-context", "value": False},
        {"kind": "double-click", "id": "revise-button"},
        {"kind": "event", "value": {"run_id": "old-run", "type": "terminal", "sequence": 99, "payload": {"status": "superseded"}}},
    ], responses=[
        {"value": {"run_id": "new-run", "plan_id": "new-plan", "session_id": "same-session", "status": "planning", "events_url": "/v2/runs/new-run/events"}},
        {"value": new},
    ])
    assert result["views"][1]["confirm-button"]["disabled"] is True
    assert [(call["path"], call["method"]) for call in result["calls"]] == [("/v2/plans/old-plan/revise", "POST"), ("/v2/runs/new-run", "GET")]
    assert result["calls"][0]["body"] == {"question": revised, "include_context": False}
    assert result["streams"] == [{"url": "/v2/runs/new-run/events", "closed": False}]
    assert result["views"][-1]["revision-question"]["value"] == revised
    assert result["views"][-1]["preview-evidence"]["hidden"] is True
    assert result["views"][-1]["review-controls"]["hidden"] is True


def test_reconnect_preserves_edited_revision_and_restores_saved_preview_without_model_calls():
    result = exercise([
        {"kind": "snapshot", "value": snapshot()},
        {"kind": "input", "id": "revision-question", "value": "My revised question"},
        {"kind": "snapshot", "value": snapshot()},
        {"kind": "snapshot", "value": snapshot(status="running")},
    ])
    assert result["views"][2]["revision-question"]["value"] == "My revised question"
    assert result["views"][2]["confirm-button"]["disabled"] is True
    assert result["views"][2]["preview-findings"]["hidden"] is False
    assert result["views"][3]["review-controls"]["hidden"] is True
    assert result["calls"] == []


def test_failed_revision_retains_original_preview_and_reenables_revision_controls():
    result = exercise([
        {"kind": "snapshot", "value": snapshot()},
        {"kind": "input", "id": "revision-question", "value": "New question"},
        {"kind": "click", "id": "revise-button"},
    ], responses=[{"ok": False, "status": 409, "value": {"detail": "The plan can no longer be revised."}}])
    view = result["views"][-1]
    assert view["preview-findings"]["hidden"] is False
    assert view["error"]["text"] == "The plan can no longer be revised."
    assert view["revise-button"]["disabled"] is False
    assert view["confirm-button"]["disabled"] is True


def test_snapshot_cursor_reconnect_skips_old_answer_deltas_and_stale_snapshots():
    result = exercise([
        {"kind": "snapshot", "value": snapshot(status="running", graph_answer="CFTR", event_sequence=8)},
        {"kind": "connect"},
        {"kind": "event", "value": {"run_id": "old-run", "type": "graph_answer", "sequence": 7, "payload": {"delta": True, "text": "duplicated"}}},
        {"kind": "event", "value": {"run_id": "old-run", "type": "graph_answer", "sequence": 9, "payload": {"delta": True, "text": " [G1]"}}},
        {"kind": "snapshot", "value": snapshot(status="awaiting_confirmation", graph_answer="stale", event_sequence=8)},
    ])
    assert result["streams"] == [{"url": "/v2/runs/old-run/events?after=8", "closed": False}]
    assert result["views"][-1]["graph-answer"]["text"] == "CFTR [G1]"
    assert result["views"][-1]["review-controls"]["hidden"] is True
    assert result["calls"] == []


def test_confirm_uses_existing_plan_once_and_keeps_preliminary_evidence_visible():
    result = exercise([
        {"kind": "snapshot", "value": snapshot()},
        {"kind": "double-click", "id": "confirm-button"},
    ], responses=[{"value": {"run_id": "old-run", "status": "queued"}}])
    assert len(result["calls"]) == 1
    assert result["calls"][0]["path"] == "/v2/plans/old-plan/confirm"
    assert result["views"][-1]["review-controls"]["hidden"] is True
    assert result["views"][-1]["preview-findings"]["hidden"] is False
    assert result["views"][-1]["graph-answer"]["text"] == ""


def test_superseded_snapshot_cannot_be_confirmed_and_links_only_to_replacement():
    result = exercise([
        {"kind": "snapshot", "value": snapshot(status="superseded", replacement_run_id="new-run")},
        {"kind": "click", "id": "confirm-button"},
        {"kind": "click", "id": "revise-button"},
    ])
    view = result["views"][-1]
    assert view["review-controls"]["hidden"] is True
    assert view["replacement-link"]["href"] == "?run=new-run"
    assert result["calls"] == []


def test_initial_form_sends_context_preference_and_condition_filters_use_prose():
    result = exercise([
        {"kind": "input", "id": "question", "value": "Is CFTR detected in ductal cells?"},
        {"kind": "context", "id": "include-context", "value": False},
        {"kind": "submit", "id": "question-form"},
        {"kind": "snapshot", "value": snapshot(plan={**PLAN, "constraints": [{"property": "condition", "operator": "IN", "value": '["ND","T1D"]'}]})},
    ], responses=[
        {"value": {"run_id": "old-run", "plan_id": "old-plan", "session_id": "same-session", "status": "planning", "events_url": "/v2/runs/old-run/events"}},
        {"value": snapshot(plan=None, preview=None, status="planning", include_context=False)},
    ])
    assert result["calls"][0]["path"] == "/v2/plans"
    assert result["calls"][0]["body"] == {"question": "Is CFTR detected in ductal cells?", "include_context": False}
    assert result["views"][-1]["constraints"]["text"] == "Condition is one of Non-diabetic, Type 1 diabetes"


def test_legacy_saved_plan_requires_revision_without_calling_models_on_reload():
    result = exercise([
        {"kind": "snapshot", "value": snapshot(preview=None)},
        {"kind": "click", "id": "confirm-button"},
        {"kind": "snapshot", "value": snapshot(preview=None)},
        {"kind": "snapshot", "value": snapshot(evidence=FAILED)},
    ])
    legacy = result["views"][2]
    assert legacy["preview-notice"]["text"] == "This saved plan needs an initial evidence check. Revise the plan to continue."
    assert legacy["confirm-button"]["hidden"] is True
    assert legacy["revision-question"]["value"] == "Is CFTR enriched in ductal cells?"
    assert legacy["revise-button"]["disabled"] is False
    assert legacy["review-controls"]["hidden"] is False
    assert result["calls"] == []
    assert result["views"][-1]["confirm-button"]["hidden"] is False
    assert result["views"][-1]["preview-badge"]["text"] == "Retrieval failed"


def test_preview_stages_use_biological_review_wording():
    result = exercise([
        {"kind": "event", "value": {"type": "progress", "sequence": 1, "status": "planning", "stage": "preparing_preview"}},
        {"kind": "event", "value": {"type": "progress", "sequence": 2, "status": "running", "stage": "reusing_preview"}},
    ])
    assert result["views"][0]["stage"]["text"] == "Checking initial graph evidence"
    assert result["views"][1]["stage"]["text"] == "Using the checked graph evidence"


def test_execution_preparation_does_not_imply_a_new_query_is_being_generated():
    result = exercise([
        {"kind": "event", "value": {"type": "progress", "sequence": 1, "status": "running", "stage": "preparing_execution"}},
    ])
    assert result["views"][0]["stage"]["text"] == "Preparing the investigation"


def test_summary_metrics_are_selected_by_evidence_type_not_property_order():
    enrichment = {**EDGE, "properties": {**{f"unrelated_property_{index}": index for index in range(25)}, **EDGE["properties"]}}
    detection = {**EDGE, "type": "GENE_DETECTED_IN", "properties": {
        **{f"unrelated_property_{index}": index for index in range(25)},
        "expression_call": "detected", "median_donor_log_cpm": 2.1234567,
        "median_pct_cells_expressing": 81.23456, "total_cells": 33686,
        "condition": "ALL", "source": "javascript:alert(1)",
    }}
    evidence = {**EVIDENCE, "edges": [enrichment, detection], "steps": [
        {**EVIDENCE["steps"][0], "edges": [enrichment, detection]},
    ]}
    view = exercise([{"kind": "snapshot", "value": snapshot(evidence=evidence)}])["views"][0]
    summary = view["preview-findings-table"]["text"]
    assert "unrelated_property" not in summary and "Unrelated property" not in summary
    for expected in ("Log₂ fold change: 7.623", "Adjusted P value: 2.7e-195", "Expression detected", "Expression call: detected", "Median donor log CPM: 2.123", "Median percent of cells expressing: 81.23", "Total cells: 33686", "ALL (as recorded)"):
        assert expected in summary
    assert all(link["href"].startswith("https://") for link in view["preview-findings-table"]["links"])
    assert "2.1234567" in view["preview-json"]["text"]


def test_full_graph_tables_start_collapsed_while_preview_summary_and_diagram_stay_visible():
    class Structure(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.ancestors, self.details = [], {}, {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            identity = attrs.get("id")
            if identity:
                self.ancestors[identity] = [entry[1] for entry in self.stack]
            if tag == "details" and identity:
                self.details[identity] = attrs
            if tag not in {"meta", "input", "link", "br", "hr", "img"}:
                self.stack.append((tag, identity))

        def handle_endtag(self, tag):
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break

    parsed = Structure()
    parsed.feed(HTML.read_text())
    assert "open" not in parsed.details["preview-all-details"]
    for identity in ("preview-nodes", "preview-edges", "preview-records"):
        assert "preview-all-details" in parsed.ancestors[identity]
    for identity in ("preview-findings-table", "preview-graph", "review-controls"):
        assert "preview-all-details" not in parsed.ancestors[identity]
