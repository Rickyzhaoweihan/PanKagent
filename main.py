from claude import *
from claude import (run_format_pipeline, run_reasoning_pipeline,
                    run_rigor_format_pipeline, run_rigor_reasoning_pipeline,
                    run_query_planner_pipeline)
from utils import *
from utils import get_neo4j_results, reset_cypher_queries, hirn_chat_one_round
from stream_events import emit
from typing import Tuple
from copy import deepcopy
import json
import os
import sys
import re
import time
import traceback
from queue import Queue
from _thread import start_new_thread

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "skills", "query-planner", "scripts"))
from qp_query_planner import (
    interpret_question, plan_query, translate_plan, execute_plan,
    revise_plan_query,
)


MAX_ITER = 2
PRINT_FUNC_CALL = True
PRINT_FUNC_RESULT = True
set_log_enable(True)

RIGOR_MODE = False

PLANNER_CANDIDATES = 5


def _select_pipeline(is_complex: bool, **kwargs) -> str:
    if is_complex:
        if RIGOR_MODE:
            emit("main_routing", {"agent": "rigor_reasoning", "complexity": "complex"})
            if 'use_glkb' in kwargs:
                kwargs['use_literature'] = kwargs.pop('use_glkb')
            return run_rigor_reasoning_pipeline(**kwargs)
        else:
            emit("main_routing", {"agent": "reasoning", "complexity": "complex"})
            if 'use_glkb' in kwargs:
                kwargs['use_literature'] = kwargs.pop('use_glkb')
            return run_reasoning_pipeline(**kwargs)
    else:
        if RIGOR_MODE:
            emit("main_routing", {"agent": "rigor_format", "complexity": "simple"})
            return run_rigor_format_pipeline(**kwargs)
        else:
            emit("main_routing", {"agent": "format", "complexity": "simple"})
            return run_format_pipeline(**kwargs)


# ---------------------------------------------------------------------------
# Scoring helper — count non-empty Neo4j results
# ---------------------------------------------------------------------------

def _count_nonempty(neo4j_results: list[dict]) -> int:
    count = 0
    for entry in neo4j_results:
        result = entry.get("result", {})
        if isinstance(result, dict) and "error" in result:
            continue
        results_value = result.get("results", "") if isinstance(result, dict) else ""
        if isinstance(results_value, str):
            norm = results_value.strip().lower()
            if not norm or norm == "no results":
                continue
            if "nodes, edges" in norm and (
                "[], []" in norm or "[][]" in norm.replace(" ", "")
            ):
                continue
        count += 1
    return count


# ---------------------------------------------------------------------------
# Extract Neo4j results from run_functions text output
# ---------------------------------------------------------------------------

def _extract_neo4j_from_funcs_result(funcs_result: str) -> tuple[list[dict], list[str]]:
    """Parse the text blob returned by run_functions to extract Neo4j results.

    run_functions concatenates text like:
        1. PankBaseAgent chat_one_round: ...
        Status: success
        Result: {"source":"pankbase", ..., "raw_results": [...]}

    We find the JSON in each PankBaseAgent result block and pull out
    raw_results (list of {"query": ..., "result": ...}).

    Returns (neo4j_results, cypher_queries_with_data).
    """
    neo4j_results: list[dict] = []
    cypher_queries: list[str] = []

    for m in re.finditer(r'Result:\s*(\{.*?"source"\s*:\s*"pankbase".*)', funcs_result):
        text = m.group(1)
        # Find balanced JSON object
        depth = 0
        end = 0
        for i, ch in enumerate(text):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == 0:
            continue
        try:
            resp = json.loads(text[:end])
        except (json.JSONDecodeError, ValueError):
            continue
        for item in resp.get("raw_results", []):
            cypher = item.get("query", "")
            neo4j_result = item.get("result", {})
            if not cypher:
                continue
            neo4j_results.append({"query": cypher, "result": neo4j_result})
            has_data = True
            if isinstance(neo4j_result, dict):
                # Genomic/HPAP/ssGSEA results: check rows list
                if neo4j_result.get("source") in ("genomic", "hpap", "ssgsea", "functional_data"):
                    has_data = bool(neo4j_result.get("rows"))
                else:
                    rv = neo4j_result.get("results", "")
                    if isinstance(rv, str):
                        norm = rv.strip().lower()
                        if not norm or norm == "no results":
                            has_data = False
                        elif "nodes, edges" in norm and (
                            "[], []" in norm or "[][]" in norm.replace(" ", "")
                        ):
                            has_data = False
            if has_data:
                cypher_queries.append(cypher)

    return neo4j_results, cypher_queries


# ---------------------------------------------------------------------------
# Single-candidate worker for planner-level test-time scaling
# ---------------------------------------------------------------------------

def _run_planner_candidate(
    candidate_id: int,
    messages_snapshot: list[dict],
    question_with_prefix: str,
    q: Queue,
) -> None:
    """Run one PlannerAgent -> run_functions -> collect Neo4j results."""
    try:
        msgs = deepcopy(messages_snapshot)

        msgs_out, response = chat_and_get_formatted(msgs)

        complexity = response.get("complexity", "simple")
        if complexity not in ("simple", "complex"):
            complexity = "simple"

        if response["to"] == "user":
            q.put((candidate_id, complexity, msgs_out, response,
                   "", False, [], [], 0))
            return

        funcs_result = run_functions(response["functions"])

        hirn_used = any(
            f.get("name") == "hirn_chat_one_round"
            for f in response.get("functions", [])
        )

        neo4j_results, cypher_queries = _extract_neo4j_from_funcs_result(funcs_result)
        score = _count_nonempty(neo4j_results)

        q.put((candidate_id, complexity, msgs_out, response,
               funcs_result, hirn_used, neo4j_results, cypher_queries, score))
    except Exception:
        q.put((candidate_id, "simple", [], {},
               "", False, [], [], -1))


# ---------------------------------------------------------------------------
# chat_one_round — with planner-level test-time scaling
# ---------------------------------------------------------------------------

def chat_one_round(messages_history: list[dict], question: str) -> Tuple[list[dict], str]:
    reset_cypher_queries()

    question = question.strip()
    if question == "":
        question = "<empty>"
    question_with_prefix = "====== From User ======\n" + question
    messages = deepcopy(messages_history)
    messages.append({"role": "user", "content": question_with_prefix})

    emit("planner_test_time_start", {
        "num_candidates": PLANNER_CANDIDATES,
        "question": question[:300],
    })

    t0 = time.time()
    q: Queue = Queue()

    for i in range(PLANNER_CANDIDATES):
        reset_cypher_queries()
        start_new_thread(
            _run_planner_candidate,
            (i, messages, question_with_prefix, q),
        )

    collected: dict = {}
    deadline = time.time() + 240
    while len(collected) < PLANNER_CANDIDATES and time.time() < deadline:
        time.sleep(0.3)
        while not q.empty():
            item = q.get_nowait()
            cid = item[0]
            collected[cid] = item[1:]

    if not collected:
        emit("planner_test_time_result", {
            "num_candidates": 0, "selected": -1, "elapsed_s": round(time.time() - t0, 1),
        })
        reset_cypher_queries()
        return (messages, json.dumps({
            "to": "user",
            "text": {"summary": "All planner candidates timed out. Please try again."}
        }))

    # Pick best candidate: highest non-empty score, tiebreak by num queries
    best_id = max(
        collected,
        key=lambda cid: (collected[cid][7], len(collected[cid][6])),
    )
    (best_complexity, best_msgs, best_response, best_funcs_result,
     best_hirn_used, best_neo4j, best_cypher, best_score) = collected[best_id]

    elapsed = time.time() - t0
    emit("planner_test_time_result", {
        "num_candidates": len(collected),
        "candidates": [
            {
                "id": cid,
                "score": vals[7],
                "num_queries": len(vals[6]),
                "complexity": vals[0],
                "to": vals[2].get("to", "?") if isinstance(vals[2], dict) else "?",
                "pankbase_input": next(
                    (f.get("input", "")[:200]
                     for f in vals[2].get("functions", [])
                     if f.get("name") == "pankbase_chat_one_round"),
                    None,
                ) if isinstance(vals[2], dict) else None,
            }
            for cid, vals in sorted(collected.items())
        ],
        "selected": best_id,
        "selected_score": best_score,
        "elapsed_s": round(elapsed, 1),
    })

    original_question = question

    # If the best candidate went directly to user (no function calls)
    if best_response.get("to") == "user":
        is_complex = (best_complexity == "complex")
        result = _select_pipeline(
            is_complex=is_complex,
            human_query=original_question,
            neo4j_results=best_neo4j,
            cypher_queries=best_cypher,
            functions_result=str(best_response.get("text", "")),
            use_glkb=False,
            pre_final_answer=json.dumps(best_response.get("text", "")),
        )
        reset_cypher_queries()
        return (best_msgs, result)

    # Best candidate made function calls — route to format/reasoning
    is_complex = (best_complexity == "complex")
    result = _select_pipeline(
        is_complex=is_complex,
        human_query=original_question,
        neo4j_results=best_neo4j,
        cypher_queries=best_cypher,
        functions_result=best_funcs_result,
        use_glkb=best_hirn_used,
    )

    reset_cypher_queries()
    return (best_msgs, result)


