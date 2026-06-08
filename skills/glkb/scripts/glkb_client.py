"""GLKB (Graph Language Knowledge Base) API client.

Targets the local GLKB_agent service's SSE endpoint (default
http://localhost:8006/stream; override with the GLKB_URL env var). It accepts
POST {question, session_id?} and streams an SSE response whose terminal event
has step=='Complete' and carries the synthesised answer + structured PubMed
references.

This client is a synchronous wrapper that drains the stream and returns
a flat result dict. All exceptions are converted to status='failed' so
callers don't need to catch — they just check ``result['status']``.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests


# Local GLKB_agent service (FastAPI) /stream endpoint. Override via env, e.g.
# GLKB_URL=http://localhost:8006/stream. The old remote
# (glkb.dcmb.med.umich.edu/api/frontend/llm_agent) now 301-redirects to a static
# site and no longer serves the API.
GLKB_URL = os.environ.get("GLKB_URL", "http://localhost:8006/stream")
DEFAULT_TIMEOUT_S = 60

logger = logging.getLogger(__name__)

_DIRECTIVE_PREFIX = (
    "In under 120 words, provide a concise literature summary that is COMPLEMENTARY "
    "to the PanKgraph knowledge graph findings shown below. Your role is to add "
    "published context that SUPPORTS or EXTENDS the PanKgraph data — do NOT "
    "contradict it or present opposing conclusions. If the published literature "
    "does not contain findings directly relevant to the question, state only: "
    "'No directly relevant published literature was identified; PanKgraph presents "
    "the data shown above.' Do NOT speculate, fabricate, or introduce entities "
    "not present in the retrieved data. Include inline PubMed citations where "
    "directly relevant. Focus on pancreatic islet biology, type 1 diabetes, and "
    "immune mechanisms where relevant. Write as a short coherent narrative. "
    "Avoid listing papers one by one.\n\n"
    "Question: "
)


def build_glkb_question(question: str, kg_context: str = "") -> str:
    """Prepend the synthesis directive to the user's question.

    kg_context (the retrieval blob the format agent sees) is always inserted
    so GLKB synthesises literature grounded in what PanKgraph actually found
    and can frame its response as complementary, not contradictory.
    """
    if kg_context:
        return (
            _DIRECTIVE_PREFIX.rstrip()
            + "\n\n=== PANKGRAPH RETRIEVED DATA (treat as primary; literature must complement, not contradict) ===\n"
            + kg_context.strip()
            + "\n\nQuestion: "
            + question.strip()
        )
    return _DIRECTIVE_PREFIX + question.strip()


def _failed() -> dict[str, Any]:
    return {"status": "failed", "response": "", "references": [],
            "session_id": "", "execution_time": 0.0}


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
            allow_redirects=False,
        )
        # Fail loudly if the endpoint moved (a redirect, or a non-SSE body such
        # as a website's HTML) instead of silently draining it as a "failed" run.
        if resp.is_redirect or resp.status_code in (301, 302, 307, 308):
            loc = resp.headers.get("Location", "?")
            logger.warning("GLKB endpoint %s redirected (%s) -> %s; endpoint moved?",
                           GLKB_URL, resp.status_code, loc)
            return _failed()
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "event-stream" not in ctype:
            logger.warning("GLKB endpoint %s returned non-SSE Content-Type '%s' — "
                           "not a stream (moved/misconfigured?)", GLKB_URL, ctype)
            return _failed()

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
        return _failed()
    except Exception as exc:
        logger.warning(f"GLKB call failed: {exc}")
        return _failed()
