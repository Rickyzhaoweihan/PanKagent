"""FormatAgent skill — main formatting pipeline using Claude Opus 4.6.

This is the core script that:
1. Compresses Neo4j results
2. Builds the prompt input
3. Calls Claude Opus 4.6 with the appropriate system prompt
4. Checks for hallucinations
5. Auto-cleans fake IDs
6. Returns structured JSON
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import anthropic

# Structured streaming events
_repo_root_for_events = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _repo_root_for_events not in sys.path:
    sys.path.insert(0, _repo_root_for_events)
from stream_events import emit

from hallucination_checker import check_hallucination, remove_hallucinated_ids


# ---------------------------------------------------------------------------
# Safety: detect when ALL retrieved data is empty / errors
# ---------------------------------------------------------------------------

_NO_DATA_RESPONSE = json.dumps({
    "to": "user",
    "text": {
        "template_matching": "agent_answer",
        "cypher": [],
        "summary": "Unfortunately, I cannot answer this question. The knowledge graph did not return any useful results for the queries that were executed. This may be because the entities or relationships you asked about are not present in the current PanKgraph database. Please try rephrasing your question or asking about a different entity."
    }
}, ensure_ascii=False)


def _extract_results_text(result: dict | str) -> str:
    """Extract a text representation of query results.

    Handles four formats:
    - **Old Neo4j**: ``{"results": "...string..."}``
    - **New Neo4j**: ``{"records": [...], "keys": [...]}``
    - **HPAP MySQL**: ``{"rows": [...], "row_count": N, "source": "hpap"}``
    - **Genomic PostgreSQL**: ``{"rows": [...], "row_count": N, "source": "genomic"}``
    """
    if not isinstance(result, dict):
        return str(result)

    # HPAP format: tabular rows from MySQL
    if result.get("source") == "hpap":
        rows = result.get("rows", [])
        if not rows:
            return "No results"
        explanation = result.get("explanation", "")
        prefix = f"[HPAP metadata — {explanation}]\n" if explanation else "[HPAP metadata]\n"
        return prefix + json.dumps(rows, default=str, indent=2)

    # ssGSEA enrichment scores
    if result.get("source") == "ssgsea":
        rows = result.get("rows", [])
        genes_used = result.get("genes_used", "?")
        genes_not_found = result.get("genes_not_found", [])
        total_donors = result.get("total_donors", len(rows))
        summary = result.get("summary_by_diabetes_status", {})

        parts = [f"[ssGSEA immune-cell enrichment — {genes_used} genes scored across {total_donors} donors]"]
        if genes_not_found:
            parts.append(f"Genes not found in dataset: {', '.join(genes_not_found)}")

        if summary:
            parts.append("\nMean enrichment score by diabetes status:")
            for status, stats in summary.items():
                parts.append(f"  {status}: {stats['mean']:.4f} (n={stats['n']})")

        if rows:
            parts.append(f"\nTop {len(rows)} donors by enrichment score (sorted descending):")
            parts.append(json.dumps(rows, default=str, indent=2))

        if not rows and not summary:
            return "No results"

        return "\n".join(parts)

    # Functional Data API: islet assay measurements
    if result.get("source") == "functional_data":
        endpoint = result.get("endpoint", "?")
        params = result.get("params", {})
        rows = result.get("rows", [])
        parts = [f"[Functional Data API — {endpoint}]",
                 f"Parameters used: {json.dumps(params)}"]
        # Include the full URL so the FormatAgent can echo it back to the user
        if result.get("url"):
            parts.append(f"URL: {result['url']}")
        # Endpoint-specific extras
        for key in ("trait", "trace_type", "y_label", "available_donors",
                    "trace_types", "options", "ranges", "times", "mean", "stimuli"):
            if key in result and result[key] is not None:
                parts.append(f"{key}: {json.dumps(result[key], default=str)}")
        if rows:
            parts.append(f"\nData ({len(rows)} rows):")
            parts.append(json.dumps(rows, default=str, indent=2))
        elif not any(result.get(k) for k in ("options", "ranges", "mean")):
            return "No results"
        return "\n".join(parts)

    # Genomic coordinate format: tabular rows from PostgreSQL
    if result.get("source") == "genomic":
        rows = result.get("rows", [])
        if not rows:
            return "No results"
        return f"[Genomic coordinates — {len(rows)} rows]\n" + json.dumps(rows, default=str, indent=2)

    # Old format: results is a plain string
    if "results" in result:
        return result["results"] if isinstance(result["results"], str) else json.dumps(result["results"])

    # New format: structured records list
    if "records" in result:
        records = result["records"]
        if not records:
            return "No results"
        return json.dumps(records, default=str)

    return ""


def _has_useful_data(neo4j_results: list[dict]) -> bool:
    """Return True if at least one result (Neo4j or HPAP) contains non-empty data."""
    for entry in neo4j_results:
        result = entry.get("result", {})
        if isinstance(result, dict) and "error" in result:
            continue
        # HPAP/genomic results: check rows directly
        if isinstance(result, dict) and result.get("source") in ("hpap", "genomic", "ssgsea", "functional_data"):
            if result.get("rows"):
                return True
            continue
        results_value = _extract_results_text(result)
        if isinstance(results_value, str):
            norm = results_value.strip().lower()
            if not norm or norm == "no results":
                continue
            if "nodes, edges" in norm and (
                "[], []" in norm or "[][]" in norm.replace(" ", "")
            ):
                continue
        return True
    return False


# ---------------------------------------------------------------------------
# Token-budget truncation for Claude API calls
# ---------------------------------------------------------------------------

CLAUDE_CONTEXT_WINDOW = 150_000
CLAUDE_MAX_OUTPUT_TOKENS = 8_192
_CHARS_PER_TOKEN_ESTIMATE = 3.2


def truncate_neo4j_results_for_claude(
    neo4j_results: list[dict],
    system_prompt: str,
    other_text: str,
    max_tokens: int = CLAUDE_CONTEXT_WINDOW,
) -> list[dict]:
    """Truncate individual Neo4j result texts so the total prompt fits within
    *max_tokens*.  The function keeps all queries but trims the largest result
    texts first (from the bottom of each result) until the budget is met.

    The budget reserves ``CLAUDE_MAX_OUTPUT_TOKENS`` for the response and uses
    a conservative chars-per-token estimate (3.2) because Neo4j JSON with short
    property names and IDs tokenizes more densely than natural language.

    Returns a **new** list (same structure) with trimmed ``results`` strings.
    """
    usable_input_tokens = max_tokens - CLAUDE_MAX_OUTPUT_TOKENS
    overhead_chars = len(system_prompt) + len(other_text) + 4000
    budget_chars = int(usable_input_tokens * _CHARS_PER_TOKEN_ESTIMATE) - overhead_chars
    if budget_chars < 10_000:
        budget_chars = 10_000

    entries: list[dict] = []
    for entry in neo4j_results:
        result = entry.get("result", {})
        text = _extract_results_text(result)
        entries.append({
            "entry": entry,
            "text": text,
            "chars": len(text),
        })

    total_chars = sum(e["chars"] for e in entries)
    if total_chars <= budget_chars:
        return neo4j_results

    per_query_budget = max(budget_chars // max(len(entries), 1), 5_000)

    truncated: list[dict] = []
    for e in entries:
        original_entry = e["entry"]
        text = e["text"]
        if len(text) > per_query_budget:
            text = text[:per_query_budget] + (
                f"\n\n... [TRUNCATED — original result was {e['chars']:,} chars, "
                f"showing first {per_query_budget:,} chars to fit token budget] ..."
            )
            result_copy = dict(original_entry.get("result", {})) if isinstance(
                original_entry.get("result"), dict) else {"results": text}
            result_copy["results"] = text
            truncated.append({**original_entry, "result": result_copy})
        else:
            truncated.append(original_entry)

    return truncated


from prompts import FORMAT_PROMPT_WITH_LITERATURE, FORMAT_PROMPT_NO_LITERATURE


# Claude model
CLAUDE_MODEL = "claude-sonnet-4-6"

# Performance log path (same as performance_monitor.py)
_PERF_LOG = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')),
    'logs', 'performance.log'
)


def _perf_log(function: str, status: str, elapsed_ms: float, **extra):
    """Write a timing entry to the shared performance log."""
    entry = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "module": "FormatAgent",
        "function": function,
        "status": status,
        "elapsed_ms": round(elapsed_ms, 3),
        **extra,
    }
    try:
        os.makedirs(os.path.dirname(_PERF_LOG), exist_ok=True)
        with open(_PERF_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _get_client() -> anthropic.Anthropic:
    """Get Anthropic client, reading API key from env or config."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Try importing from config.py in the project root
        try:
            project_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), '..', '..', '..'))
            sys.path.insert(0, project_root)
            import config
            api_key = getattr(config, 'ANTHROPIC_API_KEY', None)
        except ImportError:
            pass
    if not api_key or api_key == 'YOUR_ANTHROPIC_API_KEY_HERE':
        raise EnvironmentError(
            "ANTHROPIC_API_KEY not found. Set it as an environment variable "
            "or in config.py in the project root.")
    return anthropic.Anthropic(api_key=api_key.strip())


