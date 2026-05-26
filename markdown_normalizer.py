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


def normalize_markdown(md: str) -> tuple[str, list[str]]:
    """Deterministically repair common GFM breakages.

    Returns ``(fixed_md, fixes_applied)`` where ``fixes_applied`` is a list of
    short tags describing what changed (empty if nothing changed).
    """
    if not md or "|" not in md and "---" not in md and "***" not in md and "___" not in md:
        # Fast path: nothing that could be a table or hr.
        return md, []

    lines = md.split("\n")
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

    # Dedupe fix tags with counts for a compact report.
    if fixes:
        from collections import Counter
        counts = Counter(fixes)
        fixes = [f"{tag} x{c}" if c > 1 else tag for tag, c in counts.items()]

    return result, fixes


def needs_llm_repair(md: str) -> bool:
    """True if a table block still has a header whose column count disagrees with
    its delimiter row after the deterministic pass (genuinely ambiguous)."""
    if not md or "|" not in md:
        return False
    lines = md.split("\n")
    in_fence = False
    for i in range(len(lines) - 1):
        if _FENCE_RE.match(lines[i]):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _PIPE_ROW_RE.match(lines[i]) and _is_delim(lines[i + 1]):
            header_cols = len(_split_cells(lines[i]))
            delim_cols = len(_split_cells(lines[i + 1]))
            if header_cols != delim_cols:
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
            max_tokens=2048,
            temperature=0,
            system=(
                "You repair malformed Markdown so it renders as valid GitHub-"
                "Flavored Markdown. Fix ONLY structural/table syntax: pipe "
                "alignment, delimiter rows, blank lines around tables and "
                "horizontal rules. Do NOT change, add, or remove any values, "
                "numbers, words, or links. Return ONLY the corrected Markdown, "
                "no commentary, no code fences around the whole thing."
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


def repair_markdown(md: str, *, allow_llm: bool = True) -> str:
    """Full repair pipeline: deterministic pass, then (if still broken and
    allowed) a Haiku fallback. Emits a ``markdown_normalized`` event + logs
    when anything changes. Never raises — returns at least the deterministic
    output.
    """
    if not md:
        return md

    fixed, fixes = normalize_markdown(md)
    llm_used = False

    if allow_llm and needs_llm_repair(fixed):
        repaired = _haiku_repair_block(fixed)
        if repaired and repaired != fixed:
            fixed = repaired
            llm_used = True

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
