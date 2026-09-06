"""Versioned HIRN boundary: publish only completed answers and source metadata.

The existing wrapper audits its own attempts. This adapter neither repeats that
fan-out nor passes its retrieved excerpts, processing frames, or audit prompts
to the caller. A wrapper usage snapshot is cumulative, not a per-run bill.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
PERSPECTIVES = {
    "context_mechanism": "Evidence for the question's main mechanism",
    "alternative_explanation": "Alternative explanations and open questions",
}
REFERENCE_FIELDS = {
    "id", "document_id", "pmid", "doi", "title", "source", "source_type",
    "journal", "date", "year", "authors", "consortia", "n_citation",
    "url", "fulltext_url",
}
USAGE_FIELDS = {
    "month", "model", "input_tokens", "output_tokens", "claude_calls",
    "estimated_monthly_cost_usd", "warning_threshold_usd", "warning_active",
    "estimate_scope",
}
PROGRESS = {
    "planning": "Preparing literature searches",
    "attempt_complete": "Literature retrieval completed; checking evidence",
    "audit": "Checking literature relevance and references",
    "retrying": "Refining literature searches",
}
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 4 * 1024 * 1024


class LiteratureContractError(ValueError):
    """The configured source contract or upstream result was not met."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username:
            return value
    except ValueError:
        pass
    return None


