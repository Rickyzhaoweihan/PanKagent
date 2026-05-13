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


def test_prompt_includes_ordered_decision_procedure():
    """The new prompt must be ordered, not a flat rule list."""
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
    # Procedure markers
    assert "DECISION PROCEDURE" in sent_system
    assert "1." in sent_system and "2." in sent_system and "3." in sent_system
    # Anaphora examples must include both follow_up and new_query cases
    assert "those" in sent_system.lower()
    assert "highest PIP" in sent_system or "highest pip" in sent_system.lower()
    # Mixed-case warning: anaphora + new dimension = new_query
    assert "alpha cells" in sent_system.lower() or "expression" in sent_system.lower()


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
