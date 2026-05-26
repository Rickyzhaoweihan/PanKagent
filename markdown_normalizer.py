"""Frontend-safe Markdown normalizer + monitor for agent answers.

The rigor format/reasoning agents emit Markdown (`summary`, `reasoning_trace`)
that is *usually* valid GFM but occasionally trips a strict CommonMark/GFM
frontend renderer — most often:

  * a table not preceded by a blank line (renders as raw pipes),
  * a ``---`` horizontal rule with no blank line above it (parsed as a setext
    H2, turning the previous line into a giant heading),
  * a missing / malformed ``|---|---|`` delimiter row,
  * table rows whose cell count doesn't match the header.

This module provides a deterministic repair pass (`normalize_markdown`) plus an
optional Claude Haiku fallback (`repair_markdown`) for the rare table block the
deterministic pass cannot safely fix. When anything is changed it emits a
``markdown_normalized`` stream event so we can monitor how often malformed
markdown occurs.

Public API:
    normalize_markdown(md)  -> (fixed_md, fixes_applied)
    needs_llm_repair(md)    -> bool
    repair_markdown(md, *, allow_llm=True) -> str
"""
from __future__ import annotations

import logging
import os
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

# A GFM table delimiter row, e.g.  | --- | :--: | ---: |   (pipes optional at ends)
_DELIM_RE = re.compile(r'^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$')
# A table-ish row: contains at least one pipe with content on a side.
_ROW_RE = re.compile(r'^\s*\|.*\|\s*$|^\s*[^|\n]*\|[^|\n]*$')
# A pipe row that has a leading AND trailing pipe (the canonical GFM shape).
_PIPE_ROW_RE = re.compile(r'^\s*\|.*\|\s*$')
# Horizontal rule: ---, ***, ___ (3+), optionally spaced.
_HR_RE = re.compile(r'^\s*([-*_])(\s*\1){2,}\s*$')
# ATX header.
_HEADER_RE = re.compile(r'^\s*#{1,6}\s')
# Fenced code block open/close.
_FENCE_RE = re.compile(r'^\s*(```|~~~)')


def _split_cells(row: str) -> list[str]:
    """Split a GFM table row into trimmed cells, ignoring the outer pipes."""
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_delim(line: str) -> bool:
    return bool(_DELIM_RE.match(line)) and "-" in line


def _looks_like_table_row(line: str) -> bool:
    """A line that plausibly belongs to a table (has a pipe, not a fence/hr)."""
    if not line or "|" not in line:
        return False
    if _HR_RE.match(line) or _FENCE_RE.match(line):
        return False
    return True


def _make_delim(n_cols: int) -> str:
    return "| " + " | ".join(["---"] * max(1, n_cols)) + " |"


def _normalize_row(cells: list[str], n_cols: int) -> str:
    """Pad/truncate cells to n_cols and re-render with canonical pipes."""
    cells = list(cells)
    if len(cells) < n_cols:
        cells += [""] * (n_cols - len(cells))
    elif len(cells) > n_cols:
        cells = cells[:n_cols]
    return "| " + " | ".join(cells) + " |"


def _pull_delimiters_to_headers(lines: list[str]) -> tuple[list[str], bool]:
    """Remove blank line(s) sitting between a table header row and its delimiter.

    GFM requires the delimiter row to immediately follow the header; an LLM that
    inserts a blank line there breaks the whole table. Returns (new_lines, changed).
    """
    out: list[str] = []
    changed = False
    in_fence = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if not in_fence and _PIPE_ROW_RE.match(line) and i + 1 < n and lines[i + 1].strip() == "":
            j = i + 1
            while j < n and lines[j].strip() == "":
                j += 1
            if j < n and _is_delim(lines[j]):
                out.append(line)      # header
                out.append(lines[j])  # delimiter, gap removed
                changed = True
                i = j + 1
                continue
        out.append(line)
        i += 1
    return out, changed


