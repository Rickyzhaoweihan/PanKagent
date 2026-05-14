"""Literature retrieval via GLKB only (HIRN disabled)."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def run_literature_parallel(question: str, kg_context: str = "") -> dict[str, Any]:
    """Run GLKB literature synthesis. Returns {'glkb': ..., 'hirn': None}.

    kg_context: retrieval blob (same content the format agent sees); forwarded
    to GLKB so it can frame literature as complementary to PanKgraph findings.
    """
    from skills.glkb.scripts.glkb_client import call_glkb
    try:
        glkb_result = call_glkb(question, kg_context=kg_context)
    except Exception as exc:
        logger.warning(f"GLKB call failed: {exc}")
        glkb_result = {"status": "failed", "response": "", "references": [], "session_id": "", "execution_time": 0.0}
    return {"glkb": glkb_result, "hirn": None}


def _format_references_section(glkb_refs: list) -> str:
    """Format GLKB references as a markdown list."""
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
    if not lines:
        return ""
    return "## References\n" + "\n".join(lines)


def combine_literature_block(
    glkb: dict | None,
    hirn: dict | None = None,  # kept for call-site compatibility — ignored
    use_literature: bool = True,
) -> str:
    """Build the literature markdown block appended post-hoc to the final answer.

    GLKB is the sole source. Its response is framed as complementary to
    PanKgraph — supporting or extending findings, never contradicting them.
    Returns "" when GLKB has no useful result.
    """
    if not use_literature:
        return ""

    glkb = glkb or {}
    glkb_ok = glkb.get("status") == "success" and bool(glkb.get("response"))
    if not glkb_ok:
        return ""

    glkb_refs = glkb.get("references", []) or []
    parts: list[str] = [
        "## Supporting Literature",
        "",
        "_The following published evidence complements the PanKgraph data above._",
        "",
        glkb["response"].strip(),
    ]

    refs_section = _format_references_section(glkb_refs)
    if refs_section:
        parts.append("")
        parts.append(refs_section)

    return "\n".join(parts).rstrip() + "\n"