def extract_markdown(response: str) -> str:
    """Convert the raw JSON response into clean Markdown.

    Parses the pipeline JSON (``{"to":"user","text":{"summary":"...","cypher":[...],...}}``)
    and returns the ``summary`` field as-is (it is already Markdown).  If the
    response also contains ``cypher`` queries, they are appended under a
    ``## Cypher Queries`` section.  If the response also contains a
    ``reasoning_trace``, it is prepended under ``## Reasoning Trace``.

    Falls back to the raw string when parsing fails.
    """
    try:
        data = json.loads(response) if isinstance(response, str) else response
    except (json.JSONDecodeError, TypeError):
        return response

    text = data.get("text", data)
    if isinstance(text, str):
        try:
            text = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    if not isinstance(text, dict):
        return str(text)

    parts: list[str] = []

    reasoning = text.get("reasoning_trace", "")
    if reasoning:
        parts.append("## Reasoning Trace\n")
        parts.append(reasoning.strip())
        parts.append("")

    summary = text.get("summary", "")
    if summary:
        parts.append(summary.strip())
        parts.append("")

    cypher = text.get("cypher", [])
    if cypher:
        parts.append("## Cypher Queries\n")
        for i, q in enumerate(cypher, 1):
            parts.append(f"**Query {i}:**")
            parts.append(f"```cypher\n{q}\n```\n")

    return "\n".join(parts).strip() if parts else response


def clean_response_json(response: str) -> str:
    """Return the pipeline JSON with ``summary`` replaced by clean Markdown.

    The original JSON structure is preserved (``to``, ``text.template_matching``,
    ``text.cypher``, ``text.reasoning_trace``, etc.).  Only ``text.summary`` is
    swapped for the output of ``extract_markdown``.
    """
    try:
        data = json.loads(response) if isinstance(response, str) else response
    except (json.JSONDecodeError, TypeError):
        return response

    text = data.get("text", None)
    if isinstance(text, str):
        try:
            text = json.loads(text)
            data["text"] = text
        except (json.JSONDecodeError, TypeError):
            return response

    if isinstance(text, dict) and "summary" in text:
        text["summary"] = extract_markdown(response)

    return json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Interactive plan confirmation helpers
# ---------------------------------------------------------------------------

def _count_result_records(res: dict) -> int | None:
    """Return the number of data items in a Neo4j result, or None if error/empty.

    Handles two API response formats:

    1. **Structured format** (new API):
       ``{"keys": ["nodes", "edges"], "records": [[<nodes_list>, <edges_list>], ...]}``
       Each record is a list of values aligned with ``keys``.  We look for a
       ``nodes`` column and count the items in it.

    2. **Dict-per-record format**:
       ``{"records": [{"nodes": [...], "edges": [...]}, ...]}``
       Each record is a dict.

    3. **Legacy string format**:
       ``{"results": "(:gene {name: 'TP53', ...}), ..."}``
    """
    if isinstance(res, dict) and "error" in res:
        return None

    # HPAP/genomic result format: {"rows": [...], "row_count": N, "source": "hpap"|"genomic"}
    if isinstance(res, dict) and res.get("source") in ("hpap", "genomic", "ssgsea", "functional_data"):
        return res.get("row_count", len(res.get("rows", [])))

    if isinstance(res, dict) and "records" in res:
        records = res["records"]
        if not records:
            return 0

        keys = res.get("keys", [])

        # Find the index of the "nodes" column if keys are present
        nodes_col_idx = None
        if keys:
            for idx, k in enumerate(keys):
                if k == "nodes":
                    nodes_col_idx = idx
                    break

        total = 0
        for rec in records:
            if isinstance(rec, dict):
                # Dict-per-record format
                total += len(rec.get("nodes", []))
            elif isinstance(rec, (list, tuple)):
                # List-per-record format (aligned with keys)
                if nodes_col_idx is not None and nodes_col_idx < len(rec):
                    nodes_val = rec[nodes_col_idx]
                    if isinstance(nodes_val, list):
                        total += len(nodes_val)
                    elif nodes_val:
                        total += 1
                else:
                    # No "nodes" key — count non-empty values in the record
                    for val in rec:
                        if isinstance(val, list):
                            total += len(val)
                        elif val is not None:
                            total += 1
        return total

    if isinstance(res, dict) and "results" in res:
        rv = res["results"]
        if isinstance(rv, str):
            norm = rv.strip().lower()
            if not norm or norm == "no results":
                return 0
            if "nodes, edges" in norm and (
                "[], []" in norm or "[][]" in norm.replace(" ", "")
            ):
                return 0
        return 1
    return None


def _get_step_result(
    step_index: int,
    plan: dict,
    neo4j_results: list[dict],
) -> dict | None:
    """Return the neo4j result dict for a given step index.

    For **parallel** plans each step maps 1:1 to neo4j_results[i].
    For **chain** plans all steps share the single combined result at index 0.
    """
    if not neo4j_results:
        return None
    plan_type = plan.get("plan_type", "parallel")
    if plan_type == "chain":
        return neo4j_results[0] if neo4j_results else None
    else:
        return neo4j_results[step_index] if step_index < len(neo4j_results) else None


_EDGE_DISPLAY_NAMES = {
    "function_annotation;GO": "GO terms",
    "pathway_annotation;KEGG": "KEGG pathways",
    "pathway_annotation;reactome": "Reactome pathways",
    "physical_interaction": "interactions",
    "genetic_interaction": "genetic interactions",
    "gene_detected_in": "expression",
    "gene_enriched_in": "cell-type markers",
    "T1D_DEG_in": "differential expression",
    "part_of_QTL_signal": "QTL signals",
    "part_of_GWAS_signal": "GWAS signals",
    "signal_COLOC_with": "colocalization",
    "effector_gene_of": "effector genes",
    "OCR_peak_in": "OCR peaks",
    "gene_activity_score_in": "chromatin accessibility",
    "fGSEA_gene_enriched_in": "fGSEA gene enrichment",
    "fGSEA_enriched_in": "fGSEA pathway enrichment",
    "has_donor": "donor link",
    "has_sample": "sample link",
}


def _build_scope_line(
    steps: list[dict],
    use_literature: bool,
    literature_result: str,
) -> str:
    """Build an entity-centric scope string from plan steps.

    Example: "Gene CFTR → GO terms, interactions, expression, QTL, literature"
    """
    if not steps:
        return ""

    # Extract the target entity from the first "Find ..." step
    entity = ""
    first_nl = steps[0].get("natural_language", "") if steps else ""
    m = re.match(r"[Ff]ind\s+(\w+)\s+with\s+name\s+(.+)", first_nl)
    if m:
        entity = f"{m.group(1).capitalize()} {m.group(2).strip()}"

    # Extract relationship dimensions from subsequent steps
    dimensions: list[str] = []
    seen: set[str] = set()
    has_hpap = False
    has_genomic = False
    has_ssgsea = False
    has_functional_data = False
    for step in steps:
        if step.get("source") == "hpap":
            has_hpap = True
            continue
        if step.get("source") == "genomic":
            has_genomic = True
            continue
        if step.get("source") == "ssgsea":
            has_ssgsea = True
            continue
        if step.get("source") == "functional_data":
            has_functional_data = True
            continue
        nl = step.get("natural_language", "").lower()
        for edge_type, display in _EDGE_DISPLAY_NAMES.items():
            if edge_type.lower() in nl and display not in seen:
                dimensions.append(display)
                seen.add(display)
                break

    if has_hpap:
        dimensions.append("HPAP metadata")
    if has_genomic:
        dimensions.append("genomic coordinates")
    if has_ssgsea:
        dimensions.append("ssGSEA enrichment")
    if has_functional_data:
        dimensions.append("islet functional assays")

    if use_literature:
        has_lit = literature_result and "Status: success" in literature_result
        dimensions.append("literature" if has_lit else "literature (pending)")

    if not entity and not dimensions:
        return ""

    dim_str = ", ".join(dimensions) if dimensions else "basic info"
    return f"{entity} → {dim_str}" if entity else dim_str


