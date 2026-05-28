"""Offline tests for the deterministic markdown normalizer + LLM-fallback gate."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from markdown_normalizer import (
    normalize_markdown,
    needs_llm_repair,
    repair_markdown,
)


@pytest.fixture(autouse=True)
def _clear_repair_cache():
    # repair_markdown is @lru_cache'd in production (dedupes the redundant
    # second Haiku call per confirm). Reset it between tests so memoization
    # doesn't leak results across cases that share input strings.
    repair_markdown.cache_clear()
    yield


def _has_table(md: str) -> bool:
    """A header row immediately followed by a delimiter row == a renderable table."""
    lines = md.split("\n")
    from markdown_normalizer import _PIPE_ROW_RE, _is_delim
    for i in range(len(lines) - 1):
        if _PIPE_ROW_RE.match(lines[i]) and _is_delim(lines[i + 1]):
            return True
    return False


def test_blank_line_before_table_inserted():
    md = "Here are the values:\n| Gene | PIP |\n| --- | --- |\n| ADCY3 | 0.25 |"
    fixed, fixes = normalize_markdown(md)
    assert "blank-line-before-table" in fixes
    # The line before the header row must be blank.
    lines = fixed.split("\n")
    hdr = next(i for i, l in enumerate(lines) if l.startswith("| Gene"))
    assert lines[hdr - 1].strip() == ""
    assert _has_table(fixed)


def test_setext_collision_hr_gets_blank_line():
    md = "Summary text\n---\nNext section"
    fixed, fixes = normalize_markdown(md)
    assert "blank-line-before-hr" in fixes
    lines = fixed.split("\n")
    hr = next(i for i, l in enumerate(lines) if set(l.strip()) == {"-"})
    assert lines[hr - 1].strip() == ""


def test_missing_delimiter_inserted():
    md = "| Gene | PIP |\n| ADCY3 | 0.25 |"
    fixed, fixes = normalize_markdown(md)
    assert "delimiter-row-insert" in fixes
    assert _has_table(fixed)


def test_cell_count_padded():
    md = "| A | B | C |\n| --- | --- | --- |\n| 1 | 2 |"
    fixed, fixes = normalize_markdown(md)
    assert "cell-count-pad" in fixes
    # The short row got a third (empty) cell.
    last = fixed.strip().split("\n")[-1]
    assert last.count("|") == 4  # | 1 | 2 |  | -> 4 pipes for 3 cells


def test_malformed_delimiter_rebuilt():
    md = "| A | B | C |\n|--|-|\n| 1 | 2 | 3 |"
    fixed, fixes = normalize_markdown(md)
    assert "delimiter-row-repair" in fixes
    # Delimiter now has 3 columns.
    lines = fixed.split("\n")
    delim = next(l for l in lines if set(l.replace("|", "").replace(" ", "")) == {"-"})
    assert delim.count("---") == 3


def test_fenced_code_block_untouched():
    md = "```cypher\nMATCH (g:gene) WHERE g.name = 'INS' | extra | pipes |\n```"
    fixed, fixes = normalize_markdown(md)
    assert fixes == []
    assert "MATCH (g:gene)" in fixed
    # The pipe line inside the fence must not have been turned into a table.
    assert "| --- |" not in fixed


def test_valid_markdown_unchanged():
    md = (
        "Yes — ADCY3 has strong colocalization evidence.\n"
        "\n"
        "| Property | Value |\n"
        "| --- | --- |\n"
        "| PIP (QTL) | 0.254 |\n"
        "| Purity | 0.972 |\n"
        "\n"
        "Summary: strong shared-signal evidence.\n"
    )
    fixed, fixes = normalize_markdown(md)
    assert fixes == []
    assert fixed.strip() == md.strip()


def test_idempotent():
    md = "Header\n| A | B |\n| 1 | 2 |\nTail text"
    once, _ = normalize_markdown(md)
    twice, fixes2 = normalize_markdown(once)
    assert once == twice
    assert fixes2 == []  # second pass finds nothing to fix


def test_needs_llm_repair_false_for_fixable():
    md = "Text\n| A | B |\n| --- | --- |\n| 1 | 2 |"
    fixed, _ = normalize_markdown(md)
    assert needs_llm_repair(fixed) is False


def test_repair_markdown_does_not_call_llm_on_clean_input():
    md = "Text:\n| A | B |\n| --- | --- |\n| 1 | 2 |"
    with patch("markdown_normalizer._llm_repair_block") as mock_llm:
        out = repair_markdown(md, allow_llm=True)
        mock_llm.assert_not_called()
    assert _has_table(out)


def test_repair_markdown_emits_event_when_changed():
    md = "Text:\n| A | B |\n| --- | --- |\n| 1 | 2 |"  # missing blank line before table
    with patch("stream_events.emit") as mock_emit, \
         patch("markdown_normalizer._llm_repair_block", return_value=None):
        repair_markdown(md, allow_llm=True)
        assert mock_emit.called
        evt, payload = mock_emit.call_args[0]
        assert evt == "markdown_normalized"
        assert payload["changed"] is True


def test_repair_markdown_silent_when_unchanged():
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"  # already valid
    with patch("stream_events.emit") as mock_emit:
        repair_markdown(md, allow_llm=True)
        mock_emit.assert_not_called()


# --- crammed-block / table-anomaly detection (the HLA-DRA failure mode) ---

def test_needs_llm_repair_detects_inline_heading():
    # heading run together with preceding content + a following heading inline
    md = "## Title **Gene:** X | a | b --- ### Next Section body text here"
    assert needs_llm_repair(md) is True


def test_needs_llm_repair_detects_inline_hr():
    md = "some paragraph text --- more text after the rule"
    assert needs_llm_repair(md) is True


def test_clean_hr_line_does_not_trigger_llm():
    md = "Para one\n\n---\n\n## Heading\n\nPara two"
    assert needs_llm_repair(md) is False


def test_needs_llm_repair_detects_double_delimiter():
    md = "| A | B |\n| --- | --- |\n| --- | --- |\n| 1 | 2 |"
    assert needs_llm_repair(md) is True


def test_needs_llm_repair_detects_header_blank_then_delim():
    md = "| A | B |\n\n| --- | --- |\n| 1 | 2 |"
    assert needs_llm_repair(md) is True


def test_table_blank_gap_repaired_deterministically():
    # header, blank line, delimiter -> deterministic pass pulls delim up; no LLM needed
    md = "| A | B |\n\n| --- | --- |\n| 1 | 2 |"
    fixed, fixes = normalize_markdown(md)
    assert "table-blank-gap" in fixes
    assert needs_llm_repair(fixed) is False
    assert _has_table(fixed)


def test_double_delimiter_collapsed_deterministically():
    md = "| A | B |\n| --- | --- |\n| --- | --- |\n| 1 | 2 |"
    fixed, fixes = normalize_markdown(md)
    assert "delimiter-dedup" in fixes
    # exactly one delimiter row remains
    delim_count = sum(1 for l in fixed.split("\n")
                      if set(l.replace("|", "").replace(" ", "")) == {"-"})
    assert delim_count == 1


def test_haiku_fires_on_crammed_blocks_and_result_used():
    md = "## Title body --- ### Section more body text"
    repaired = "## Title body\n\n---\n\n### Section\n\nmore body text"
    with patch("markdown_normalizer._llm_repair_block", return_value=repaired) as mock_llm:
        out = repair_markdown(md, allow_llm=True)
        mock_llm.assert_called_once()
    assert out == repaired


def test_content_loss_guard_rejects_short_haiku():
    md = "## Title body --- ### Section " + ("word " * 200)  # long, crammed
    det, _ = normalize_markdown(md)
    truncated = "## Title body"  # Haiku "dropped" almost everything
    with patch("markdown_normalizer._llm_repair_block", return_value=truncated):
        out = repair_markdown(md, allow_llm=True)
    # guard rejects the short output -> falls back to deterministic, not the stub
    assert out == det
    assert out != truncated


# --- hardening: absorbed-heading detector, guard, model selection ---

def test_needs_llm_repair_detects_heading_absorbed_paragraph():
    # A heading that swallowed a long paragraph (overlong) or a second block
    # marker — caught without fragile sentence-boundary heuristics.
    overlong = ("### Summary " + "CTLA4 is a negative regulator of T cells and "
                "restrains autoimmunity across many tissues " * 2)  # > 120 chars
    assert needs_llm_repair(overlong) is True
    assert needs_llm_repair("## Title body --- ### Section more") is True  # 2nd marker


def test_short_clean_heading_not_flagged():
    assert needs_llm_repair("## Results\n\nSome text here.\n") is False
    assert needs_llm_repair("### GO Annotations\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n") is False


def test_numbered_and_colon_headings_not_flagged():
    # Real, valid headings that previously false-positived the sentence-break heuristic.
    for h in (
        "### 1. T1D Effector Gene",
        "### 1. GWAS membership of rs13393590 (T1D)",
        "### SCN Genes Detected in Beta Cells (condition: ALL, total_cells: 120,502)",
        "## Gene: HLA-DRA",
    ):
        assert needs_llm_repair(h + "\n\nbody text\n") is False, h


def test_guard_accepts_valid_shorter_repair():
    # crammed input; repair is shorter than the deterministic output but VALID
    md = "## A body --- ### B more"
    det, _ = normalize_markdown(md)
    valid_shorter = "## A body\n\n---\n\n### B more"  # clean, may be shorter
    with patch("markdown_normalizer._llm_repair_block", return_value=valid_shorter):
        out = repair_markdown(md, allow_llm=True)
    assert out == valid_shorter  # accepted because it is now structurally valid


def test_repair_markdown_passes_model_through():
    md = "## A body --- ### B more"
    seen = {}
    def _fake(block, model="x"):
        seen["model"] = model
        return "## A body\n\n---\n\n### B more"
    with patch("markdown_normalizer._llm_repair_block", side_effect=_fake):
        repair_markdown(md, allow_llm=True, model="claude-sonnet-4-6")
    assert seen["model"] == "claude-sonnet-4-6"