def _extract_json_from_response(text: str) -> str:
    """Extract and repair JSON from Claude's response.

    Handles: markdown code blocks, raw JSON, truncated output, and
    unescaped characters inside string values that break naive parsing.
    """
    text = text.strip()

    # Strip markdown code fences if present
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if json_match:
        text = json_match.group(1).strip()

    # Fast path: already valid JSON
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, ValueError):
        pass

    # Locate the outermost JSON object using a string-aware scanner
    brace_start = text.find('{')
    if brace_start != -1:
        candidate = _find_balanced_json(text, brace_start)
        if candidate:
            try:
                json.loads(candidate)
                return candidate
            except (json.JSONDecodeError, ValueError):
                pass
            repaired = _repair_truncated_json(candidate)
            if repaired:
                return repaired

    if brace_start != -1:
        repaired = _repair_truncated_json(text[brace_start:])
        if repaired:
            return repaired

    return text


def _find_balanced_json(text: str, start: int) -> str | None:
    """Find the outermost balanced { ... } respecting JSON string quoting."""
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == '\\':
                i += 2
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return text[start:] if depth > 0 else None


def _repair_truncated_json(text: str) -> str | None:
    """Attempt to close truncated JSON by appending missing braces/brackets."""
    sanitized = text
    for _ in range(3):
        try:
            json.loads(sanitized)
            return sanitized
        except json.JSONDecodeError as exc:
            msg = str(exc)
            if 'Unterminated string' in msg or 'Expecting' in msg or 'end of' in msg:
                in_str = False
                open_stack: list[str] = []
                j = 0
                while j < len(sanitized):
                    c = sanitized[j]
                    if in_str:
                        if c == '\\':
                            j += 2
                            continue
                        if c == '"':
                            in_str = False
                    else:
                        if c == '"':
                            in_str = True
                        elif c in ('{', '['):
                            open_stack.append('}' if c == '{' else ']')
                        elif c in ('}', ']'):
                            if open_stack:
                                open_stack.pop()
                    j += 1
                suffix = ''
                if in_str:
                    suffix += '"'
                suffix += ''.join(reversed(open_stack))
                if suffix:
                    sanitized = sanitized + suffix
                    continue
            break
    try:
        json.loads(sanitized)
        return sanitized
    except (json.JSONDecodeError, ValueError):
        return None


