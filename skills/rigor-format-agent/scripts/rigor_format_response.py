"""RigorFormatAgent skill — strict evidence-only formatting pipeline.

Rigor mode variant of the FormatAgent. Key differences:
- Uses a prompt that enforces absolute evidence requirements
- Short, direct answers — no forced multi-section essay
- Every claim must trace to input data
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import anthropic

_repo_root = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
from stream_events import emit

import importlib.util as _ilu
_prompts_path = os.path.join(os.path.dirname(__file__), 'prompts.py')
_spec = _ilu.spec_from_file_location("rigor_format_prompts", _prompts_path)
_rigor_prompts = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_rigor_prompts)
RIGOR_FORMAT_PROMPT_WITH_LITERATURE = _rigor_prompts.RIGOR_FORMAT_PROMPT_WITH_LITERATURE
RIGOR_FORMAT_PROMPT_NO_LITERATURE = _rigor_prompts.RIGOR_FORMAT_PROMPT_NO_LITERATURE

# Reuse hallucination_checker from format-agent
_format_agent_scripts = os.path.join(
    os.path.dirname(__file__), '..', '..', 'format-agent', 'scripts'
)
sys.path.insert(0, os.path.abspath(_format_agent_scripts))
from hallucination_checker import check_hallucination, remove_hallucinated_ids
from format_response import _has_useful_data, _NO_DATA_RESPONSE, truncate_neo4j_results_for_claude, _extract_results_text

# Functional data domain glossary helper
_fd_skill_dir = os.path.join(_repo_root, "skills", "functional_data")
if _fd_skill_dir not in sys.path:
    sys.path.insert(0, _fd_skill_dir)
from functional_data_client import build_functional_data_glossary

CLAUDE_MODEL = "claude-sonnet-4-6"

_PERF_LOG = os.path.join(_repo_root, 'logs', 'performance.log')


def _perf_log(function: str, status: str, elapsed_ms: float, **extra):
    entry = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "module": "RigorFormatAgent",
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
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        try:
            sys.path.insert(0, _repo_root)
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


def rigor_format_response(
    human_query: str,
    neo4j_results: list[dict],
    cypher_queries: list[str],
    literature_text: str = "",
    use_literature: bool = False,
    pre_final_answer: str = "",
) -> str:
    """Call Claude with the RigorFormatAgent prompt."""
    client = _get_client()

    system_prompt = RIGOR_FORMAT_PROMPT_WITH_LITERATURE
    emit("rigor_format_claude_start", {"mode": "KG-ONLY", "model": CLAUDE_MODEL})

    other_text = f"Human Query: {human_query}\n\n{json.dumps(cypher_queries, indent=2)}"
    neo4j_results = truncate_neo4j_results_for_claude(
        neo4j_results, system_prompt, other_text,
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

    _fd_glossary = build_functional_data_glossary(neo4j_results)
    if _fd_glossary:
        emit("rigor_format_glossary_injected", {"chars": len(_fd_glossary)})

    user_input = f"""Human Query: {human_query}

=== QUERIES ===
{json.dumps(cypher_queries, indent=2)}

=== DATABASE RESULTS (RAW — {len(neo4j_results)} queries) ===
{raw_neo4j_block}
{_fd_glossary}

REMINDER: Only state facts that appear in the data above. If data is missing, say so.

