"""ReasoningAgent skill — multi-hop reasoning pipeline using Claude Opus 4.6.

This is the core script that:
1. Compresses Neo4j results
2. Builds the prompt input (same data as FormatAgent, different system prompt)
3. Calls Claude Opus 4.6 with the reasoning system prompt
4. Checks for hallucinations
5. Auto-cleans fake IDs
6. Returns structured JSON with a reasoning_trace field

The ReasoningAgent is invoked for complex questions that require multi-hop
inference, set operations, or cross-data-source integration. The PlannerAgent
decides at the start whether to route to ReasoningAgent or FormatAgent.
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

# Import reasoning prompts FIRST (before touching sys.path) using importlib
# to avoid name collision with format-agent's prompts.py
import importlib.util as _ilu
_prompts_path = os.path.join(os.path.dirname(__file__), 'prompts.py')
_spec = _ilu.spec_from_file_location("reasoning_prompts", _prompts_path)
_reasoning_prompts = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_reasoning_prompts)
REASONING_PROMPT_WITH_LITERATURE = _reasoning_prompts.REASONING_PROMPT_WITH_LITERATURE
REASONING_PROMPT_NO_LITERATURE = _reasoning_prompts.REASONING_PROMPT_NO_LITERATURE

# Reuse hallucination_checker from the format-agent skill
_format_agent_scripts = os.path.join(
    os.path.dirname(__file__), '..', '..', 'format-agent', 'scripts'
)
sys.path.insert(0, os.path.abspath(_format_agent_scripts))
from hallucination_checker import check_hallucination, remove_hallucinated_ids
from format_response import _has_useful_data, _NO_DATA_RESPONSE, truncate_neo4j_results_for_claude, _extract_results_text


# Claude model
CLAUDE_MODEL = "claude-sonnet-4-6"

# Performance log path (same as format-agent and performance_monitor.py)
_PERF_LOG = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')),
    'logs', 'performance.log'
)


def _perf_log(function: str, status: str, elapsed_ms: float, **extra):
    """Write a timing entry to the shared performance log."""
    entry = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "module": "ReasoningAgent",
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


def reasoning_response(
    human_query: str,
    neo4j_results: list[dict],
    cypher_queries: list[str],
    literature_text: str = "",
    use_literature: bool = False,
    pre_final_answer: str = "",
) -> str:
    """
    Call Claude Opus 4.6 with the ReasoningAgent prompt to perform multi-hop
    reasoning over the retrieved data.

    NOTE: Unlike the FormatAgent, the ReasoningAgent receives the RAW Neo4j
    results (not compressed). Complex multi-hop questions need full graph
    structure — including which edges connect which nodes — and compression
    strips that information.

    Args:
        human_query: The user's original question.
        neo4j_results: Raw Neo4j results (list of dicts with 'query' and 'result').
        cypher_queries: List of Cypher query strings.
        literature_text: Raw HIRN literature skill output (only if use_literature=True).
        use_literature: Whether HIRN literature skill was used.
        pre_final_answer: Pre-final answer from the planner agent.

    Returns:
        Raw JSON string from Claude (includes reasoning_trace + summary).
    """
    client = _get_client()

    system_prompt = REASONING_PROMPT_WITH_LITERATURE
    emit("reasoning_claude_start", {"mode": "KG-ONLY", "model": CLAUDE_MODEL})

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
- Pay close attention to WHICH nodes each edge connects (source → target).
- DECOMPOSE the question into sub-questions FIRST, then reason step-by-step.
- Cross-reference data across queries: e.g. if Query 1 returns SNPs and Query 2 returns genes,
  check if any edges in Query 1 connect those SNPs to those genes.
- Show your full reasoning chain in the reasoning_trace field.
- The summary should synthesize your reasoning into a direct answer."""

    if pre_final_answer:
        user_input += f"\n\nPre-Final Answer from Planner: {pre_final_answer}"

    emit("reasoning_claude_input", {
        "input_chars": len(user_input),
        "num_queries": len(neo4j_results),
    })

    # DEBUG: dump full prompt + retrieved data to file for inspection
    _debug_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
    os.makedirs(_debug_dir, exist_ok=True)
    _debug_path = os.path.join(_debug_dir, "reasoning_debug_prompt.txt")
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
    emit("reasoning_debug_dump", {"path": _debug_path, "user_input_chars": len(user_input), "neo4j_block_chars": len(raw_neo4j_block)})

    # Call Claude (timed)
    t0 = time.perf_counter()

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        temperature=0.2,
        system=system_prompt,
        messages=[{"role": "user", "content": user_input}],
    )

    claude_elapsed = time.perf_counter() - t0
    _in_tok = response.usage.input_tokens
    _out_tok = response.usage.output_tokens
    _cost_input = _in_tok * 5.0 / 1_000_000   # $5 / MTok
    _cost_output = _out_tok * 25.0 / 1_000_000  # $25 / MTok
    _cost_total = _cost_input + _cost_output
    emit("reasoning_claude_done", {
        "elapsed_s": round(claude_elapsed, 2),
        "input_tokens": _in_tok,
        "output_tokens": _out_tok,
        "cost_input_usd": round(_cost_input, 6),
        "cost_output_usd": round(_cost_output, 6),
        "cost_total_usd": round(_cost_total, 6),
    })
    _perf_log("reasoning_response_claude_call", "success", claude_elapsed * 1000,
              input_tokens=_in_tok,
              output_tokens=_out_tok,
              cost_usd=round(_cost_total, 6))

    raw_text = response.content[0].text

    # DEBUG: append Claude's raw output + stop reason to the debug file
    with open(_debug_path, "a") as _df:
        _df.write(f"\n\n=== CLAUDE RAW OUTPUT ({len(raw_text)} chars) ===\n")
        _df.write(f"stop_reason: {response.stop_reason}\n")
        _df.write(f"input_tokens: {_in_tok}\n")
        _df.write(f"output_tokens: {_out_tok}\n")
        _df.write(f"max_tokens: 8192\n")
        _df.write(f"cost_input_usd: ${_cost_input:.6f}\n")
        _df.write(f"cost_output_usd: ${_cost_output:.6f}\n")
        _df.write(f"cost_total_usd: ${_cost_total:.6f}\n")
        _df.write(f"\n{raw_text}\n")

    result = _extract_json_from_response(raw_text)
    return result


