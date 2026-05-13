"""Issue #15: /chat/start (non-auto-confirm) must persist round-1 turns
to session.history so round 2 can resolve pronouns / anaphora."""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient


def _no_op_lifespan(app):
    """Replace the heavy lifespan with a no-op for unit tests."""
    @asynccontextmanager
    async def _inner(app):
        yield
    return _inner(app)


def _stub_plan_pipeline(question, use_literature=True, chat_history=None):
    return {
        "interpreted_question": question,
        "plan": {"plan_type": "parallel", "steps": [
            {"id": 1, "query": "Get effector genes for type 1 diabetes"},
        ]},
        "neo4j_results": [{"records": [{"nodes": [
            {"labels": ["gene"], "properties": {"name": "CFTR", "id": "ENSG00000001626"}},
        ]}]}],
        "cypher_queries": ["MATCH (g:gene)-[:effector_gene_of]->(d:disease) RETURN g LIMIT 1"],
        "complexity": "simple",
        "literature_result": "",
    }


def _make_client():
    from server import app
    return TestClient(app)


def test_chat_start_persists_round1_history_when_pending():
    with (
        patch("server.lifespan", _no_op_lifespan),
        patch("server._run_plan_pipeline", side_effect=_stub_plan_pipeline),
        patch("server._persist_plan_session"),
        patch("server._persist_chat_session"),
        patch("server._cleanup_expired_chat_sessions"),
    ):
        client = _make_client()
        r = client.post("/chat/start", json={
            "question": "What are the effector genes for T1D?",
            "auto_confirm": False,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["route"] == "new_query_pending"
        session_id = body["session_id"]

        # Inspect persisted history via /chat/history
        h = client.get(f"/chat/history?session_id={session_id}")
        assert h.status_code == 200, h.text
        turns = h.json()["history"]
        assert len(turns) == 2, f"expected user+assistant placeholder, got {turns}"
        assert turns[0]["role"] == "user"
        assert turns[0]["content"] == "What are the effector genes for T1D?"
        assert turns[1]["role"] == "assistant"
        # placeholder should mention the plan summary and the retrieved gene
        assert "CFTR" in turns[1]["content"] or "effector" in turns[1]["content"].lower()