def normalize_markdown(md: str) -> tuple[str, list[str]]:
    """Deterministically repair common GFM breakages.

    Returns ``(fixed_md, fixes_applied)`` where ``fixes_applied`` is a list of
    short tags describing what changed (empty if nothing changed).
    """
    if not md or "|" not in md and "---" not in md and "***" not in md and "___" not in md:
        # Fast path: nothing that could be a table or hr.
        return md, []

    lines = md.split("\n")
    lines, _gap_fixed = _pull_delimiters_to_headers(lines)
    out: list[str] = []
    fixes: list[str] = []
    in_fence = False
    i = 0
    n = len(lines)

    def _last_nonfence_is_blank() -> bool:
        return (not out) or out[-1].strip() == ""

    while i < n:
        line = lines[i]

        # Track fenced code blocks — never touch their interior.
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue

        # --- Table block detection: header row + delimiter row ---
        is_header = _looks_like_table_row(line)
        next_line = lines[i + 1] if i + 1 < n else ""
        next_is_delim = _is_delim(next_line) if next_line else False
        # Also treat "header then data rows but no delimiter" as a table needing a delimiter.
        next_is_rowish = _looks_like_table_row(next_line) if next_line else False

        if is_header and (next_is_delim or next_is_rowish) and _PIPE_ROW_RE.match(line):
            header_cells = _split_cells(line)
            n_cols = len(header_cells)

            # Ensure a blank line before the table.
            if not _last_nonfence_is_blank():
                out.append("")
                fixes.append("blank-line-before-table")

            # Emit normalized header.
            out.append(_normalize_row(header_cells, n_cols))
            i += 1

            # Delimiter row: repair if malformed, insert if missing.
            if i < n and _is_delim(lines[i]):
                delim_cells = _split_cells(lines[i])
                if len(delim_cells) != n_cols:
                    out.append(_make_delim(n_cols))
                    fixes.append("delimiter-row-repair")
                else:
                    out.append(_make_delim(n_cols))  # canonicalize spacing
                i += 1
            else:
                out.append(_make_delim(n_cols))
                fixes.append("delimiter-row-insert")

            # Skip any duplicate delimiter rows (malformed double-delimiter tables).
            while i < n and _is_delim(lines[i]):
                fixes.append("delimiter-dedup")
                i += 1

            # Body rows: consume contiguous table-ish lines, pad/truncate.
            while i < n and lines[i].strip() != "" and _looks_like_table_row(lines[i]) \
                    and not _FENCE_RE.match(lines[i]) and not _HR_RE.match(lines[i]):
                body_cells = _split_cells(lines[i])
                if len(body_cells) != n_cols:
                    fixes.append("cell-count-pad")
                out.append(_normalize_row(body_cells, n_cols))
                i += 1

            # Ensure a blank line after the table.
            if i < n and lines[i].strip() != "":
                out.append("")
                fixes.append("blank-line-after-table")
            continue

        # --- Horizontal rule: ensure blank line before (avoid setext H2) ---
        if _HR_RE.match(line):
            if not _last_nonfence_is_blank():
                out.append("")
                fixes.append("blank-line-before-hr")
            out.append(line)
            i += 1
            # Ensure blank line after hr too.
            if i < n and lines[i].strip() != "":
                out.append("")
            continue

        out.append(line)
        i += 1

    # Collapse 3+ consecutive blank lines into 2.
    collapsed: list[str] = []
    blank_run = 0
    for ln in out:
        if ln.strip() == "":
            blank_run += 1
            if blank_run <= 2:
                collapsed.append(ln)
        else:
            blank_run = 0
            collapsed.append(ln)

    result = "\n".join(collapsed).strip() + ("\n" if md.endswith("\n") else "")
    result = result.rstrip("\n") if not md.endswith("\n") else result

    if collapsed != out:
        fixes.append("collapse-blank-lines")

    if _gap_fixed:
        fixes.append("table-blank-gap")

    # Dedupe fix tags with counts for a compact report.
    if fixes:
        from collections import Counter
        counts = Counter(fixes)
        fixes = [f"{tag} x{c}" if c > 1 else tag for tag, c in counts.items()]

    return result, fixes


# An ATX heading marker (#..######) that is NOT at the start of a line — i.e. it
# has other content before it on the same line. Signals a heading the LLM crammed
# onto a line with surrounding blocks (missing newlines).
_INLINE_HEADING_RE = re.compile(r'\S[^\n]*?\s#{1,6}\s+\S')
# A run of 3+ rule characters.
_RULE_RUN_RE = re.compile(r'(?:^|\s)(?:-{3,}|\*{3,}|_{3,})(?:\s|$)')


def _has_crammed_blocks(md: str) -> bool:
    """Detect block markers (ATX headings / horizontal rules) crammed onto a line
    together with other content because the LLM omitted the surrounding newlines.

    The deterministic line-based pass cannot split these safely — it cannot know
    where a heading ends and its following paragraph begins — so they require LLM
    repair. Fenced code blocks are ignored.
    """
    in_fence = False
    for line in md.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue
        # Heading marker with content before it on the same line.
        if _INLINE_HEADING_RE.search(line):
            return True
        # Inline horizontal rule: a non-table line containing a --- / *** / ___ run
        # AND other non-rule content (a standalone HR line is fine — handled
        # deterministically; a table delimiter line starts with '|' and is skipped).
        if not line.lstrip().startswith("|") and _RULE_RUN_RE.search(line):
            residue = re.sub(r'-{3,}|\*{3,}|_{3,}', '', line).strip()
            if residue:
                return True
    return False


