"""Parallel literature retrieval (GLKB + HIRN) + hardcoded merge."""
from __future__ import annotations

import logging
import threading
from queue import Queue
from typing import Any

logger = logging.getLogger(__name__)


def _glkb_worker(question: str, q: "Queue[dict]", kg_context: str = "") -> None:
    from skills.glkb.scripts.glkb_client import call_glkb
    try:
        q.put({"source": "glkb", "result": call_glkb(question, kg_context=kg_context)})
    except Exception as exc:
        logger.warning(f"GLKB worker failed: {exc}")
        q.put({"source": "glkb", "result": {
            "status": "failed", "response": "", "references": [],
            "session_id": "", "execution_time": 0.0,
        }})


def _hirn_worker(question: str, q: "Queue[dict]") -> None:
    import json as _json
    import re as _re
    _FAILED = {"status": "failed", "summary": "", "raw_passages": []}
    try:
        from queue import Queue as _Queue
        from main import _run_hirn_search
        inner_q: _Queue = _Queue()
        _run_hirn_search(question, inner_q)
        raw: str = inner_q.get(block=False) if not inner_q.empty() else ""
        # _run_hirn_search puts a formatted string: "...Status: success\nResult: <json>\n\n"
        m = _re.search(r'Status: success\nResult: (.+?)(?:\n\n|$)', raw, _re.DOTALL)
        if m:
            data = _json.loads(m.group(1).strip())
            result = {
                "status": "success",
                "summary": "",
                "raw_passages": data.get("raw_passages", []),
            }
        else:
            result = _FAILED
        q.put({"source": "hirn", "result": result})
    except Exception as exc:
        logger.warning(f"HIRN worker failed: {exc}")
        q.put({"source": "hirn", "result": _FAILED})


def run_literature_parallel(question: str, kg_context: str = "") -> dict[str, Any]:
    """Run GLKB + HIRN in parallel threads; return {'glkb': ..., 'hirn': ...}.

    kg_context: optional retrieval blob (same content the format agent sees);
    forwarded to GLKB so it can synthesise literature grounded in what the
    KG/SQL/ssGSEA pipeline actually found.
    """
    q: Queue[dict] = Queue()
    t1 = threading.Thread(target=_glkb_worker, args=(question, q, kg_context), daemon=True)
    t2 = threading.Thread(target=_hirn_worker, args=(question, q), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    out: dict[str, Any] = {"glkb": None, "hirn": None}
    while not q.empty():
        msg = q.get()
        out[msg["source"]] = msg["result"]
    return out


def _render_hirn_highlights(passages: list, max_n: int = 3) -> str:
    if not passages:
        return ""
    lines = []
    for p in passages[:max_n]:
        title = p.get("title", "").strip() or "(untitled)"
        pmid = p.get("pmid", "")
        snippet = (p.get("passage", "") or "").strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:237] + "..."
        ref = f" [PMID: {pmid}]" if pmid else ""
        lines.append(f"- {title}{ref}: {snippet}")
    return "\n".join(lines)


def _format_references_section(glkb_refs: list, hirn_passages: list) -> str:
    """Deduped references. GLKB refs first (richer metadata), then HIRN."""
    seen: set[str] = set()
    lines = []
    for r in glkb_refs:
        pmid = str(r.get("pmid", "")).strip()
        if not pmid or pmid in seen:
            continue
        seen.add(pmid)
        title = r.get("title", "").strip() or "(untitled)"
        url = r.get("url", "").strip() or f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        journal = str(r.get("journal", "") or "").strip()
        date = str(r.get("date", "") or "").strip()
        meta = " · ".join(x for x in [journal, date] if x)
        suffix = f" ({meta})" if meta else ""
        lines.append(f"- {title}{suffix}\n  PMID: {pmid}\n  {url}")
    for p in hirn_passages:
        pmid = str(p.get("pmid", "")).strip()
        if not pmid or pmid in seen:
            continue
        seen.add(pmid)
        title = p.get("title", "").strip() or "(untitled)"
        url = p.get("url", "").strip() or f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        lines.append(f"- {title}\n  PMID: {pmid}\n  {url}")
    if not lines:
        return ""
    return "## References\n" + "\n".join(lines)


def combine_literature_block(
    glkb: dict | None,
    hirn: dict | None,
    use_literature: bool = True,
) -> str:
    """Build the literature markdown block that gets spliced into the final answer.

    Strategy: GLKB is primary (synthesised narrative). If GLKB has < 3
    references, supplement with top 3 HIRN passages. If GLKB failed, fall
    back to HIRN-only. References are deduped by PMID (GLKB first).
    """
    if not use_literature:
        return ""

    glkb = glkb or {}
    hirn = hirn or {}
    glkb_ok = glkb.get("status") == "success" and bool(glkb.get("response"))
    glkb_refs = glkb.get("references", []) or []
    hirn_ok = hirn.get("status") == "success" and (
        bool(hirn.get("summary")) or bool(hirn.get("raw_passages"))
    )
    hirn_passages = hirn.get("raw_passages", []) or []

    if not glkb_ok and not hirn_ok:
        return ""

    parts: list[str] = ["## Literature Evidence", ""]

    if glkb_ok:
        parts.append(glkb["response"].strip())
        if len(glkb_refs) < 3 and hirn_passages:
            highlights = _render_hirn_highlights(hirn_passages, max_n=3)
            if highlights:
                parts.append("")
                parts.append("### Additional HIRN Evidence")
                parts.append(highlights)
    else:
        # GLKB failed; HIRN-only fallback
        summary = (hirn.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        highlights = _render_hirn_highlights(hirn_passages, max_n=5)
        if highlights:
            parts.append("")
            parts.append("### Supporting Passages")
            parts.append(highlights)

    refs_section = _format_references_section(glkb_refs, hirn_passages)
    if refs_section:
        parts.append("")
        parts.append(refs_section)

    return "\n".join(parts).rstrip() + "\n"