def format_plan_as_markdown(
    question: str,
    plan: dict,
    neo4j_results: list[dict],
    use_literature: bool = False,
    literature_result: str = "",
) -> str:
    """Render the query plan as clean, concise Markdown for confirmation."""
    plan_type = plan.get("plan_type", "unknown")
    reasoning = plan.get("reasoning", "")
    steps = plan.get("steps", [])
    display_question = plan.get("interpreted_question") or question

    # Detect cross-source chain for display hint
    has_non_kg = any(
        s.get("source") in ("hpap", "genomic", "ssgsea", "functional_data") for s in steps
    )
    flow_hint = ""
    if plan_type == "chain" and has_non_kg:
        arrow_parts = []
        for s in sorted(steps, key=lambda x: x.get("id", 0)):
            src = s.get("source") or "kg"
            arrow_parts.append(f"[{src}:{s.get('id')}]")
        flow_hint = f"\n**Sequential flow:** {' → '.join(arrow_parts)}\n"

    parts = [
        f"## Interpreted Question\n\n{display_question}\n",
        f"## Query Plan ({plan_type})\n\n{reasoning}\n{flow_hint}",
        "### Steps\n",
    ]

    steps_with_data = 0

    for i, step in enumerate(steps):
        sid = step.get("id", i + 1)
        nl = step.get("natural_language", "")
        dep = step.get("depends_on")
        dep_str = f" *(depends on step {dep})*" if dep else ""
        source = step.get("source")
        source_tag = {"hpap": "[HPAP] ", "genomic": "[Genomic] ", "ssgsea": "[ssGSEA] ",
                      "functional_data": "[Functional] "}.get(source, "")

        status = "pending"
        result_entry = _get_step_result(i, plan, neo4j_results)
        if result_entry is not None:
            res = result_entry.get("result", {})
            rc = _count_result_records(res)
            if rc is None:
                err = res.get("error", "unknown error") if isinstance(res, dict) else "?"
                status = f"error — {str(err)[:60]}"
            elif rc == 0:
                status = "0 rows" if step.get("source") in ("hpap", "genomic", "ssgsea", "functional_data") else "0 records"
            else:
                status = f"{rc} rows" if step.get("source") in ("hpap", "genomic", "ssgsea", "functional_data") else f"{rc} records"
                steps_with_data += 1

        parts.append(f"{sid}. {source_tag}{nl}{dep_str} — **{status}**")

    # Literature step — always shown so user can see the toggle state
    lit_step_num = len(steps) + 1
    if use_literature:
        parts.append(f"{lit_step_num}. **Literature retrieval** — GLKB + HIRN running in background (parallel)")
    else:
        parts.append(f"{lit_step_num}. ~~Literature retrieval~~ — **disabled**")

    scope = _build_scope_line(steps, use_literature, literature_result)
    if scope:
        parts.append(f"\n**Scope:** {scope}\n")

    parts.append("---")
    parts.append("Type a revision, **confirm** to generate the answer, or **new** for a different question.")

    return "\n".join(parts)


def build_execution_summary(plan: dict, neo4j_results: list[dict]) -> list[dict]:
    """Build per-step execution summary for the revision prompt."""
    summary = []
    for i, step in enumerate(plan.get("steps", [])):
        sid = step.get("id")
        cypher = step.get("cypher", "")
        records = 0
        result_entry = _get_step_result(i, plan, neo4j_results)
        if result_entry is not None:
            res = result_entry.get("result", {})
            rc = _count_result_records(res)
            records = rc if rc is not None else 0
            cypher = result_entry.get("query", cypher)
        summary.append({"id": sid, "cypher": cypher, "records": records})
    return summary


PLAN_START_CANDIDATES = 5
HIRN_SEARCH_TIMEOUT = 10  # seconds to wait for HIRN literature search


def _run_hirn_search(question: str, q: Queue) -> None:
    """Worker: run the HIRN literature pipeline directly (no double-thread).

    Calls the hirn_publication_retrieval scripts in-process instead of going
    through the hirn_chat_one_round wrapper which adds a redundant inner
    thread + 30-second polling loop.

    The raw *question* is first expanded into multiple focused keyword queries
    via ``query_expander.expand_query`` so that publication title matching and
    BM25 chunk search receive precise biomedical terms instead of the full
    natural-language sentence.
    """
    try:
        import json as _json
        import os as _os

        hirn_skill_dir = _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)),
            'hirn_publication_retrieval', 'skills', 'hirn-literature-retrieve',
        )
        if hirn_skill_dir not in sys.path:
            sys.path.insert(0, hirn_skill_dir)

        from scripts.scrape_hirn import fetch_hirn_publications, search_publications
        from scripts.resolve_ids import resolve_pmcids
        from scripts.fetch_fulltext import fetch_fulltext
        from scripts.chunk_text import chunk_passages
        from scripts.search_chunks import search_chunks
        from scripts.query_expander import expand_query

        raw_query = question.strip()
        emit("hirn_search_start", {"query": raw_query[:200]})

        queries = expand_query(raw_query)
        emit("hirn_queries_expanded", {
            "original": raw_query[:200],
            "expanded": queries,
        })

        cache_dir = _os.path.join(hirn_skill_dir, 'data', 'cache')
        _os.makedirs(cache_dir, exist_ok=True)

        pubs = fetch_hirn_publications(cache_dir=cache_dir)
        emit("hirn_publications_loaded", {"count": len(pubs)})

        matches = search_publications(pubs, query=queries, max_results=10)
        emit("hirn_matches_found", {"count": len(matches)})

        if not matches:
            result = _json.dumps({
                'source': 'hirn', 'query_used': queries,
                'publications_searched': len(pubs), 'matches_found': 0,
                'raw_passages': [],
                'note': 'No HIRN publications matched the query',
            })
            q.put(f"0. HIRN_literature chat_one_round: {raw_query!r}\nStatus: success\nResult: {result}\n\n")
            emit("hirn_result", {"status": "success", "query": raw_query[:200], "result_length": len(result)})
            return

        pmids = [p['pmid'] for p in matches if p.get('pmid')]
        pmcid_map = resolve_pmcids(pmids, cache_dir=cache_dir) if pmids else {}
        emit("hirn_pmcids_resolved", {
            "resolved": len([v for v in pmcid_map.values() if v]),
            "total_pmids": len(pmids),
        })

        all_results = []
        for pub in matches:
            pmid = pub.get('pmid', '')
            pmcid = pmcid_map.get(pmid)
            pub_meta = {
                'pmid': pmid,
                'article_title': pub.get('title', ''),
                'doi': pub.get('doi', ''),
                'authors': pub.get('authors', ''),
                'consortia': pub.get('consortia', []),
            }

            if not pmcid:
                all_results.append({
                    **pub_meta, 'pmcid': None,
                    'text': f"Title: {pub.get('title', '')}",
                    'section': 'TITLE_ONLY', 'score': 0.0,
                    'note': 'Full text not available in PMC Open Access',
                })
                continue

            ft = fetch_fulltext(pmcid, cache_dir=cache_dir)
            if not ft.get('success'):
                all_results.append({
                    **pub_meta, 'pmcid': pmcid,
                    'text': f"Title: {pub.get('title', '')}",
                    'section': 'TITLE_ONLY', 'score': 0.0,
                    'note': f"Full text fetch failed: {ft.get('error', {}).get('message', 'unknown')}",
                })
                continue

            chunks = chunk_passages(ft['passages'])
            hits = search_chunks(chunks, query=queries, top_k=3)
            for h in hits:
                h.update(pub_meta)
                h['pmcid'] = pmcid
            all_results.extend(hits)

        all_results.sort(key=lambda x: x.get('score', 0), reverse=True)
        top_results = all_results[:15]
        emit("hirn_chunks_ready", {"count": len(top_results)})

        result = _json.dumps({
            'source': 'hirn', 'query_used': queries,
            'publications_searched': len(pubs),
            'matches_found': len(matches),
            'passages_returned': len(top_results),
            'raw_passages': top_results,
        })
        formatted = f"0. HIRN_literature chat_one_round: {raw_query!r}\nStatus: success\nResult: {result}\n\n"
        q.put(formatted)
        emit("hirn_result", {"status": "success", "query": raw_query[:200], "result_length": len(result)})

    except Exception:
        err = traceback.format_exc()
        emit("hirn_result", {"status": "error", "query": question[:200], "error": err[:500]})
        q.put(f"0. HIRN_literature chat_one_round: {question!r}\nStatus: error\nError: {err[:2000]}\n\n")