Cross-source chain handling: if an ssGSEA or genomic (SQL) result appears alongside KG results, they may be linked (the ssGSEA gene set was likely retrieved by the KG step). Report scores alongside their upstream gene context when applicable, and filter/partition by any cohort criteria that appear in other steps."""

    if pre_final_answer:
        user_input += f"\n\nPre-Final Answer from Planner: {pre_final_answer}"

    emit("rigor_format_claude_input", {
        "input_chars": len(user_input),
        "num_queries": len(neo4j_results),
    })

    # Debug dump
    _debug_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
    os.makedirs(_debug_dir, exist_ok=True)
    _debug_path = os.path.join(_debug_dir, "rigor_format_debug_prompt.txt")
    with open(_debug_path, "w") as _df:
        _df.write(f"=== SYSTEM PROMPT ({len(system_prompt)} chars) ===\n")
        _df.write(system_prompt)
        _df.write(f"\n\n=== USER INPUT ({len(user_input)} chars) ===\n")
        _df.write(user_input)

    t0 = time.perf_counter()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        temperature=0.1,
        system=system_prompt,
        messages=[{"role": "user", "content": user_input}]
    )
    claude_elapsed = time.perf_counter() - t0

    _in_tok = response.usage.input_tokens
    _out_tok = response.usage.output_tokens
    _cost_input = _in_tok * 5.0 / 1_000_000
    _cost_output = _out_tok * 25.0 / 1_000_000
    _cost_total = _cost_input + _cost_output
    emit("rigor_format_claude_done", {
        "elapsed_s": round(claude_elapsed, 2),
        "input_tokens": _in_tok,
        "output_tokens": _out_tok,
        "cost_total_usd": round(_cost_total, 6),
    })
    _perf_log("rigor_format_response_claude_call", "success", claude_elapsed * 1000,
              input_tokens=_in_tok, output_tokens=_out_tok, cost_usd=round(_cost_total, 6))

    raw_text = response.content[0].text

    try:
        with open(_debug_path, "a") as _df:
            _df.write(f"\n\n=== CLAUDE RAW OUTPUT ({len(raw_text)} chars, stop_reason={response.stop_reason}) ===\n")
            _df.write(raw_text)
    except Exception:
        pass

    return _extract_json_from_response(raw_text)


def run_rigor_format_pipeline(
    human_query: str,
    neo4j_results: list[dict],
    cypher_queries: list[str],
    functions_result: str = "",
    use_glkb: bool = False,
    pre_final_answer: str = "",
) -> str:
    """End-to-end rigor formatting pipeline (no compression, hallucination check)."""
    pipeline_start = time.perf_counter()
    emit("rigor_format_start", {
        "human_query": human_query[:200],
        "num_neo4j_results": len(neo4j_results),
        "use_literature": use_glkb,
    })

    # Safety check: if ALL Neo4j results are empty/errors, return early
    if not _has_useful_data(neo4j_results):
        emit("rigor_format_no_data", {"reason": "all Neo4j results empty or errored"})
        return _NO_DATA_RESPONSE

    # Extract HIRN literature section
    hirn_text = ""
    if use_glkb and functions_result:
        hirn_lines = []
        in_hirn_block = False
        for line in functions_result.split('\n'):
            if re.match(r'^\d+\.\s+HIRN_literature', line):
                in_hirn_block = True
            elif re.match(r'^\d+\.\s+PankBaseAgent', line):
                in_hirn_block = False
            if in_hirn_block:
                hirn_lines.append(line)
        hirn_text = '\n'.join(hirn_lines) if hirn_lines else functions_result

    format_result = rigor_format_response(
        human_query=human_query,
        neo4j_results=neo4j_results,
        cypher_queries=cypher_queries,
        literature_text=hirn_text,
        use_literature=use_glkb,
        pre_final_answer=pre_final_answer,
    )

    emit("rigor_format_raw_output", {"output": format_result[:3000]})

    # Hallucination check
    t4 = time.perf_counter()
    try:
        format_json = json.loads(format_result)
        summary_text = format_json.get('text', {}).get('summary', '')

        report = check_hallucination(
            summary=summary_text,
            neo4j_results=neo4j_results,
            raw_agent_output=functions_result
        )
        emit("rigor_format_halluc_check", {
            "is_clean": report["is_clean"],
            "hallucinated_go_terms": report["hallucinated_go_terms"],
            "hallucinated_pubmed_ids": report["hallucinated_pubmed_ids"],
        })

        if not report['is_clean']:
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
            }
            format_result = json.dumps(format_json)
            emit("rigor_format_auto_clean", {
                "removed_pubmed_ids": len(report["hallucinated_pubmed_ids"]),
                "removed_go_terms": len(report["hallucinated_go_terms"]),
            })

    except Exception as e:
        emit("rigor_format_halluc_check", {"error": str(e)})

    halluc_elapsed = time.perf_counter() - t4

    # Emit follow-up questions if present
    try:
        fj = json.loads(format_result) if not isinstance(format_result, dict) else format_result
        follow_ups = fj.get('text', {}).get('follow_up_questions', [])
        if follow_ups:
            emit("follow_up_questions", {"questions": follow_ups})
    except Exception:
        pass

    pipeline_elapsed = time.perf_counter() - pipeline_start
    emit("rigor_format_done", {
        "elapsed_s": round(pipeline_elapsed, 2),
        "halluc_check_s": round(halluc_elapsed, 2),
    })
    _perf_log("run_rigor_format_pipeline", "success", pipeline_elapsed * 1000)

    return format_result
