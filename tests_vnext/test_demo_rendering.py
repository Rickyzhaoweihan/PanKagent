"""Execute the shipped DOM-only renderer in Node; no browser/network dependency."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


HTML = Path(__file__).resolve().parents[1] / "pankagent_vnext" / "assets" / "index.html"
NODE = shutil.which("node") or shutil.which("nodejs")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is needed for source-driven JavaScript renderer tests")


# The double implements DOM construction and fragment movement, not Markdown.
# Dangerous HTML sinks deliberately throw so a renderer regression fails tests.
DOM = r"""
class DomNode {
  constructor(tag,kind=1,text=''){this.tag=tag;this.nodeType=kind;this.value=text;this.children=[];this.attributes={};this.style={};}
  append(...items){for(let item of items){if(typeof item==='string')item=new DomNode('#text',3,item);if(item.nodeType===11){this.children.push(...item.children);item.children=[];}else this.children.push(item);}}
  replaceChildren(...items){this.children=[];this.append(...items);}
  set textContent(value){this.children=[];this.value='';if(String(value))this.children.push(new DomNode('#text',3,String(value)));}
  get textContent(){return this.nodeType===3?this.value:this.children.map(child=>child.textContent).join('');}
  set innerHTML(_){throw new Error('unsafe HTML sink');}
  set outerHTML(_){throw new Error('unsafe HTML sink');}
  insertAdjacentHTML(){throw new Error('unsafe HTML sink');}
  setAttribute(key,value){this.attributes[key]=String(value);}
}
const document={createElement:tag=>new DomNode(tag.toUpperCase()),createTextNode:text=>new DomNode('#text',3,String(text)),createDocumentFragment:()=>new DomNode('#fragment',11)};
function snapshot(item){return {tag:item.tag,text:item.textContent,attributes:item.attributes,style:item.style,href:item.href,rel:item.rel,target:item.target,start:item.start,tabIndex:item.tabIndex,children:item.children.map(snapshot)};}
"""


def render_many(values):
    html = HTML.read_text()
    renderer = re.search(r"// BEGIN SAFE ANSWER RENDERER\n(.*?)// END SAFE ANSWER RENDERER", html, re.S).group(1)
    harness = "const values=JSON.parse(require('node:fs').readFileSync(0,'utf8'));const root=document.createElement('div');process.stdout.write(JSON.stringify(values.map(value=>{renderAnswer(root,value);return snapshot(root);})));"
    result = subprocess.run([NODE, "-e", DOM + renderer + harness], input=json.dumps(values), text=True, capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


def descendants(tree, tag):
    found = [tree] if tree["tag"] == tag else []
    for child in tree["children"]:
        found.extend(descendants(child, tag))
    return found


def test_legacy_scientific_sections_and_paragraphs():
    answer = "Answer\nINS has returned graph evidence [G1].\n\nGene overview\n**INS** is recorded as a gene.\n\nQTL overview:\nNo data available.\n\n**Specific relation to Type 1 Diabetes**\nEvidence remains limited [G2]."
    tree = render_many([answer])[0]
    assert [node["text"] for node in descendants(tree, "H3")] == ["Answer", "Gene overview", "QTL overview", "Specific relation to Type 1 Diabetes"]
    assert len(descendants(tree, "P")) == 4
    assert descendants(tree, "STRONG")[0]["text"] == "INS"
    assert "[G1]" in tree["text"] and "[G2]" in tree["text"]


def test_markdown_headings_lists_and_nested_items_preserve_ids():
    tree = render_many(["# Evidence summary\n\n- INS [G1]\n  - MONDO_0005147 and ENSG_00000001\n- GCG [G2]\n\n3. Review graph evidence.\n4. Review limitations.\n\n## Limitations\nAbsence of data is not biological absence."])[0]
    assert descendants(tree, "H3")[0]["text"] == "Evidence summary"
    assert descendants(tree, "H4")[0]["text"] == "Limitations"
    assert len(descendants(tree, "UL")) == 2
    assert descendants(tree, "OL")[0]["start"] == 3
    assert "MONDO_0005147 and ENSG_00000001" in tree["text"]
    assert not descendants(tree, "EM")


def test_tables_keep_alignment_escaped_pipes_and_citations():
    tree = render_many(["| Gene | Signal | Evidence |\n| :--- | :---: | ---: |\n| **INS** | `x|y` | [G1] |\n| GCG | A\\|B | 0.02 [G2] |"])[0]
    assert len(descendants(tree, "TABLE")) == 1
    assert [node["text"] for node in descendants(tree, "TH")] == ["Gene", "Signal", "Evidence"]
    assert [node["style"]["textAlign"] for node in descendants(tree, "TH")] == ["left", "center", "right"]
    assert [node["text"] for node in descendants(tree, "TD")] == ["INS", "x|y", "[G1]", "GCG", "A|B", "0.02 [G2]"]
    wrap = tree["children"][0]
    assert wrap["attributes"] == {"role": "region", "aria-label": "Answer table"}
    assert wrap["tabIndex"] == 0


def test_streamed_partial_markers_and_tables_reconcile_without_duplicate_text():
    stages = ["## Gene over", "## Gene overview\n\n**INS", "## Gene overview\n\n**INS** [G", "## Gene overview\n\n**INS** [G1].\n\n| Gene | Count |\n| --- |", "## Gene overview\n\n**INS** [G1].\n\n| Gene | Count |\n| --- | --- |\n| INS | 2 |"]
    trees = render_many(stages)
    assert "**INS" in trees[1]["text"]
    assert "INS [G" in trees[2]["text"]
    assert not descendants(trees[3], "TABLE")
    assert len(descendants(trees[4], "TABLE")) == 1
    assert trees[4]["text"].count("[G1]") == 1
    assert len(descendants(trees[4], "STRONG")) == 1


def test_untrusted_html_and_dangerous_links_never_create_active_dom():
    text = '<img src=x onerror="alert(1)"><script>alert(2)</script>\n\n[unsafe](javascript:alert(3)) [data](data:text/html,attack) [credential](https://:secret@example.com/) [paper](https://pubmed.ncbi.nlm.nih.gov/123/) [balanced](https://example.com/paper_(2024))'
    tree = render_many([text])[0]
    assert not descendants(tree, "IMG") and not descendants(tree, "SCRIPT")
    assert '<img src=x onerror="alert(1)">' in tree["text"]
    links = descendants(tree, "A")
    assert [node["href"] for node in links] == ["https://pubmed.ncbi.nlm.nih.gov/123/", "https://example.com/paper_(2024)"]
    assert all(node["rel"] == "noopener noreferrer" and node["target"] == "_blank" for node in links)


def test_code_blocks_and_quotes_are_literal_and_do_not_load_images():
    tree = render_many(["> Reported evidence [G1].\n\n```cypher\nMATCH (n) RETURN '<script>not code</script>'\n```\n\n![tracking](https://example.com/pixel)"])[0]
    assert descendants(tree, "BLOCKQUOTE")[0]["text"] == "Reported evidence [G1]."
    assert descendants(tree, "PRE")[0]["text"] == "MATCH (n) RETURN '<script>not code</script>'"
    assert not descendants(tree, "SCRIPT") and not descendants(tree, "IMG")
    assert not descendants(tree, "A")