# ---------------------------------------------------------------------------
# HPAP metadata step execution
# ---------------------------------------------------------------------------

HPAP_QUERY_TIMEOUT = 15  # seconds

def _run_hpap_step(question_text: str, prior_entities: dict | None = None) -> dict:
    # HPAP skill is disabled; prior_entities is accepted for API uniformity but ignored.
    _ = prior_entities
    """Execute a single HPAP metadata step via NL-to-SQL.

    Returns a result dict shaped like a Neo4j step result so it integrates
    seamlessly with the rest of the plan pipeline::

        {"query": "<SQL>", "result": {"rows": [...], "row_count": N, "source": "hpap"}}
    """
    import os as _os

    hpap_skill_dir = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)),
        'skills', 'hpap-database-metadata',
    )
    if hpap_skill_dir not in sys.path:
        sys.path.insert(0, hpap_skill_dir)

    from nl_query import nl_to_sql, run_query

    emit("hpap_query_start", {"query": question_text[:300]})

    try:
        parsed = nl_to_sql(question_text)
        database = parsed.get("database", "donors")
        sql = parsed.get("sql", "")
        explanation = parsed.get("explanation", "")

        emit("hpap_sql_generated", {
            "database": database,
            "sql": sql[:500],
            "explanation": explanation[:200],
        })

        rows = run_query(database, sql)

        emit("hpap_query_done", {
            "success": True,
            "database": database,
            "sql": sql[:500],
            "row_count": len(rows),
        })

        return {
            "query": sql,
            "result": {
                "rows": rows,
                "row_count": len(rows),
                "source": "hpap",
                "database": database,
                "explanation": explanation,
            },
        }

    except Exception:
        err = traceback.format_exc()
        emit("hpap_query_done", {
            "success": False,
            "error": err[:500],
        })
        return {
            "query": question_text,
            "result": {
                "error": err[:2000],
                "source": "hpap",
            },
        }


def _run_genomic_step(question_text: str, prior_entities: dict | None = None) -> dict:
    """Execute a single genomic coordinate step via NL-to-SQL (PostgreSQL).

    If ``prior_entities`` contains ``gene_ids`` (Ensembl IDs) from a parent
    KG step, they are appended to the natural-language query so the text2sql
    model uses exact IDs and skips the gene name resolver.

    Returns a result dict shaped like HPAP/Neo4j step results::

        {"query": "<SQL>", "result": {"rows": [...], "row_count": N, "source": "genomic"}}
    """
    import os as _os

    genomic_skill_dir = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)),
        'PankBaseAgent', 'text_to_sql',
    )
    if genomic_skill_dir not in sys.path:
        sys.path.insert(0, genomic_skill_dir)

    from src.text2sql_agent import Text2SQLAgent
    from src.sql_validator import validate_sql

    import psycopg2
    import psycopg2.extras

    # If prior KG step gave us Ensembl IDs, inline them into the NL prompt
    augmented_nl = question_text
    prior_gene_ids: list[str] = []
    if prior_entities and isinstance(prior_entities, dict):
        prior_gene_ids = [g for g in prior_entities.get("gene_ids", []) if g][:200]
        if prior_gene_ids:
            id_list = ", ".join(f"'{g}'" for g in prior_gene_ids)
            augmented_nl = f"{question_text}\n\n(Upstream step resolved these Ensembl gene IDs — use them directly with WHERE id IN ({id_list})): {id_list}"
            emit("genomic_prior_entities", {
                "gene_id_count": len(prior_gene_ids),
                "source_step_id": prior_entities.get("source_step_id"),
            })

    emit("genomic_query_start", {"query": augmented_nl[:300]})

    try:
        agent = Text2SQLAgent(provider="local")
        result = agent.respond_with_refinement(augmented_nl)
        sql = result["sql"]

        emit("genomic_sql_generated", {
            "sql": sql[:500],
            "score": result["score"],
        })

        conn = psycopg2.connect(
            host="localhost", port=5432,
            user="serviceuser", password="password",
            dbname="pankgraph",
        )
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql)
            rows = [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

        emit("genomic_query_done", {
            "success": True,
            "sql": sql[:500],
            "row_count": len(rows),
        })

        return {
            "query": sql,
            "result": {
                "rows": rows,
                "row_count": len(rows),
                "source": "genomic",
            },
        }

    except Exception:
        err = traceback.format_exc()
        emit("genomic_query_done", {
            "success": False,
            "error": err[:500],
        })
        return {
            "query": question_text,
            "result": {
                "error": err[:2000],
                "source": "genomic",
            },
        }


def _run_ssgsea_step(question_text: str, prior_entities: dict | None = None) -> dict:
    """Execute a single ssGSEA step: extract gene names and run enrichment analysis.

    If ``prior_entities`` contains ``gene_names`` from a parent step, those are
    used as the gene set (capped at 200). NL-extracted genes are merged in as
    fallback. This enables chains like "find T1D effector genes -> run ssGSEA on them".

    Returns a result dict::

        {"query": "<genes>", "result": {"rows": [scores], "row_count": N, "source": "ssgsea"}}
    """
    import os as _os

    ssgsea_skill_dir = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)),
        'skills', 'ssgsea',
    )
    if ssgsea_skill_dir not in sys.path:
        sys.path.insert(0, ssgsea_skill_dir)

    from ssgsea_client import extract_gene_names, run_ssgsea, get_donors

    emit("ssgsea_start", {"query": question_text[:300]})

    _GENE_CAP = 200

    try:
        # Start with any genes from a parent step (chain / dependency)
        genes: list[str] = []
        seen: set[str] = set()

        if prior_entities and isinstance(prior_entities, dict):
            prior_genes = prior_entities.get("gene_names", []) or []
            # cap to avoid huge payloads — ssGSEA server can handle reasonable sets
            truncated = len(prior_genes) > _GENE_CAP
            for g in prior_genes[:_GENE_CAP]:
                if g not in seen:
                    seen.add(g)
                    genes.append(g)
            if prior_genes:
                emit("ssgsea_prior_entities", {
                    "gene_count": len(prior_genes),
                    "kept": len(genes),
                    "truncated": truncated,
                    "source_step_id": prior_entities.get("source_step_id"),
                })

        # Also merge any genes named directly in the NL (user may have added extras)
        nl_genes = extract_gene_names(question_text) or []
        for g in nl_genes:
            if g not in seen:
                seen.add(g)
                genes.append(g)

        if not genes:
            emit("ssgsea_done", {"success": False, "error": "No gene names found in question or prior steps"})
            return {
                "query": question_text,
                "result": {"error": "No gene names could be extracted from the question or prior steps", "source": "ssgsea"},
            }

        emit("ssgsea_genes_extracted", {"genes": genes[:20], "count": len(genes)})

        result = run_ssgsea(genes)
        scores = result.get("scores", [])

        # Enrich scores with donor metadata for the FormatAgent
        try:
            donors = get_donors()
            donor_map = {d["center_donor_id"]: d for d in donors}
            enriched_scores = []
            for s in scores:
                d = donor_map.get(s["donor_id"], {})
                enriched_scores.append({
                    "donor_id": s["donor_id"],
                    "score": s["score"],
                    "diabetes_status": d.get("description_of_diabetes_status", "unknown"),
                    "age": d.get("age_(years)"),
                    "sex": d.get("sex"),
                    "hba1c": d.get("hba1c_(percentage)"),
                })
        except Exception:
            enriched_scores = scores

        # Compute summary stats by diabetes status
        from collections import defaultdict
        by_status = defaultdict(list)
        for s in enriched_scores:
            status = s.get("diabetes_status", "unknown")
            by_status[status].append(s["score"])
        summary_stats = {
            status: {"mean": round(sum(v) / len(v), 4), "n": len(v)}
            for status, v in sorted(by_status.items())
        }

        # Sort by score descending, keep top 10 + summary
        enriched_scores.sort(key=lambda x: x["score"], reverse=True)

        emit("ssgsea_done", {
            "success": True,
            "genes_used": result.get("genes_used", 0),
            "genes_not_found": result.get("genes_not_found", []),
            "num_donors": len(scores),
        })

        return {
            "query": f"ssGSEA({', '.join(genes)})",
            "result": {
                "rows": enriched_scores[:15],
                "row_count": len(scores),
                "source": "ssgsea",
                "genes_submitted": result.get("genes_submitted", len(genes)),
                "genes_used": result.get("genes_used", 0),
                "genes_not_found": result.get("genes_not_found", []),
                "summary_by_diabetes_status": summary_stats,
                "total_donors": len(scores),
            },
        }

    except Exception:
        err = traceback.format_exc()
        emit("ssgsea_done", {"success": False, "error": err[:500]})
        return {
            "query": question_text,
            "result": {"error": err[:2000], "source": "ssgsea"},
        }


