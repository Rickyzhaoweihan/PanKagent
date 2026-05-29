"""Issue #15: Haiku classifier prompt regression cases.

We stub the Anthropic client so the tests stay deterministic and offline,
asserting that the prompt+messages sent to Haiku contain the signals
we expect the model to use."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import server


def _stub_anthropic(return_text: str):
    fake_response = MagicMock()
    fake_response.content = [MagicMock(text=return_text)]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    return fake_client


def test_prompt_is_confidence_first_biased_to_new_query():
    """The prompt must be confidence-first: default to new_query, pick follow_up
    only when highly confident it's answerable from already-shown text."""
    fake_client = _stub_anthropic("follow_up")
    with patch("anthropic.Anthropic", return_value=fake_client):
        server._classify_followup(
            history=[
                {"role": "user", "content": "What are the effector genes for T1D?"},
                {"role": "assistant", "content": "CFTR, RFX6, IDDM2, ..."},
            ],
            new_question="Which of those has the highest PIP?",
        )
    sent_system = fake_client.messages.create.call_args.kwargs["system"]
    # Confidence-first bias markers
    assert "DEFAULT TO new_query" in sent_system
    assert "confiden" in sent_system.lower()  # "HIGHLY CONFIDENT" / "confidence"
    # Anaphora must be explicitly called out as NOT decisive on its own
    assert "anaphora" in sent_system.lower()
    # Both follow_up and new_query examples present
    assert "highest PIP" in sent_system or "highest pip" in sent_system.lower()
    assert "expression" in sent_system.lower() or "antigen presentation" in sent_system.lower()


def test_classifier_uses_sonnet():
    """Routing must use Sonnet (not Haiku) for accuracy."""
    fake_client = _stub_anthropic("new_query")
    with patch("anthropic.Anthropic", return_value=fake_client):
        server._classify_followup(
            history=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
            new_question="what pathways connect that gene to antigen presentation?",
        )
    model = fake_client.messages.create.call_args.kwargs["model"]
    assert "sonnet" in model.lower()


def test_classifier_input_truncation_widened_to_800():
    """Per-turn truncation in the classifier input should be 800 chars,
    not 500 — long retrieved-data answers need entity tails preserved."""
    fake_client = _stub_anthropic("new_query")
    long_answer = "A" * 1000
    with patch("anthropic.Anthropic", return_value=fake_client):
        server._classify_followup(
            history=[
                {"role": "user", "content": "Q1"},
                {"role": "assistant", "content": long_answer},
            ],
            new_question="What about CFTR?",
        )
    sent_messages = fake_client.messages.create.call_args.kwargs["messages"]
    user_content = sent_messages[0]["content"]
    # 800-char window means the truncated assistant turn shows 800 'A's
    assert "A" * 800 in user_content
    assert "A" * 801 not in user_content


def test_followup_keyword_still_parsed():
    fake_client = _stub_anthropic("follow_up")
    with patch("anthropic.Anthropic", return_value=fake_client):
        result = server._classify_followup(
            history=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
            new_question="explain that",
        )
    assert result == "follow_up"


def test_new_query_default():
    fake_client = _stub_anthropic("new_query")
    with patch("anthropic.Anthropic", return_value=fake_client):
        result = server._classify_followup(history=[], new_question="anything")
    assert result == "new_query"