def _references(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 100:
        raise LiteratureContractError("invalid_references")
    output = []
    for reference in value:
        if not isinstance(reference, dict):
            raise LiteratureContractError("invalid_reference")
        clean = {}
        for key in REFERENCE_FIELDS:
            item = reference.get(key)
            if item is None:
                continue
            if key in {"url", "fulltext_url"}:
                item = _safe_url(item)
                if item is None:
                    continue
            elif key == "authors":
                if isinstance(item, list):
                    item = [author for author in item if isinstance(author, str)]
                elif not isinstance(item, str):
                    continue
            elif not isinstance(item, (str, int, float, bool)):
                continue
            clean[key] = item
        # Preserve list positions: upstream answers can cite numbered references.
        output.append(clean)
    return output


def _citation_report(answer: str, references: list[dict[str, Any]]) -> dict[str, Any]:
    identifiers = {
        str(ref[field]) for ref in references for field in ("id", "document_id", "pmid")
        if ref.get(field) is not None
    }
    matched, unresolved = [], []
    for match in re.finditer(r"\[([^\]\n]{1,128})\]", answer):
        marker = match.group(1).strip()
        normalized = re.sub(r"^(?:PMID|pubmedid)\s*:\s*", "", marker, flags=re.I)
        if normalized in identifiers or (normalized.isdigit() and 1 <= int(normalized) <= len(references)):
            matched.append(marker)
        elif normalized.isdigit() or re.match(r"^(?:PMID|pubmedid|doc[-_:])", marker, re.I):
            unresolved.append(marker)
    return {
        "status": "incomplete" if unresolved else "linked" if matched else "no_inline_markers",
        "matched_markers": list(dict.fromkeys(matched)),
        "unresolved_markers": list(dict.fromkeys(unresolved)),
        "scope": "Reference linkage only; scientific support is not independently verified.",
    }


def _answer_unit(attempt: Any) -> dict[str, Any] | None:
    if attempt is None:
        return None
    if not isinstance(attempt, dict) or attempt.get("status") != "complete":
        raise LiteratureContractError("invalid_selected_attempt")
    result = attempt.get("result")
    if not isinstance(result, dict):
        raise LiteratureContractError("missing_completed_result")
    answer = result.get("response")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 120000:
        raise LiteratureContractError("invalid_answer")
    references = _references(result.get("references", []))
    report = _citation_report(answer, references)
    return {
        "answer": answer,
        "references": references,
        "attempt_id": str(attempt.get("attempt_id") or ""),
        "status": "no_evidence" if not references else "partial" if report["unresolved_markers"] else "complete",
        "citation_validation": report,
    }


def _normalize_legacy(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("perspectives")
    if not isinstance(raw, dict):
        raise LiteratureContractError("missing_perspectives")
    output = []
    for perspective_id, label in PERSPECTIVES.items():
        entry = raw.get(perspective_id)
        if not isinstance(entry, dict):
            raise LiteratureContractError("invalid_perspective")
        selected = _answer_unit(entry.get("selected"))
        perspective = {
            "id": perspective_id,
            "label": entry.get("label") if isinstance(entry.get("label"), str) else label,
            **(selected or {"answer": "No completed evidence was returned for this perspective.",
                            "references": [], "status": "unavailable"}),
            "alternatives": [],
        }
        alternatives = entry.get("alternatives", [])
        if not isinstance(alternatives, list) or len(alternatives) > 8:
            raise LiteratureContractError("invalid_alternatives")
        seen = set()
        for unit in [selected, *[_answer_unit(item) for item in alternatives]]:
            if unit is None:
                continue
            fingerprint = json.dumps({"answer": unit["answer"], "references": unit["references"]}, sort_keys=True)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            if unit is not selected:
                perspective["alternatives"].append(unit)
        output.append(perspective)
    return output


class LiteratureAdapter:
    def __init__(self, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None):
        self.url = str(settings.literature_url).rstrip("/")
        self.timeout = float(settings.literature_timeout)
        self.corpus_version = str(settings.corpus_version)
        self.source_policy = str(settings.source_policy)
        self.api_version = str(getattr(settings, "literature_api_version", "hirn-agent-v1"))
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=min(5.0, self.timeout)),
            transport=transport, follow_redirects=False, trust_env=False,
        )
        self.last_success: str | None = None
        self.last_error: str | None = None

    @property
    def cache_identity(self) -> str:
        """Changing endpoint, wire contract, policy, or corpus invalidates results."""
        value = [self.url, self.api_version, self.corpus_version, self.source_policy]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

    def _check_configuration(self) -> None:
        if self.api_version != "hirn-agent-v1":
            raise LiteratureContractError("unsupported_adapter_version")
        if self.source_policy != "mixed":
            raise LiteratureContractError("unsupported_source_policy")

    def _result(self, status: str, **values: Any) -> dict[str, Any]:
        return {
            "status": status, "perspectives": [], "corpus_version": self.corpus_version,
            "source_policy": self.source_policy, "service_version": self.api_version,
            "corpus_identity_source": "deployment_configuration",
            "upstream_usage": {"scope": "shared HIRN service; per-request cost unavailable",
                               "per_request_cost_usd": None, "included_in_local_budget": False},
            **values,
        }

    async def _consume(self, question: str, conversation: list, emit: Emit) -> dict[str, Any]:
        self._check_configuration()
        # Match the current strict wrapper request schema; keep answer/ref units together.
        history, turns, pending_question = [], [], None
        for turn in conversation:
            if isinstance(turn, dict) and turn.get("role") == "user":
                pending_question = turn.get("content")
            elif isinstance(turn, dict) and turn.get("role") == "assistant" and pending_question:
                turns.append({"question": pending_question, "response": turn.get("content"), "references": []})
                pending_question = None
            else:
                turns.append(turn)
        for turn in turns[-12:]:
            if not isinstance(turn, dict):
                continue
            prior_question = turn.get("question")
            answer = turn.get("response", turn.get("answer"))
            if isinstance(prior_question, str) and prior_question.strip() and isinstance(answer, str) and answer.strip():
                history.append({"question": prior_question[:6000], "response": answer[:30000],
                                "references": _references(turn.get("references", []))[:30]})
        async with self.client.stream("POST", f"{self.url}/stream", headers={"Accept": "text/event-stream"},
                                      json={"question": question, "conversation": history}) as response:
            response.raise_for_status()
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise LiteratureContractError("invalid_content_type")
            event_name, data, event_bytes, total_bytes = "message", [], 0, 0

            async def consume_event() -> dict[str, Any] | None:
                if event_name in PROGRESS:
                    # Deliberately do not parse processing/attempt/audit content.
                    await emit("literature_progress", {"stage": "searching_literature", "status": "running",
                                                       "message": PROGRESS[event_name]})
                elif event_name == "error":
                    raise LiteratureContractError("upstream_error")
                elif event_name == "complete":
                    try:
                        value = json.loads("\n".join(data))
                    except (ValueError, TypeError) as error:
                        raise LiteratureContractError("invalid_complete_json") from error
                    if not isinstance(value, dict):
                        raise LiteratureContractError("invalid_complete_payload")
                    return value
                return None

            async for line in response.aiter_lines():
                count = len(line.encode("utf-8")) + 1
                total_bytes += count
                event_bytes += count
                if total_bytes > MAX_STREAM_BYTES or event_bytes > MAX_EVENT_BYTES:
                    raise LiteratureContractError("response_too_large")
                if not line:
                    final = await consume_event()
                    if final is not None:
                        return final
                    event_name, data, event_bytes = "message", [], 0
                elif line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
            if data:
                final = await consume_event()
                if final is not None:
                    return final
        raise LiteratureContractError("incomplete_stream")

    async def search(self, question: str, conversation: list, emit: Emit) -> dict[str, Any]:
        started = time.monotonic()
        try:
            final = await asyncio.wait_for(self._consume(question, conversation, emit), timeout=self.timeout)
            perspectives = _normalize_legacy(final)
            self.last_success, self.last_error = _utcnow(), None
            for perspective in perspectives:
                await emit("literature_perspective", perspective)
            status = "complete" if all(p["status"] in {"complete", "no_evidence"} for p in perspectives) else "partial"
            usage = final.get("usage_status")
            result = self._result(status, perspectives=perspectives, elapsed_ms=round((time.monotonic() - started) * 1000))
            if isinstance(usage, dict):
                result["upstream_usage"]["service_cumulative_snapshot"] = {
                    key: value for key, value in usage.items()
                    if key in USAGE_FIELDS and isinstance(value, (str, int, float, bool))
                }
            return result
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException):
            category = "timeout"
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            category = "authentication" if code in {401, 403} else "rate_limit" if code == 429 else "upstream_http"
        except httpx.HTTPError:
            category = "unreachable"
        except LiteratureContractError as error:
            category = str(error)  # Only fixed categories generated in this module.
        self.last_error = category
        return self._result("unavailable", error_category=category,
                            elapsed_ms=round((time.monotonic() - started) * 1000))

    async def probe(self) -> dict[str, Any]:
        started = time.monotonic()
        result = {"state": "unknown", "checked_at": _utcnow(), "last_success": self.last_success,
                  "last_retrieval_success": self.last_success, "recent_error_category": self.last_error,
                  "corpus_version": self.corpus_version, "source_policy": self.source_policy,
                  "service_version": self.api_version, "corpus_identity_source": "deployment_configuration"}
        try:
            self._check_configuration()
            response = await self.client.get(f"{self.url}/health", timeout=4.0)
            payload = response.json()
            if not isinstance(payload, dict):
                raise LiteratureContractError("invalid_health_contract")
            wrapper_ok = response.is_success or response.status_code == 503
            upstream_ok = payload.get("hirn_healthy") is True
            configured = payload.get("anthropic_configured") is True
            result.update(wrapper_reachable=wrapper_ok, upstream_reachable=upstream_ok,
                          online_configured=configured, model=payload.get("model"))
            result["state"] = "healthy" if response.is_success and upstream_ok and configured else "degraded" if wrapper_ok else "unavailable"
            if result["state"] != "healthy":
                result["error_category"] = "upstream_unavailable" if wrapper_ok else "upstream_http"
        except (httpx.HTTPError, ValueError) as error:
            result["state"] = "unavailable"
            result["error_category"] = str(error) if isinstance(error, LiteratureContractError) else "unreachable" if isinstance(error, httpx.HTTPError) else "invalid_health_contract"
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
        return result

    async def close(self) -> None:
        await self.client.aclose()