def format_response(
    human_query: str,
    neo4j_results: list[dict],
    cypher_queries: list[str],
    literature_text: str = "",
    use_literature: bool = False,
    pre_final_answer: str = "",
) -> str:
    """
    Call Claude Opus 4.6 to format the response.

    Args:
        human_query: The user's original question.
        neo4j_results: Raw Neo4j results (list of dicts with 'query' and 'result').
        cypher_queries: List of Cypher query strings.
        literature_text: Raw HIRN literature skill output (only if use_literature=True).
        use_literature: Whether HIRN literature skill was used.
        pre_final_answer: Pre-final answer from the planner agent.

    Returns:
        Raw JSON string from Claude.
    """
    client = _get_client()

    system_prompt = FORMAT_PROMPT_WITH_LITERATURE
    emit("format_claude_start", {"mode": "KG-ONLY", "model": CLAUDE_MODEL})

    # Truncate Neo4j results if they would exceed the token budget
    other_text = f"Human Query: {human_query}\n\n{json.dumps(cypher_queries, indent=2)}"
    neo4j_results = truncate_neo4j_results_for_claude(
        neo4j_results, system_prompt, other_text
    )

    neo4j_sections = []
    for i, entry in enumerate(neo4j_results, 1):
        query = entry.get('query', '')
        result = entry.get('result', {})
        results_text = _extract_results_text(result)
        source = result.get("source") if isinstance(result, dict) else None
        query_label = {"hpap": "SQL (HPAP)", "genomic": "SQL (Genomic)", "ssgsea": "ssGSEA",
                       "functional_data": "Functional API"}.get(source, "Cypher")
        neo4j_sections.append(
            f"--- Query {i} ---\n"
            f"{query_label}: {query}\n"
            f"Result:\n{results_text}"
        )
    raw_neo4j_block = '\n\n'.join(neo4j_sections) if neo4j_sections else '(no results)'

    user_input = f"""Human Query: {human_query}

=== QUERIES ===
{json.dumps(cypher_queries, indent=2)}

=== DATABASE RESULTS (RAW — {len(neo4j_results)} queries) ===
{raw_neo4j_block}

CRITICAL INSTRUCTIONS:
- The above contains ALL raw data retrieved from the databases.
- Neo4j query results show NODES and EDGES. Edges encode relationships BETWEEN nodes.
- Pay close attention to WHICH nodes each edge connects (use start/end IDs to match).
- Extract ALL property values from edges (expression means, p-values, fold changes, etc.).
- If a gene appears in the nodes list, it IS in the data — do NOT say it is missing.
- Cross-reference: if the user asks about gene X, check if X appears in ANY node's "name" field above.
- Cross-source chain handling: if an ssGSEA or genomic (SQL) result appears alongside KG results, the two may be linked — the ssGSEA gene set may have come from the KG step's retrieved genes. Join them narratively: "ssGSEA was run on the <N> effector genes retrieved in step 1: ..." When relevant, filter ssGSEA scores to donors that match any cohort filter from another KG step (e.g., female T1D Stage 3 donors).
- Functional Data API: if a Functional Data API result appears, echo back the endpoint and parameters used verbatim (they are included in the result under "Parameters used" and "URL"). If the result came from a chain where donor_ids were passed from a prior KG step, state this explicitly: "Functional data was retrieved for the <N> donors identified in step 1." Interpret traits using domain knowledge (INS = insulin, GCG = glucagon; IEQ = islet equivalent normalization; AUC = integrated secretion; SI = stimulation index; II = inhibition index)."""

    if pre_final_answer:
        user_input += f"\n\nPre-Final Answer from Planner: {pre_final_answer}"

    emit("format_claude_input", {
        "input_chars": len(user_input),
        "num_queries": len(neo4j_results),
    })

    # DEBUG: dump full prompt + retrieved data to file for inspection
    _debug_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
    os.makedirs(_debug_dir, exist_ok=True)
    _debug_path = os.path.join(_debug_dir, "format_debug_prompt.txt")
    with open(_debug_path, "w") as _df:
        _df.write(f"=== SYSTEM PROMPT ({len(system_prompt)} chars) ===\n")
        _df.write(system_prompt)
        _df.write(f"\n\n=== USER INPUT ({len(user_input)} chars) ===\n")
        _df.write(user_input)
        _df.write(f"\n\n=== RAW NEO4J BLOCK ({len(raw_neo4j_block)} chars) ===\n")
        _df.write(raw_neo4j_block)
        _df.write(f"\n\n=== NEO4J RESULTS COUNT: {len(neo4j_results)} ===\n")
        for i, entry in enumerate(neo4j_results, 1):
            result = entry.get('result', {})
            results_text = _extract_results_text(result)
            _df.write(f"\n--- Result {i} length: {len(results_text)} chars ---\n")
            _df.write(results_text)
            _df.write("\n")
    emit("format_debug_dump", {
        "path": _debug_path,
        "user_input_chars": len(user_input),
        "neo4j_block_chars": len(raw_neo4j_block),
    })

    # Call Claude (timed)
    t0 = time.perf_counter()

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        temperature=0.2,
        system=system_prompt,
        messages=[{"role": "user", "content": user_input}]
    )

    claude_elapsed = time.perf_counter() - t0
    _in_tok = response.usage.input_tokens
    _out_tok = response.usage.output_tokens
    _cost_input = _in_tok * 5.0 / 1_000_000   # $5 / MTok
    _cost_output = _out_tok * 25.0 / 1_000_000  # $25 / MTok
    _cost_total = _cost_input + _cost_output
    emit("format_claude_done", {
        "elapsed_s": round(claude_elapsed, 2),
        "input_tokens": _in_tok,
        "output_tokens": _out_tok,
        "cost_input_usd": round(_cost_input, 6),
        "cost_output_usd": round(_cost_output, 6),
        "cost_total_usd": round(_cost_total, 6),
    })
    _perf_log("format_response_claude_call", "success", claude_elapsed * 1000,
              input_tokens=_in_tok,
              output_tokens=_out_tok,
              cost_usd=round(_cost_total, 6))

    raw_text = response.content[0].text

    # DEBUG: append Claude's raw output to the debug file
    try:
        with open(_debug_path, "a") as _df:
            _df.write(f"\n\n=== CLAUDE RAW OUTPUT ({len(raw_text)} chars, stop_reason={response.stop_reason}) ===\n")
            _df.write(raw_text)
            _df.write("\n")
    except Exception:
        pass

    result = _extract_json_from_response(raw_text)
    return result


