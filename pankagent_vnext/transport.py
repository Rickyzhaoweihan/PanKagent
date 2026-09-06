"""Delivery limits independent of graph materialization and model execution."""

from __future__ import annotations

import json


RESPONSE_BYTE_LIMIT = 8 * 1024 * 1024
ANSWER_PREVIEW_BYTES = 64 * 1024
NOTICE = "Delivery is partial: the response exceeded the 8 MiB transport limit. Evidence was omitted and text may be shortened."
HEAVY_ARRAYS = {"nodes", "edges", "relationships", "rows", "references", "queries", "validation", "provenance", "alternatives"}
ANSWER_FIELDS = {"answer", "graph_answer", "text", "response"}
PRIORITY_FIELDS = (
    "version", "run_id", "plan_id", "session_id", "sequence", "timestamp", "type",
    "status", "stage", "elapsed_ms", "id", "step_id", "evidence_id", "label",
    "delta", "payload", "answer", "graph_answer", "text", "response", "plan",
    "evidence", "literature", "steps", "perspectives", "graph_version", "error",
)


def _encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _shorten(value: str, maximum: int) -> str:
    return value.encode("utf-8")[:max(0, maximum)].decode("utf-8", errors="ignore")


def partial_delivery(value, original_bytes: int):
    """Build a bounded preview; execution metadata and replay identity survive."""
    budget = {"items": 512, "text_bytes": 1024 * 1024}

    def project(item, field="", depth=0):
        if isinstance(item, str):
            limit = ANSWER_PREVIEW_BYTES if field in ANSWER_FIELDS else 1024
            text = _shorten(item, min(limit, budget["text_bytes"]))
            budget["text_bytes"] -= len(text.encode("utf-8"))
            return text + "\n\n[" + NOTICE + "]" if field in ANSWER_FIELDS else text
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if budget["items"] <= 0 or depth > 7:
            return [] if isinstance(item, list) else {"delivery_status": "partial", "truncated": True}
        if isinstance(item, list):
            projected = []
            for child in item[:16]:
                if budget["items"] <= 0:
                    break
                budget["items"] -= 1
                projected.append(project(child, field, depth + 1))
            return projected
        if isinstance(item, dict):
            selected = [key for key in PRIORITY_FIELDS if key in item]
            for key in item:
                if len(selected) >= 48:
                    break
                if key not in selected:
                    selected.append(key)
            output, omitted = {}, {}
            for key in selected:
                if budget["items"] <= 0:
                    break
                budget["items"] -= 1
                child = item[key]
                safe_key = _shorten(str(key), 128)
                if key in HEAVY_ARRAYS and isinstance(child, (dict, list)):
                    output[safe_key] = []
                    omitted[safe_key] = len(child)
                else:
                    output[safe_key] = project(child, key, depth + 1)
                    if isinstance(child, list) and len(output[safe_key]) < len(child):
                        omitted[safe_key] = len(child) - len(output[safe_key])
            output.update(delivery_status="partial", truncated=True)
            if omitted:
                output["omitted_counts"] = omitted
            if field == "evidence" or any(key in item for key in ("nodes", "edges", "rows")):
                output["completeness"] = "partial"
            workflow = "run_id" in item or "plan_id" in item or "sequence" in item
            if not workflow and item.get("status") in {"complete", "completed"}:
                output["status"] = "partial"
            if "references" in omitted or "answer_reference_validation" in item:
                output["citation_validation"] = {"valid": False, "reason": "response_size_limit"}
                if "answer_reference_validation" in item:
                    output["answer_reference_validation"] = {"valid": False, "reason": "response_size_limit"}
            return output
        return None

    preview = project(value)
    if not isinstance(preview, dict):
        preview = {"preview": preview}
    preview.update(delivery_status="partial", truncated=True)
    preview["transport_truncation"] = {
        "reason": "response_size_limit", "limit_bytes": RESPONSE_BYTE_LIMIT,
        "original_bytes": original_bytes, "message": NOTICE,
    }
    # Keep durable workflow/SSE envelope identity and status exactly, including
    # terminal status inside payload. Partial delivery is a separate state.
    if isinstance(value, dict):
        for key in ("version", "run_id", "plan_id", "session_id", "sequence", "timestamp", "type", "status", "stage", "elapsed_ms"):
            if key in value:
                preview[key] = value[key]
        if value.get("type") == "terminal" and isinstance(value.get("payload"), dict):
            preview.setdefault("payload", {})["status"] = value["payload"].get("status")
    return preview


def bounded_json_bytes(raw: bytes, limit: int = RESPONSE_BYTE_LIMIT) -> bytes:
    if len(raw) <= limit:
        return raw
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        value = {}
    preview = partial_delivery(value, len(raw))
    encoded = _encode(preview)
    if len(encoded) <= limit:
        return encoded
    # This guard handles malformed giant metadata/IDs without ever writing an
    # oversized response. Normal service-generated workflow IDs are UUIDs.
    minimal = {key: _shorten(preview[key], 1024) if isinstance(preview[key], str) else preview[key] for key in ("version", "sequence", "status", "stage", "elapsed_ms") if key in preview and isinstance(preview[key], (str, int, float, bool))}
    for key in ("run_id", "plan_id", "session_id", "timestamp", "type"):
        if key in preview:
            minimal[key] = _shorten(str(preview[key]), 1024)
    minimal.update(delivery_status="partial", truncated=True, transport_truncation=preview["transport_truncation"])
    return _encode(minimal)


def sse_event_bytes(event: dict) -> bytes:
    prefix = f'id: {event["sequence"]}\nevent: {event["type"]}\ndata: '.encode("utf-8")
    # Preserve the previous serialization for normal events. The complete frame,
    # including SSE framing and newlines, is bounded by the same fixed ceiling.
    raw = json.dumps(event, ensure_ascii=False).encode("utf-8")
    body = bounded_json_bytes(raw, RESPONSE_BYTE_LIMIT - len(prefix) - 2)
    return prefix + body + b"\n\n"


class JSONResponseLimitMiddleware:
    """Apply the ceiling to every JSON response, including validation errors."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        pending_start = None
        chunks = []

        async def bounded_send(message):
            nonlocal pending_start
            if message["type"] == "http.response.start":
                content_type = next((value for key, value in message.get("headers", []) if key.lower() == b"content-type"), b"")
                media_type = content_type.lower().split(b";", 1)[0].strip()
                if media_type.endswith((b"/json", b"+json")):
                    pending_start = message
                    return
            if message["type"] == "http.response.body" and pending_start is not None:
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    raw = b"".join(chunks)
                    body = bounded_json_bytes(raw)
                    start = dict(pending_start)
                    start["headers"] = [(key, value) for key, value in start.get("headers", []) if key.lower() != b"content-length"] + [(b"content-length", str(len(body)).encode("ascii"))]
                    await send(start)
                    await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            await send(message)

        await self.app(scope, receive, bounded_send)