def _run_functional_data_step(question_text: str, prior_entities: dict | None = None) -> dict:
    """Execute a single functional-data API step.

    If ``prior_entities`` contains ``donor_ids`` from a parent step, those are
    passed as the donor_ids filter/override selector (cap 200).

    Returns::
        {"query": "GET /api/...", "result": {"rows": [...], "row_count": N, "source": "functional_data", ...}}
    """
    import os as _os
    from urllib.parse import urlencode as _urlencode

    functional_skill_dir = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)),
        'skills', 'functional_data',
    )
    if functional_skill_dir not in sys.path:
        sys.path.insert(0, functional_skill_dir)

    from functional_data_client import (
        FUNCTIONAL_BASE_URL,
        extract_endpoint_and_params,
        _validate_selection,
        call_endpoint,
        _summarize_summary,
        _summarize_donors,
        _summarize_cohort_traces,
        _summarize_trait_summary,
    )

    emit("functional_data_start", {"query": question_text[:300]})

    _DONOR_CAP = 200

    try:
        prior_donor_ids: list[str] = []
        if prior_entities and isinstance(prior_entities, dict):
            raw = prior_entities.get("donor_ids", []) or []
            prior_donor_ids = [str(d) for d in raw[:_DONOR_CAP]]
            if raw:
                emit("functional_data_prior_entities", {
                    "donor_count": len(raw),
                    "kept": len(prior_donor_ids),
                    "truncated": len(raw) > _DONOR_CAP,
                    "source_step_id": prior_entities.get("source_step_id"),
                })

        selection = extract_endpoint_and_params(question_text, prior_donor_ids or None)

        valid, err_msg = _validate_selection(selection)
        if not valid:
            emit("functional_data_done", {"success": False, "error": err_msg})
            return {
                "query": question_text,
                "result": {"error": err_msg, "source": "functional_data"},
            }

        endpoint = selection["endpoint"]
        params = selection.get("params", {})

        emit("functional_data_endpoint_selected", {"endpoint": endpoint, "params": params})

        payload = call_endpoint(endpoint, params)

        _SUMMARIZERS = {
            "/api/data/summary": _summarize_summary,
            "/api/data/donors": _summarize_donors,
            "/api/charts/cohort-traces": _summarize_cohort_traces,
            "/api/charts/trait-summary": _summarize_trait_summary,
        }
        summarized = _SUMMARIZERS.get(endpoint, _summarize_summary)(payload)

        qs = _urlencode(params)
        full_url = f"{FUNCTIONAL_BASE_URL}{endpoint}?{qs}" if qs else f"{FUNCTIONAL_BASE_URL}{endpoint}"

        emit("functional_data_done", {
            "success": True,
            "endpoint": endpoint,
            "row_count": summarized.get("row_count", 0),
        })

        result: dict = {
            "rows": summarized.get("rows", []),
            "row_count": summarized.get("row_count", 0),
            "source": "functional_data",
            "endpoint": endpoint,
            "url": full_url,
            "params": params,
        }
        # Merge endpoint-specific extras
        for _k in ("trait", "trace_type", "y_label", "times", "mean", "stimuli",
                   "options", "ranges", "available_donors", "trace_types"):
            if _k in summarized:
                result[_k] = summarized[_k]

        return {
            "query": f"GET {endpoint}?{qs}" if qs else f"GET {endpoint}",
            "result": result,
        }

    except Exception:
        err = traceback.format_exc()
        emit("functional_data_done", {"success": False, "error": err[:500]})
        return {
            "query": question_text,
            "result": {"error": err[:2000], "source": "functional_data"},
        }


def _filter_empty_steps(plan: dict, neo4j_results: list[dict]) -> tuple[dict, list[dict]]:
    """Remove steps that returned 0 records or errored, then renumber sequentially.

    For **chain** plans all steps share the single combined result — if it has
    data, all steps are kept; if not, all are removed.
    For **parallel** plans each step is evaluated independently.
    """
    steps = plan.get("steps", [])
    plan_type = plan.get("plan_type", "parallel")

    # Chain plans: all-or-nothing for pure-KG chains; per-step for cross-source chains
    # (cross-source chains mix KG steps with supplementary steps like functional_data,
    # and supplementary steps are always kept regardless of whether the KG step has data)
    has_supplementary_step = any(
        s.get("source") in ("hpap", "genomic", "ssgsea", "functional_data") for s in steps
    )
    if plan_type == "chain" and not has_supplementary_step:
        if neo4j_results:
            rc = _count_result_records(neo4j_results[0].get("result", {}))
            if rc is not None and rc > 0:
                renumbered_steps = []
                for idx, step in enumerate(steps):
                    s = dict(step)
                    s["id"] = idx + 1
                    renumbered_steps.append(s)
                new_plan = dict(plan)
                new_plan["steps"] = renumbered_steps
                return new_plan, neo4j_results
        new_plan = dict(plan)
        new_plan["steps"] = []
        return new_plan, []
    elif plan_type == "chain" and has_supplementary_step:
        # Cross-source chain: evaluate each step individually.
        # KG steps are kept if they have data; supplementary steps are always kept.
        pass  # fall through to the per-step parallel logic below

    # Parallel plans: per-step filtering
    # HPAP steps are always kept (even with 0 rows) so the user sees them in the plan.
    kept_steps = []
    kept_results = []
    old_to_new: dict[int | str, int] = {}

    for i, step in enumerate(steps):
        if i >= len(neo4j_results):
            break
        is_supplementary = step.get("source") in ("hpap", "genomic", "ssgsea", "functional_data")
        rc = _count_result_records(neo4j_results[i].get("result", {}))
        if is_supplementary or (rc is not None and rc > 0):
            new_id = len(kept_steps) + 1
            old_id = step.get("id", i + 1)
            old_to_new[old_id] = new_id
            renumbered = dict(step)
            renumbered["id"] = new_id
            kept_steps.append(renumbered)
            kept_results.append(neo4j_results[i])

    for step in kept_steps:
        dep = step.get("depends_on")
        if dep is not None and dep in old_to_new:
            step["depends_on"] = old_to_new[dep]

    new_plan = dict(plan)
    new_plan["steps"] = kept_steps
    return new_plan, kept_results