def run_format_pipeline(
    human_query: str,
    neo4j_results: list[dict],
    cypher_queries: list[str],
    functions_result: str = "",
    use_glkb: bool = False,
    pre_final_answer: str = "",
) -> str:
    """
    Complete end-to-end formatting pipeline:
    1. Pass raw Neo4j results directly (no compression)
    2. Extract HIRN literature section (if applicable)
    3. Call Claude FormatAgent
    4. Check hallucinations
    5. Auto-clean fake IDs
    6. Return final JSON string

    Args:
        human_query: The user's original question.
        neo4j_results: Raw Neo4j results (list of dicts with 'query' and 'result').
        cypher_queries: List of Cypher query strings.
        functions_result: Raw concatenated output from all sub-agents.
        use_glkb: Whether HIRN literature skill was used (kept as use_glkb for API compat).
        pre_final_answer: Pre-final answer from the planner agent.

    Returns:
        Final JSON string ready to return to the user.
    """
    pipeline_start = time.perf_counter()
    emit("format_start", {
        "human_query": human_query[:200],
        "num_neo4j_results": len(neo4j_results),
        "use_literature": use_glkb,
    })

    # Safety check: if ALL Neo4j results are empty/errors, return early
    if not _has_useful_data(neo4j_results):
        emit("format_no_data", {"reason": "all Neo4j results empty or errored"})
        return _NO_DATA_RESPONSE

    # Step 1: Skip compression — pass raw Neo4j results directly to Claude
    emit("format_compress", {
        "skipped": True,
        "reason": "raw results preserve all node/edge properties",
        "num_results": len(neo4j_results),
    })

    # Step 2: Call Claude FormatAgent with raw Neo4j results (no literature — spliced post-hoc)
    format_result = format_response(
        human_query=human_query,
        neo4j_results=neo4j_results,
        cypher_queries=cypher_queries,
        literature_text="",
        use_literature=False,
        pre_final_answer=pre_final_answer,
    )

    # Emit raw output (truncated for streaming)
    emit("format_raw_output", {"output": format_result[:3000]})

    # Step 4: Hallucination check + auto-cleanup
    t4 = time.perf_counter()
    try:
        format_json = json.loads(format_result)
        summary_text = format_json.get('text', {}).get('summary', '')

        report = check_hallucination(
            summary=summary_text,
            neo4j_results=neo4j_results,
            raw_agent_output=functions_result
        )
        emit("format_halluc_check", {
            "is_clean": report["is_clean"],
            "hallucinated_go_terms": report["hallucinated_go_terms"],
            "hallucinated_pubmed_ids": report["hallucinated_pubmed_ids"],
            "summary_go_count": len(report["summary_ids"]["go_terms"]),
            "summary_pubmed_count": len(report["summary_ids"]["pubmed_ids"]),
        })

        if not report['is_clean']:
            # Step 5: Auto-remove hallucinated IDs
            cleaned_summary = remove_hallucinated_ids(
                summary_text,
                report['hallucinated_go_terms'],
                report['hallucinated_pubmed_ids']
            )
            format_json['text']['summary'] = cleaned_summary
            format_json['text']['hallucination_check'] = {
                'is_clean': False,
                'removed_go_terms': report['hallucinated_go_terms'],
                'removed_pubmed_ids': report['hallucinated_pubmed_ids'],
                'note': 'Fake IDs were automatically removed from the summary'
            }
            format_result = json.dumps(format_json)
            emit("format_auto_clean", {
                "removed_pubmed_ids": len(report["hallucinated_pubmed_ids"]),
                "removed_go_terms": len(report["hallucinated_go_terms"]),
            })

    except Exception as e:
        emit("format_halluc_check", {"error": str(e)})

    halluc_elapsed = time.perf_counter() - t4
    _perf_log("hallucination_check", "success", halluc_elapsed * 1000)

    # Total pipeline timing
    pipeline_elapsed = time.perf_counter() - pipeline_start
    emit("format_done", {
        "elapsed_s": round(pipeline_elapsed, 2),
        "halluc_check_s": round(halluc_elapsed, 2),
    })
    _perf_log("run_format_pipeline", "success", pipeline_elapsed * 1000)

    return format_result

