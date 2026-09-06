"""Execute the shipped demo's SSE and snapshot handlers against a minimal DOM."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


HTML = Path(__file__).resolve().parents[1] / "pankagent_vnext" / "assets" / "index.html"
NODE = shutil.which("node") or shutil.which("nodejs")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is needed for source-driven demo tests")

DOM = r"""
class DomNode {
  constructor(tag,kind=1,text=''){this.tag=tag;this.nodeType=kind;this.value=text;this.children=[];this.attributes={};this.style={};this.className='';this.hidden=false;this.checked=true;this.listeners={};this.classList={toggle(){}};}
  append(...items){for(let item of items){if(typeof item==='string')item=new DomNode('#text',3,item);if(item.nodeType===11){this.children.push(...item.children);item.children=[];}else this.children.push(item);}}
  prepend(...items){this.children.unshift(...items);}
  replaceChildren(...items){this.children=[];this.append(...items);}
  set textContent(value){this.children=[];this.value='';if(String(value))this.children.push(new DomNode('#text',3,String(value)));}
  get textContent(){return this.nodeType===3?this.value:this.children.map(child=>child.textContent).join('');}
  set innerHTML(_){throw new Error('unsafe HTML sink');}
  set outerHTML(_){throw new Error('unsafe HTML sink');}
  insertAdjacentHTML(){throw new Error('unsafe HTML sink');}
  setAttribute(key,value){this.attributes[key]=String(value);}
  addEventListener(type,callback){(this.listeners[type] ||= []).push(callback);}
  async dispatch(type){await Promise.all((this.listeners[type] || []).map(callback=>callback({preventDefault(){}})));}
  focus(){}
}
const elements=new Map();
const document={getElementById:id=>{if(!elements.has(id))elements.set(id,new DomNode('DIV'));return elements.get(id);},querySelectorAll:()=>[],createElement:tag=>new DomNode(tag.toUpperCase()),createElementNS:(_,tag)=>new DomNode(tag),createTextNode:text=>new DomNode('#text',3,String(text)),createDocumentFragment:()=>new DomNode('#fragment',11)};
globalThis.fetch=()=>{throw new Error('No network permitted in this test');};
globalThis.EventSource=()=>{throw new Error('No live event stream permitted in this test');};
"""


def demo_actions(actions):
    script = re.search(r"<script>(.*?)</script>", HTML.read_text(), re.S).group(1)
    end = script.rfind("})();")
    assert end > 0
    script = script[:end] + "globalThis.demo={handle,renderSnapshot,renderPlan,resetView};\n" + script[end:]
    harness = r"""