def _emit_plan_event(
    event_name: str,
    question: str,
    plan: dict,
    neo4j_results: list[dict],
    complexity: str,
    extra: dict | None = None,
    use_literature: bool = False,
    literature_preview: str = "",
) -> None:
    """Emit a structured plan event the frontend can consume."""
    steps_out = []
    for i, step in enumerate(plan.get("steps", [])):
        result_entry = _get_step_result(i, plan, neo4j_results)
        rc = _count_result_records(result_entry.get("result", {})) if result_entry else None
        steps_out.append({
            "id": step.get("id"),
            "natural_language": step.get("natural_language", ""),
            "cypher": result_entry.get("query", "") if result_entry else "",
            "records": rc if rc is not None else 0,
            "depends_on": step.get("depends_on"),
            "type": "cypher",
        })

    lit_step_id = len(steps_out) + 1
    lit_has_results = bool(literature_preview and "Status: success" in literature_preview)
    steps_out.append({
        "id": lit_step_id,
        "natural_language": "Search HIRN publications for relevant literature",
        "cypher": "",
        "records": 0,
        "depends_on": None,
        "type": "literature",
        "enabled": use_literature,
        "has_results": lit_has_results,
    })

    display_question = plan.get("interpreted_question") or question
    payload = {
        "question": display_question[:300],
        "plan_type": plan.get("plan_type", "unknown"),
        "reasoning": plan.get("reasoning", ""),
        "complexity": complexity,
        "num_steps": len(steps_out),
        "steps": steps_out,
        "use_literature": use_literature,
    }
    if extra:
        payload.update(extra)
    emit(event_name, payload)


def _score_plan_results(plan: dict, neo4j_results: list[dict]) -> int:
    """Score a plan by how many queries returned non-empty data."""
    plan_type = plan.get("plan_type", "parallel")
    steps = plan.get("steps", [])
    if plan_type == "chain":
        if neo4j_results and _count_result_records(neo4j_results[0].get("result", {})) not in (None, 0):
            return len(steps)
        return 0
    return sum(
        1 for r in neo4j_results
        if _count_result_records(r.get("result", {})) not in (None, 0)
    )


def _run_plan_candidate(question: str, candidate_id: int, q: Queue,
                        prebuilt_plan: dict | None = None,
                        history_context: str = "") -> None:
    """Worker: run plan_query + translate_plan + execute_plan for one candidate.

    If *prebuilt_plan* is provided, skip plan_query (reuse the plan from a
    prior call) and go straight to translate + execute.

    ``history_context`` is a compact string of prior conversation turns that
    helps the planner resolve pronouns/entity references in follow-up questions.
    """
    try:
        plan = (
            prebuilt_plan if prebuilt_plan is not None
            else plan_query(question, chat_history_context=history_context)
        )
        plan = translate_plan(plan)
        neo4j_results = execute_plan(plan, hpap_handler=None, genomic_handler=_run_genomic_step, ssgsea_handler=_run_ssgsea_step, functional_data_handler=_run_functional_data_step)
        score = _score_plan_results(plan, neo4j_results)
        q.put((candidate_id, score, plan, neo4j_results, None))
    except Exception:
        q.put((candidate_id, -1, {}, [], traceback.format_exc()))


def clean_user_question(question: str) -> str:
    """Fix typos and grammar in the user's raw question via a lightweight
    Claude call.  This MUST run before any planning or text2cypher work.

    Returns the cleaned question string, or the original on failure.
    """
    emit("plan_interpreting", {"question": question[:300]})
    try:
        return interpret_question(question)
    except Exception:
        return question


def run_plan_start(question: str, use_literature: bool = True,
                   chat_history: list[dict] | None = None) -> dict:
    """Run best-of-N plan candidates with test-time scaling.

    *question* is expected to be already cleaned (via ``clean_user_question``).

    If ``chat_history`` is provided (list of ``{"role", "content"}`` dicts from
    a multi-turn chat session), a compact context string is built and passed
    to each candidate's ``plan_query`` so the planner can resolve pronouns
    and entity references against prior turns.

    Returns dict with keys: plan, neo4j_results, cypher_queries, complexity,
                            use_literature, literature_result.
    """
    n = PLAN_START_CANDIDATES

    # Build compact history context for the planner (text-only, no raw data)
    history_context = ""
    if chat_history:
        turns = [
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content'][:1200]}"
            for m in chat_history[-6:]
        ]
        history_context = "\n".join(turns)

    emit("plan_test_time_start", {
        "num_candidates": n,
        "question": question[:300],
        "use_literature": use_literature,
        "has_history_context": bool(history_context),
    })

    t0 = time.time()
    q: Queue = Queue()

    for i in range(n):
        start_new_thread(_run_plan_candidate, (question, i, q, None, history_context))

    # Literature is now handled by a background thread in server.py — not spawned here.
    literature_result = ""

    collected: dict[int, tuple] = {}
    deadline = time.time() + 180
    while len(collected) < n and time.time() < deadline:
        time.sleep(0.3)
        while not q.empty():
            item = q.get_nowait()
            collected[item[0]] = item[1:]  # (score, plan, neo4j_results, error)

    if not collected:
        emit("plan_test_time_result", {
            "num_candidates": 0, "selected": -1,
            "elapsed_s": round(time.time() - t0, 1),
        })
        return {
            "plan": {}, "neo4j_results": [], "cypher_queries": [],
            "complexity": "simple",
            "use_literature": use_literature,
            "literature_result": literature_result,
        }

    best_id = max(
        collected,
        key=lambda cid: (collected[cid][0], len(collected[cid][2])),
    )
    best_score, best_plan, best_results, best_error = collected[best_id]

    elapsed = time.time() - t0
    emit("plan_test_time_result", {
        "num_candidates": len(collected),
        "candidates": [
            {
                "id": cid,
                "score": vals[0],
                "num_queries": len(vals[2]),
                "plan_type": vals[1].get("plan_type", "?") if isinstance(vals[1], dict) else "?",
                "error": vals[3][:200] if vals[3] else None,
            }
            for cid, vals in sorted(collected.items())
        ],
        "selected": best_id,
        "selected_score": best_score,
        "elapsed_s": round(elapsed, 1),
    })

    best_plan, best_results = _filter_empty_steps(best_plan, best_results)

    cypher_queries = [
        r["query"] for r in best_results
        if not (isinstance(r.get("result", {}), dict) and "error" in r.get("result", {}))
    ]

    complexity = "complex" if best_plan.get("plan_type") == "chain" else "simple"

    _emit_plan_event("plan_selected", question, best_plan, best_results, complexity,
                     extra={"score": best_score},
                     use_literature=use_literature,
                     literature_preview=literature_result)

    return {
        "plan": best_plan,
        "neo4j_results": best_results,
        "cypher_queries": cypher_queries,
        "complexity": complexity,
        "use_literature": use_literature,
        "literature_result": literature_result,
    }


def _plans_steps_equal(plan_a: dict, plan_b: dict) -> bool:
    """Return True if two plans have identical step definitions (ignoring cypher)."""
    steps_a = plan_a.get("steps", [])
    steps_b = plan_b.get("steps", [])
    if len(steps_a) != len(steps_b):
        return False
    for sa, sb in zip(steps_a, steps_b):
        if (sa.get("natural_language") != sb.get("natural_language")
                or sa.get("join_var") != sb.get("join_var")
                or sa.get("depends_on") != sb.get("depends_on")):
            return False
    return plan_a.get("plan_type") == plan_b.get("plan_type")


def _collect_hirn_result(hirn_q: Queue, literature_result: str) -> str:
    """Wait for the HIRN search thread and return the result."""
    new_literature = literature_result
    try:
        hirn_deadline = time.time() + HIRN_SEARCH_TIMEOUT + 5
        while time.time() < hirn_deadline:
            if not hirn_q.empty():
                new_literature = hirn_q.get_nowait()
                break
            time.sleep(0.3)
    except Exception:
        new_literature = ""
    lit_ok = bool(new_literature and "Status: success" in new_literature)
    emit("plan_hirn_search_done", {"success": lit_ok, "length": len(new_literature), "trigger": "revision"})
    return new_literature


