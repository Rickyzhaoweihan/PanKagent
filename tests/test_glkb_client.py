"""GLKB SSE-stream parsing — mocked HTTP, no network."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


SAMPLE_STREAM = (
    b'data: {"step":"Start","done":false}\n\n'
    b'data: {"step":"PubMedSearch","done":false}\n\n'
    b'data: {"step":"Complete","response":"CFTR encodes a chloride channel. '
    b'Mutations cause cystic fibrosis [PMID: 12345].","references":'
    b'[{"pmid":"12345","title":"CFTR review","url":"https://pubmed.ncbi.nlm.'
    b'nih.gov/12345/","journal":"NEJM","date":"2020","authors":["Smith J"],'
    b'"evidence":"CFTR encodes..."}],"session_id":"abc-123",'
    b'"execution_time":28.4,"done":true}\n\n'
)


def _fake_response(stream_bytes: bytes, status: int = 200):
    fake = MagicMock()
    fake.status_code = status
    fake.iter_lines.return_value = stream_bytes.split(b"\n")
    fake.raise_for_status = MagicMock()
    return fake


def test_call_glkb_parses_complete_event():
    from skills.glkb.scripts.glkb_client import call_glkb

    with patch("requests.post", return_value=_fake_response(SAMPLE_STREAM)):
        result = call_glkb("What does CFTR do?")

    assert result["status"] == "success"
    assert "CFTR" in result["response"]
    assert len(result["references"]) == 1
    assert result["references"][0]["pmid"] == "12345"
    assert result["session_id"] == "abc-123"
    assert result["execution_time"] == pytest.approx(28.4)


def test_call_glkb_directive_prefix_applied():
    from skills.glkb.scripts.glkb_client import build_glkb_question

    out = build_glkb_question("What does CFTR do?")
    assert "synthesis" in out.lower() or "narrative" in out.lower()
    assert out.endswith("What does CFTR do?")


def test_call_glkb_handles_http_error():
    from skills.glkb.scripts.glkb_client import call_glkb

    with patch("requests.post", side_effect=Exception("boom")):
        result = call_glkb("anything")

    assert result["status"] == "failed"
    assert result["response"] == ""
    assert result["references"] == []


def test_call_glkb_handles_missing_complete_event():
    from skills.glkb.scripts.glkb_client import call_glkb

    incomplete = (
        b'data: {"step":"Start","done":false}\n\n'
        b'data: {"step":"PubMedSearch","done":false}\n\n'
    )
    with patch("requests.post", return_value=_fake_response(incomplete)):
        result = call_glkb("anything")

    assert result["status"] == "failed"


def test_call_glkb_skips_non_data_lines():
    """Real SSE streams have empty/comment lines mixed in."""
    from skills.glkb.scripts.glkb_client import call_glkb

    noisy = (
        b'\n'
        b': keepalive\n\n'
        b'data: {"step":"Complete","response":"ok","references":[],'
        b'"session_id":"s","execution_time":1.0,"done":true}\n\n'
    )
    with patch("requests.post", return_value=_fake_response(noisy)):
        result = call_glkb("q")

    assert result["status"] == "success"
    assert result["response"] == "ok"