def _has_table_anomaly(md: str) -> bool:
    """Detect malformed tables the deterministic pass may not have repaired:
    consecutive (duplicate) delimiter rows, or a header separated from its
    delimiter by a blank line."""
    if "|" not in md:
        return False
    lines = md.split("\n")
    in_fence = False
    n = len(lines)
    for i in range(n - 1):
        if _FENCE_RE.match(lines[i]):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _is_delim(lines[i]) and _is_delim(lines[i + 1]):
            return True
        if _PIPE_ROW_RE.match(lines[i]) and lines[i + 1].strip() == "":
            j = i + 1
            while j < n and lines[j].strip() == "":
                j += 1
            if j < n and _is_delim(lines[j]):
                return True
    return False


def needs_llm_repair(md: str) -> bool:
    """True when markdown is structurally broken in a way the deterministic pass
    cannot safely fix, so it should be handed to the Haiku repair fallback:

    - block markers (headings / horizontal rules) crammed onto a line with other
      content (LLM dropped the newlines), or
    - a malformed table (duplicate delimiter rows, header/delimiter split by a
      blank line), or
    - a table whose header and delimiter column counts disagree.
    """
    if not md:
        return False
    if _has_crammed_blocks(md):
        return True
    if _has_table_anomaly(md):
        return True
    if "|" in md:
        lines = md.split("\n")
        in_fence = False
        for i in range(len(lines) - 1):
            if _FENCE_RE.match(lines[i]):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if _PIPE_ROW_RE.match(lines[i]) and _is_delim(lines[i + 1]):
                if len(_split_cells(lines[i])) != len(_split_cells(lines[i + 1])):
                    return True
    return False


def _haiku_repair_block(md: str) -> str | None:
    """Send markdown to Claude Haiku to repair table/structure syntax only.

    Returns the repaired markdown, or None on any failure (caller falls back).
    """
    try:
        import anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key.strip())
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8000,
            temperature=0,
            system=(
                "You repair malformed Markdown so it renders as valid GitHub-"
                "Flavored Markdown (GFM). Fix ONLY structure/whitespace:\n"
                "- Put each ATX heading (#, ##, ###) on its own line with a blank "
                "line before and after it. If a heading was run together with the "
                "paragraph that follows it, split them onto separate lines.\n"
                "- Put each horizontal rule (---) on its own line with a blank line "
                "before and after.\n"
                "- Ensure every table has exactly one delimiter row (| --- | --- |) "
                "immediately under its header (no blank line between, no duplicate "
                "delimiter rows), a blank line before and after the table, and a "
                "consistent column count per row.\n"
                "- Separate paragraphs and list blocks with blank lines.\n"
                "CRITICAL: Do NOT change, add, remove, summarize, reorder, or "
                "truncate any content — preserve every word, number, ID, link, and "
                "every table row exactly. You are only inserting/normalizing "
                "newlines and pipe/delimiter syntax. Return ONLY the corrected "
                "Markdown — no commentary, no surrounding code fences."
            ),
            messages=[{"role": "user", "content": md}],
        )
        text = resp.content[0].text.strip()
        # Strip an accidental wrapping fence if the model added one.
        if text.startswith("```"):
            text = re.sub(r'^```[a-zA-Z]*\n', '', text)
            text = re.sub(r'\n```$', '', text)
        return text.strip() or None
    except Exception as exc:  # noqa: BLE001 — never let repair crash the response
        logger.warning("Haiku markdown repair failed: %s", exc)
        return None


@lru_cache(maxsize=256)
def repair_markdown(md: str, *, allow_llm: bool = True) -> str:
    """Full repair pipeline: deterministic pass, then (if still broken and
    allowed) a Haiku fallback. Emits a ``markdown_normalized`` event + logs
    when anything changes. Never raises — returns at least the deterministic
    output.

    Memoized on the input string: the same markdown is normalized at most once
    per process. This eliminates the redundant second Haiku call within a single
    confirm (``clean_response_json`` and ``extract_markdown`` both repair the same
    string) and makes a cached-answer replay return byte-identical markdown
    instantly (the slow, non-deterministic Haiku step is not re-run).
    """
    if not md:
        return md

    fixed, fixes = normalize_markdown(md)
    llm_used = False

    if allow_llm and needs_llm_repair(fixed):
        repaired = _haiku_repair_block(fixed)
        # Guard against content loss/truncation: a structural repair only inserts
        # whitespace/pipes, so it should never come back materially shorter. If it
        # does, the model dropped content — discard it and keep the deterministic
        # output (never worse than today).
        if repaired and repaired != fixed and len(repaired) >= 0.9 * len(fixed):
            fixed = repaired
            llm_used = True
        elif repaired and len(repaired) < 0.9 * len(fixed):
            logger.warning(
                "Haiku markdown repair rejected: output %d chars vs input %d "
                "(possible content loss)", len(repaired), len(fixed),
            )

    if fixes or llm_used:
        try:
            from stream_events import emit
            emit("markdown_normalized", {
                "changed": True,
                "fixes": fixes,
                "llm_repair": llm_used,
            })
        except Exception:  # noqa: BLE001 — monitoring must never break output
            pass
        logger.info("markdown_normalized: fixes=%s llm_repair=%s", fixes, llm_used)

    return fixed