def run_plan_revise(
    original_question: str,
    current_plan: dict,
    neo4j_results: list[dict],
    user_prompt: str,
    use_literature: bool = False,
    literature_result: str = "",
) -> dict:
    """Revise the plan based on the user's prompt, re-translate and re-execute.

    The revision LLM decides whether literature search should be on/off via the
    ``use_literature`` field in its JSON output.  If literature is newly enabled,
    HIRN search runs in parallel with Cypher re-execution.
    If the LLM reports the revision as unsupported (plan_type="error"), the
    original plan is kept and the error reason is returned as a soft message.

    Returns dict with keys: plan, neo4j_results, cypher_queries, complexity, error,
                            use_literature, literature_result.
    """
    exec_summary = build_execution_summary(current_plan, neo4j_results)

    try:
        new_plan = revise_plan_query(
            original_question=original_question,
            current_plan=current_plan,
            execution_summary=exec_summary,
            user_prompt=user_prompt,
            use_literature=use_literature,
        )
    except Exception as e:
        return {
            "plan": current_plan,
            "neo4j_results": neo4j_results,
            "cypher_queries": [],
            "complexity": "simple",
            "error": f"Failed to revise plan: {e}",
            "use_literature": use_literature,
            "literature_result": literature_result,
        }

    # The LLM decides the literature state
    new_use_literature = bool(new_plan.get("use_literature", use_literature))
    literature_toggled = new_use_literature != use_literature

    # Start HIRN search only if literature is enabled and we have never fetched
    # results for this session.  When literature is toggled off then back on, we
    # Literature is now handled by server.py background thread — not re-run here.
    need_hirn = False
    hirn_q: Queue = Queue()  # kept for _collect_hirn_result call sites below

    # If the LLM says the revision is unsupported, keep the original plan
    # but still honour the literature toggle.
    if new_plan.get("plan_type") == "error":
        new_literature = literature_result
        error_reason = new_plan.get("reasoning", "Revision not supported")
        complexity = "complex" if current_plan.get("plan_type") == "chain" else "simple"
        _emit_plan_event("plan_revised", original_question, current_plan, neo4j_results, complexity,
                         use_literature=new_use_literature, literature_preview=new_literature if new_use_literature else "")
        return {
            "plan": current_plan,
            "neo4j_results": neo4j_results,
            "cypher_queries": [r["query"] for r in neo4j_results
                               if not (isinstance(r.get("result", {}), dict) and "error" in r.get("result", {}))],
            "complexity": complexity,
            "error": f"Revision not applied: {error_reason}. Keeping the original plan.",
            "use_literature": new_use_literature,
            "literature_result": new_literature,
        }

    # If the LLM didn't change the Cypher steps, skip re-translate/re-execute.
    # HIRN search (if needed) was already started above and will be collected.
    if _plans_steps_equal(new_plan, current_plan):
        merged_plan = dict(current_plan)
        if new_plan.get("interpreted_question"):
            merged_plan["interpreted_question"] = new_plan["interpreted_question"]
        if new_plan.get("reasoning"):
            merged_plan["reasoning"] = new_plan["reasoning"]

        new_literature = literature_result
        complexity = "complex" if merged_plan.get("plan_type") == "chain" else "simple"
        _emit_plan_event("plan_revised", original_question, merged_plan, neo4j_results, complexity,
                         use_literature=new_use_literature, literature_preview=new_literature if new_use_literature else "")
        return {
            "plan": merged_plan,
            "neo4j_results": neo4j_results,
            "cypher_queries": [r["query"] for r in neo4j_results
                               if not (isinstance(r.get("result", {}), dict) and "error" in r.get("result", {}))],
            "complexity": complexity,
            "error": None,
            "use_literature": new_use_literature,
            "literature_result": new_literature,
        }

    new_plan = translate_plan(new_plan)
    new_results = execute_plan(new_plan, hpap_handler=None, genomic_handler=_run_genomic_step, ssgsea_handler=_run_ssgsea_step, functional_data_handler=_run_functional_data_step)
    new_plan, new_results = _filter_empty_steps(new_plan, new_results)

    new_literature = literature_result

    cypher_queries = [
        r["query"] for r in new_results
        if not (isinstance(r.get("result", {}), dict) and "error" in r.get("result", {}))
    ]

    complexity = "complex" if new_plan.get("plan_type") == "chain" else "simple"

    _emit_plan_event("plan_revised", original_question, new_plan, new_results, complexity,
                     use_literature=new_use_literature,
                     literature_preview=new_literature if new_use_literature else "")

    return {
        "plan": new_plan,
        "neo4j_results": new_results,
        "cypher_queries": cypher_queries,
        "complexity": complexity,
        "error": None,
        "use_literature": new_use_literature,
        "literature_result": new_literature,
    }


def run_plan_confirm(
    question: str,
    neo4j_results: list[dict],
    cypher_queries: list[str],
    complexity: str,
    functions_result: str = "",
    use_literature: bool = False,
    literature_result: str = "",
    rigor: bool = True,
    pre_final_answer: str = "",
    chat_history: list[dict] | None = None,
) -> str:
    """Execute the final format/reasoning pipeline on already-retrieved data.

    If use_literature is True and literature_result contains HIRN data,
    it is incorporated into functions_result so the format/reasoning agents
    can reference publications in their output.

    ``pre_final_answer`` is forwarded to the format/reasoning pipeline where
    it is appended to the user input (see ``format_response.py``). Use it to
    pass prior conversation context (for follow-up questions) or a
    pre-formatted planner answer.

    If ``chat_history`` is provided and ``pre_final_answer`` is empty, a
    compact context string is built from the last 4 turns and used as
    ``pre_final_answer`` so the format/reasoning agent sees prior dialogue.
    """
    global RIGOR_MODE
    prev_rigor = RIGOR_MODE
    RIGOR_MODE = rigor
    try:
        # Literature is now appended post-hoc by server.py via combine_literature_block;
        # the format/reasoning agents receive only KG/SQL/ssGSEA data.
        final_functions_result = functions_result

        if chat_history and not pre_final_answer:
            turns = [
                f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content'][:500]}"
                for m in chat_history[-4:]
            ]
            pre_final_answer = "[Prior conversation]\n" + "\n".join(turns)

        is_complex = complexity == "complex"
        return _select_pipeline(
            is_complex=is_complex,
            human_query=question,
            neo4j_results=neo4j_results,
            cypher_queries=cypher_queries,
            functions_result=final_functions_result,
            use_glkb=use_literature,
            pre_final_answer=pre_final_answer,
        )
    finally:
        RIGOR_MODE = prev_rigor


def chat_forever():
    """Interactive mode: continuous conversation loop"""
    messages = []
    while True:
        question = input('Your question: ')
        messages, response = chat_one_round(messages, question)
        emit("final_response", {"response": response})
        md = extract_markdown(response)
        print("\n" + "=" * 60)
        print("MARKDOWN OUTPUT")
        print("=" * 60 + "\n")
        print(md)
        print("\n" + "=" * 60)


def _spawn_cli_literature_thread(question: str):
    """Start GLKB+HIRN in background for CLI plan mode. Returns (event, holder)."""
    import threading as _threading
    from literature_runner import run_literature_parallel as _run_lit_parallel
    _event = _threading.Event()
    _holder: dict = {"result": {}, "status": "pending"}

    def _target(q=question):
        try:
            r = _run_lit_parallel(q)
            _holder["result"] = r
            glkb_ok = (r.get("glkb") or {}).get("status") == "success"
            hirn_ok = (r.get("hirn") or {}).get("status") == "success"
            _holder["status"] = "success" if (glkb_ok or hirn_ok) else "failed"
        except Exception as exc:
            logger.warning(f"CLI literature thread: {exc}")
            _holder["status"] = "failed"
        finally:
            _event.set()

    _threading.Thread(target=_target, daemon=True, name="lit-cli").start()
    return _event, _holder


def _splice_cli_literature(md: str, lit_event, lit_holder, use_literature: bool) -> str:
    """Wait up to 30s for CLI literature thread and splice block into md."""
    if not use_literature:
        return md
    from literature_runner import combine_literature_block as _combine
    completed = lit_event.wait(timeout=30.0)
    if not completed:
        logger.warning("CLI literature thread did not complete within 30s; skipping.")
        return md
    lit_result = lit_holder.get("result") or {}
    block = _combine(
        glkb=lit_result.get("glkb"),
        hirn=lit_result.get("hirn"),
        use_literature=True,
    )
    if block:
        md = md.rstrip() + "\n\n" + block
    return md


