"""GLKB (Graph Language Knowledge Base) API client.

The GLKB endpoint at https://glkb.dcmb.med.umich.edu/api/frontend/llm_agent
accepts POST {question, session_id?} and streams an SSE response. The
terminal event has step=='Complete' and carries the synthesised answer +
structured PubMed references.

This client is a synchronous wrapper that drains the stream and returns
a flat result dict. All exceptions are converted to status='failed' so
callers don't need to catch — they just check ``result['status']``.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import requests


GLKB_URL = "https://glkb.dcmb.med.umich.edu/api/frontend/llm_agent"
DEFAULT_TIMEOUT_S = 60

logger = logging.getLogger(__name__)

_DIRECTIVE_PREFIX = (
    "In under 100 words, provide a concise synthesis answering the following "
    "biomedical question, with inline PubMed citations as supporting evidence. "
    "Focus on pancreatic islet biology, type 1 diabetes, and immune mechanisms "
    "where relevant. Write as a coherent narrative that directly addresses the "
    "question. Avoid listing papers one by one. Keep the response strictly "
    "under 100 words.\n\n"
    "Question: "
)


def build_glkb_question(question: str, kg_context: str = "") -> str:
    """Prepend the synthesis directive to the user's question.

    If kg_context is provided (the same retrieval blob the format agent sees),
    it is inserted between the directive and the question so GLKB can synthesise
    literature that is grounded in what the KG/SQL/ssGSEA pipeline actually found.
    """
    if kg_context:
        return (
            _DIRECTIVE_PREFIX.rstrip()
            + "\n\n=== RETRIEVED DATA (use as supporting evidence; "
            "do not invent facts beyond it) ===\n"
            + kg_context.strip()
            + "\n\nQuestion: "
            + question.strip()
        )
    return _DIRECTIVE_PREFIX + question.strip()


def call_glkb(
    question: str,
    session_id: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    kg_context: str = "",
) -> dict[str, Any]:
    """Call GLKB and return the flat Complete-event payload.

    Returns dict with keys: status ('success'|'failed'), response (str),
    references (list[dict]), session_id (str), execution_time (float).
    Never raises.
    """
    payload: dict[str, Any] = {"question": build_glkb_question(question, kg_context=kg_context)}
    if session_id:
        payload["session_id"] = session_id

    try:
        resp = requests.post(
            GLKB_URL,
            json=payload,
            stream=True,
            timeout=timeout,
            headers={"Accept": "text/event-stream"},
        )
        resp.raise_for_status()

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data:"):
                continue
            body = line[len("data:"):].strip()
            if not body:
                continue
            try:
                event = json.loads(body)
            except json.JSONDecodeError:
                continue
            if event.get("step") == "Complete" and event.get("done"):
                return {
                    "status": "success",
                    "response": event.get("response", ""),
                    "references": event.get("references", []) or [],
                    "session_id": event.get("session_id", ""),
                    "execution_time": float(event.get("execution_time", 0.0)),
                }
        logger.warning("GLKB stream ended without Complete event")
        return {
            "status": "failed",
            "response": "",
            "references": [],
            "session_id": "",
            "execution_time": 0.0,
        }
    except Exception as exc:
        logger.warning(f"GLKB call failed: {exc}")
        return {
            "status": "failed",
            "response": "",
            "references": [],
            "session_id": "",
            "execution_time": 0.0,
        }