const actions=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const read=id=>{const item=document.getElementById(id);return {text:item.textContent,className:item.className,hidden:item.hidden};};
process.stdout.write(JSON.stringify(actions.map(action=>{
  if(action.kind==='reset')demo.resetView();
  if(action.kind==='snapshot')demo.renderSnapshot(action.value);
  if(action.kind==='plan')demo.renderPlan(action.value);
  if(action.kind==='event')demo.handle({type:action.value.type,data:JSON.stringify(action.value)});
  return Object.fromEntries(['graph-badge','graph-notice','graph-counts','graph-answer','graph-evidence','steps','constraints','confirm-button'].map(id=>[id,read(id)]));
})));
"""
    result = subprocess.run(
        [NODE, "-e", DOM + script + harness], input=json.dumps(actions),
        text=True, capture_output=True, check=True, timeout=10,
    )
    return json.loads(result.stdout)


FAILED = {"completeness": "partial", "nodes": [], "edges": [], "steps": [
    {"step_id": "s1", "status": "failed", "nodes": [], "edges": [], "rows": [],
     "validation": [{"valid": False, "reasons": ["unrequested_predicate"]}]},
]}
EMPTY = {"completeness": "empty", "nodes": [], "edges": [], "steps": [{"status": "empty", "rows": []}]}
COMPLETE = {"completeness": "complete", "nodes": [{"id": "CFTR", "name": "CFTR"}], "edges": [], "steps": [{"status": "complete"}]}
PARTIAL = {**COMPLETE, "completeness": "partial", "steps": [{"status": "complete"}, {"status": "failed"}]}


@pytest.mark.parametrize("evidence,expected,tone", [
    (FAILED, "Retrieval failed", "badge error"),
    (EMPTY, "No matching evidence", "badge warn"),
    (PARTIAL, "Partial evidence", "badge warn"),
    (COMPLETE, "Evidence available", "badge"),
    ({"nodes": [], "edges": [], "completeness": "complete", "steps": [{"status": "complete", "rows": [{"count": 4}]}]}, "Evidence available", "badge"),
    ({**EMPTY, "completeness": "partial", "truncated": True, "delivery_status": "partial"}, "Partial evidence", "badge warn"),
])
@pytest.mark.parametrize("delivery", ["snapshot", "event"])
def test_badges_use_evidence_outcomes_for_snapshot_and_final_sse(evidence, expected, tone, delivery):
    if delivery == "snapshot":
        actions = [{"kind": "snapshot", "value": {"graph_answer": "Review the evidence.", "evidence": evidence, "status": "partial"}}]
    else:
        actions = [
            {"kind": "event", "value": {"type": "graph_answer", "sequence": 1, "payload": {"answer": "Review the evidence.", "evidence": evidence, "delta": False}}},
            {"kind": "event", "value": {"type": "terminal", "sequence": 2, "payload": {"status": "partial"}}},
        ]
    view = demo_actions(actions)[-1]
    assert view["graph-badge"] == {"text": expected, "className": tone, "hidden": False}
    assert view["graph-notice"]["hidden"] == (expected == "Evidence available")
    assert view["graph-answer"]["text"] == "Review the evidence."
    assert view["graph-evidence"]["hidden"] is False


def test_success_clears_stale_warning_and_missing_evidence_cannot_keep_green_badge():
    views = demo_actions([
        {"kind": "snapshot", "value": {"graph_answer": "Incomplete", "evidence": PARTIAL}},
        {"kind": "event", "value": {"type": "graph_answer", "sequence": 1, "payload": {"answer": "Complete", "evidence": COMPLETE, "delta": False}}},
        {"kind": "snapshot", "value": {"graph_answer": "No evidence supplied", "evidence": None}},
    ])
    assert views[0]["graph-notice"]["hidden"] is False
    assert views[1]["graph-notice"] == {"text": "", "className": "", "hidden": True}
    assert views[1]["graph-badge"]["text"] == "Evidence available"
    assert views[2]["graph-badge"]["text"] == "Evidence unavailable"
    assert views[2]["graph-evidence"]["hidden"] is True


def test_incremental_text_and_replayed_sequence_do_not_claim_evidence_success():
    views = demo_actions([
        {"kind": "reset"},
        {"kind": "event", "value": {"type": "graph_answer", "sequence": 1, "payload": {"text": "CFTR ", "delta": True}}},
        {"kind": "event", "value": {"type": "graph_answer", "sequence": 2, "payload": {"text": "[G1]", "delta": True}}},
        {"kind": "event", "value": {"type": "graph_answer", "sequence": 3, "payload": {"answer": "Retrieval failed", "evidence": FAILED, "delta": False}}},
        {"kind": "event", "value": {"type": "graph_answer", "sequence": 3, "payload": {"answer": "Duplicate must be ignored", "evidence": COMPLETE, "delta": False}}},
        {"kind": "snapshot", "value": {"graph_answer": "Reloaded failed result", "evidence": FAILED, "status": "partial"}},
    ])
    assert views[2]["graph-answer"]["text"] == "CFTR [G1]"
    assert views[2]["graph-badge"]["text"] == "Awaiting confirmation"
    assert views[3]["graph-badge"]["text"] == views[4]["graph-badge"]["text"] == views[5]["graph-badge"]["text"] == "Retrieval failed"
    assert views[4]["graph-answer"]["text"] == "Retrieval failed"


def test_plan_review_renders_each_steps_required_filters_without_html_execution():
    constraint = {"property": "target.name", "operator": "eq", "value": "ductal cell"}
    plan = {"interpreted_question": "CFTR in ductal cells", "steps": [
        {"id": "s1", "question": "Retrieve CFTR relations", "constraints": [constraint]},
        {"id": "s2", "question": "Inspect <img onerror=attack> neighbors", "constraints": []},
    ], "constraints": [{"property": "name", "operator": "eq", "value": "CFTR"}], "literature": False}
    views = demo_actions([{"kind": "snapshot", "value": {"status": "awaiting_confirmation", "plan": plan, "preview": {"status": "empty", "evidence": EMPTY}}}, {"kind": "snapshot", "value": {"status": "awaiting_confirmation", "plan": {**plan, "steps": [{"question": "No filters", "constraints": []}], "constraints": []}}}])
    assert "Focus on ductal cell" in views[0]["steps"]["text"]
    assert "target.name" not in views[0]["steps"]["text"] and '"property"' not in views[0]["steps"]["text"]
    assert "Inspect <img onerror=attack> neighbors" in views[0]["steps"]["text"]
    assert "CFTR" in views[0]["constraints"]["text"]
    assert views[0]["confirm-button"]["hidden"] is False
    assert "Focus on" not in views[1]["steps"]["text"]
    assert views[1]["constraints"]["text"] == ""