def chat_plan_interactive():
    """Interactive plan-confirmation loop.

    Flow:
      1. User enters a question → plan is generated, translated, and executed.
         Literature search (GLKB + HIRN) runs in background in parallel.
      2. The plan (including literature status) is displayed as Markdown for review.
      3. User can type revision prompts to modify the plan (loop).
         User can toggle literature with phrases like "also search literature"
         or "no literature".
      4. User types 'confirm' or 'yes' to run the final answer pipeline.
      5. User types 'new' to start over with a fresh question.
      6. User types 'quit' or 'exit' to leave.
    """
    print("\n" + "=" * 60)
    print("INTERACTIVE PLAN MODE")
    print("=" * 60)
    print("Commands:")
    print("  confirm / yes  — accept the plan and generate final answer")
    print("  new            — start over with a new question")
    print("  quit / exit    — leave")
    print("  (anything else is treated as a revision prompt)")
    print("=" * 60 + "\n")

    while True:
        question = input("\nYour question (or 'quit'): ").strip()
        if not question or question.lower() in ("quit", "exit"):
            print("Goodbye.")
            break

        question = clean_user_question(question)
        print(f"\nInterpreted: {question}")
        print("Generating plan...")
        try:
            result = run_plan_start(question)
        except Exception as e:
            print(f"\nError generating plan: {e}")
            traceback.print_exc()
            continue

        plan = result["plan"]
        neo4j_results = result["neo4j_results"]
        cypher_queries = result["cypher_queries"]
        complexity = result["complexity"]
        use_literature = result.get("use_literature", True)
        literature_result = result.get("literature_result", "")

        # Spawn GLKB+HIRN in background — latency hides behind plan review.
        _lit_event, _lit_holder = _spawn_cli_literature_thread(question)

        plan_md = format_plan_as_markdown(
            question, plan, neo4j_results,
            use_literature=use_literature,
            literature_result=literature_result,
        )
        print("\n" + plan_md)

        while True:
            user_input = input("\nRevise, 'confirm', or 'new': ").strip()
            if not user_input:
                continue

            cmd = user_input.lower()
            if cmd in ("quit", "exit"):
                print("Goodbye.")
                return
            if cmd == "new":
                break
            if cmd in ("confirm", "yes", "ok", "y"):
                print("\nRunning final answer pipeline...")
                try:
                    response = run_plan_confirm(
                        question=question,
                        neo4j_results=neo4j_results,
                        cypher_queries=cypher_queries,
                        complexity=complexity,
                        use_literature=False,
                        literature_result="",
                        rigor=RIGOR_MODE,
                    )
                    emit("final_response", {"response": response})
                    md = extract_markdown(response)
                    md = _splice_cli_literature(md, _lit_event, _lit_holder, use_literature)
                    print("\n" + "=" * 60)
                    print("FINAL ANSWER")
                    print("=" * 60 + "\n")
                    print(md)
                    print("\n" + "=" * 60)
                except Exception as e:
                    print(f"\nError generating answer: {e}")
                    traceback.print_exc()
                break

            print("\nRevising plan...")
            try:
                rev = run_plan_revise(
                    original_question=question,
                    current_plan=plan,
                    neo4j_results=neo4j_results,
                    user_prompt=user_input,
                    use_literature=use_literature,
                    literature_result=literature_result,
                )
            except Exception as e:
                print(f"\nRevision error: {e}")
                traceback.print_exc()
                continue

            if rev.get("error"):
                print(f"\n> Note: {rev['error']}")

            plan = rev["plan"]
            neo4j_results = rev["neo4j_results"]
            cypher_queries = rev["cypher_queries"]
            complexity = rev["complexity"]
            use_literature = rev.get("use_literature", use_literature)
            literature_result = rev.get("literature_result", literature_result)

            plan_md = format_plan_as_markdown(
                question, plan, neo4j_results,
                use_literature=use_literature,
                literature_result=literature_result,
            )
            print("\n" + plan_md)


def chat_single_round(question: str) -> str:
    """Single round mode: ask one question and return the answer"""
    with open("log.txt", "w") as f:
        pass
    
    messages = []
    _, response = chat_one_round(messages, question)
    return response


if __name__ == "__main__":
    args = sys.argv[1:]
    if '--rigor' in args:
        RIGOR_MODE = True
        args.remove('--rigor')
        emit("rigor_mode", {"enabled": True})

    plan_mode = '--plan' in args
    if plan_mode:
        args.remove('--plan')

    # ---- Pre-initialise heavy singletons in the MAIN thread ----
    # This forces all imports (langchain, cypher_validator, schema_loader,
    # etc.) to complete NOW, before any worker threads are spawned.
    # Without this, threads fight over Python's import lock and
    # _text2cypher_lock, causing 120-second translate_plan timeouts.
    print("Warming up agents...")
    from qp_query_planner import _get_text2cypher_agent as _warmup_agent
    _warmup_agent()

    hirn_skill_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'hirn_publication_retrieval', 'skills', 'hirn-literature-retrieve',
    )
    if hirn_skill_dir not in sys.path:
        sys.path.insert(0, hirn_skill_dir)
    import scripts.scrape_hirn, scripts.resolve_ids    # noqa: F401
    import scripts.fetch_fulltext, scripts.chunk_text   # noqa: F401
    import scripts.search_chunks, scripts.query_expander  # noqa: F401

    # HPAP MySQL skill disabled — donor metadata is now in the Neo4j KG (donor nodes).

    genomic_skill_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'PankBaseAgent', 'text_to_sql',
    )
    if genomic_skill_dir not in sys.path:
        sys.path.insert(0, genomic_skill_dir)
    from src.text2sql_agent import Text2SQLAgent  # noqa: F401
    from src.sql_validator import validate_sql  # noqa: F401

    ssgsea_skill_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'skills', 'ssgsea',
    )
    if ssgsea_skill_dir not in sys.path:
        sys.path.insert(0, ssgsea_skill_dir)
    import ssgsea_client  # noqa: F401

    print("Agents ready.")

    if args:
        question = ' '.join(args)
        if plan_mode:
            print(f"\nQuestion: {question}")
            question = clean_user_question(question)
            print(f"Interpreted: {question}")
            print("Generating plan...")
            result = run_plan_start(question)
            _use_lit = result.get("use_literature", True)
            _lit_event, _lit_holder = _spawn_cli_literature_thread(question)
            plan_md = format_plan_as_markdown(
                question, result["plan"], result["neo4j_results"],
                use_literature=_use_lit,
                literature_result=result.get("literature_result", ""),
            )
            print("\n" + plan_md)
            while True:
                user_input = input("\nRevise, 'confirm', or 'quit': ").strip()
                if not user_input:
                    continue
                cmd = user_input.lower()
                if cmd in ("quit", "exit"):
                    break
                if cmd in ("confirm", "yes", "ok", "y"):
                    print("\nRunning final answer pipeline...")
                    response = run_plan_confirm(
                        question=question,
                        neo4j_results=result["neo4j_results"],
                        cypher_queries=result["cypher_queries"],
                        complexity=result["complexity"],
                        use_literature=False,
                        literature_result="",
                        rigor=RIGOR_MODE,
                    )
                    emit("final_response", {"response": response})
                    md = extract_markdown(response)
                    md = _splice_cli_literature(md, _lit_event, _lit_holder, _use_lit)
                    print("\n" + "=" * 60)
                    print("FINAL ANSWER")
                    print("=" * 60 + "\n")
                    print(md)
                    print("\n" + "=" * 60)
                    break
                print("\nRevising plan...")
                rev = run_plan_revise(
                    original_question=question,
                    current_plan=result["plan"],
                    neo4j_results=result["neo4j_results"],
                    user_prompt=user_input,
                    use_literature=result.get("use_literature", False),
                    literature_result=result.get("literature_result", ""),
                )
                if rev.get("error"):
                    print(f"\nRevision failed: {rev['error']}")
                    continue
                result["plan"] = rev["plan"]
                result["neo4j_results"] = rev["neo4j_results"]
                result["cypher_queries"] = rev["cypher_queries"]
                result["complexity"] = rev["complexity"]
                result["use_literature"] = rev.get("use_literature", result.get("use_literature", True))
                _use_lit = result["use_literature"]
                result["literature_result"] = rev.get("literature_result", result.get("literature_result", ""))
                plan_md = format_plan_as_markdown(
                    question, rev["plan"], rev["neo4j_results"],
                    use_literature=_use_lit,
                    literature_result=result["literature_result"],
                )
                print("\n" + plan_md)
        else:
            response = chat_single_round(question)
            emit("final_response", {"response": response})
            md = extract_markdown(response)
            print("\n" + "=" * 60)
            print("MARKDOWN OUTPUT")
            print("=" * 60 + "\n")
            print(md)
            print("\n" + "=" * 60)
    else:
        with open("log.txt", "w") as f:
            pass
        if plan_mode:
            chat_plan_interactive()
        else:
            chat_forever()