def run_reasoning_pipeline(
    human_query: str,
    neo4j_results: list[dict],
    cypher_queries: list[str],
    functions_result: str = "",
    use_literature: bool = False,
    pre_final_answer: str = "",
) -> str:
    """
    Complete end-to-end reasoning pipeline:
    1. Compress Neo4j results
    2. Extract HIRN literature section (if applicable)
    3. Call Claude ReasoningAgent (multi-hop reasoning)
    4. Check hallucinations
    5. Auto-clean fake IDs
    6. Return final JSON string (with reasoning_trace)

    Args:
        human_query: The user's original question.
        neo4j_results: Raw Neo4j results (list of dicts with 'query' and 'result').
        cypher_queries: List of Cypher query strings.
        functions_result: Raw concatenated output from all sub-agents.
        use_literature: Whether HIRN literature skill was used.
        pre_final_answer: Pre-final answer from the planner agent.

    Returns:
        Final JSON string ready to return to the user (includes reasoning_trace).
    """
    pipeline_start = time.perf_counter()
    emit("reasoning_start", {
        "human_query": human_query[:200],
        "num_neo4j_results": len(neo4j_results),
        "use_literature": use_literature,
    })

    # Safety check: if ALL Neo4j results are empty/errors, return early
    if not _has_useful_data(neo4j_results):
        emit("reasoning_no_data", {"reason": "all Neo4j results empty or errored"})
        return _NO_DATA_RESPONSE

    # Step 1: NO compression — pass raw Neo4j results directly.
    emit("reasoning_compress", {
        "skipped": True,
        "reason": "complex reasoning needs full graph structure",
        "num_results": len(neo4j_results),
    })

    # Step 2: Call Claude ReasoningAgent with RAW results (no literature — spliced post-hoc)
    reasoning_result = reasoning_response(
        human_query=human_query,
        neo4j_results=neo4j_results,
        cypher_queries=cypher_queries,
        literature_text="",
        use_literature=False,
        pre_final_answer=pre_final_answer,
    )

    # Emit raw output (truncated for streaming)
    emit("reasoning_raw_output", {"output": reasoning_result[:3000]})

    # Step 4: Hallucination check + auto-cleanup
    t4 = time.perf_counter()
    try:
        result_json = json.loads(reasoning_result)
        summary_text = result_json.get('text', {}).get('summary', '')

        report = check_hallucination(
            summary=summary_text,
            neo4j_results=neo4j_results,
            raw_agent_output=functions_result
        )
        emit("reasoning_halluc_check", {
            "is_clean": report["is_clean"],
            "hallucinated_go_terms": report["hallucinated_go_terms"],
            "hallucinated_pubmed_ids": report["hallucinated_pubmed_ids"],
            "summary_go_count": len(report["summary_ids"]["go_terms"]),
            "summary_pubmed_count": len(report["summary_ids"]["pubmed_ids"]),
        })

        if not report['is_clean']:
            # Auto-remove hallucinated IDs from summary
            cleaned_summary = remove_hallucinated_ids(
                summary_text,
                report['hallucinated_go_terms'],
                report['hallucinated_pubmed_ids']
            )
            result_json['text']['summary'] = cleaned_summary
            result_json['text']['hallucination_check'] = {
                'is_clean': False,
                'removed_go_terms': report['hallucinated_go_terms'],
                'removed_pubmed_ids': report['hallucinated_pubmed_ids'],
                'note': 'Fake IDs were automatically removed from the summary'
            }
            reasoning_result = json.dumps(result_json)
            emit("reasoning_auto_clean", {
                "removed_pubmed_ids": len(report["hallucinated_pubmed_ids"]),
                "removed_go_terms": len(report["hallucinated_go_terms"]),
            })

    except Exception as e:
        emit("reasoning_halluc_check", {"error": str(e)})

    halluc_elapsed = time.perf_counter() - t4
    _perf_log("hallucination_check", "success", halluc_elapsed * 1000)

    # Total pipeline timing
    pipeline_elapsed = time.perf_counter() - pipeline_start
    emit("reasoning_done", {
        "elapsed_s": round(pipeline_elapsed, 2),
        "halluc_check_s": round(halluc_elapsed, 2),
    })
    _perf_log("run_reasoning_pipeline", "success", pipeline_elapsed * 1000)

    return reasoning_result

