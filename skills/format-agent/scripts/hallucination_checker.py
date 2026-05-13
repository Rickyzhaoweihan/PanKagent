"""Hallucination detection and cleanup for FormatAgent output."""
from __future__ import annotations

import os
import re
import sys
from typing import List

# Structured streaming events
_repo_root_for_events = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _repo_root_for_events not in sys.path:
    sys.path.insert(0, _repo_root_for_events)
from stream_events import emit


def extract_ids_from_text(text: str) -> dict:
    """
    Extract GO terms and PubMed IDs from text.

    Returns:
        dict with 'go_terms' and 'pubmed_ids' lists
    """
    # GO terms (GO:XXXXXXX or GO_XXXXXXX)
    go_pattern = r'GO[_:]?\d{7}'
    go_terms = list(set(re.findall(go_pattern, text, re.IGNORECASE)))
    go_terms = [g.upper().replace(':', '_') for g in go_terms]

    # PubMed IDs
    pubmed_ids: set[str] = set()
    pubmed_ids.update(re.findall(r'PubMed\s*(?:ID)?[:\s]*(\d{6,9})', text, re.IGNORECASE))
    pubmed_ids.update(re.findall(r'PMID[:\s]*(\d{6,9})', text, re.IGNORECASE))
    pubmed_ids.update(re.findall(r'\[(\d{7,8})\]', text))

    return {
        'go_terms': go_terms,
        'pubmed_ids': list(pubmed_ids)
    }


def extract_ids_from_retrieved_data(neo4j_results: List[dict],
                                     raw_agent_output: str = "") -> dict:
    """
    Extract all GO terms and PubMed IDs from the retrieved data sources.

    Args:
        neo4j_results: List of Neo4j query results
        raw_agent_output: Raw output from sub-agents (includes HIRN literature results)

    Returns:
        dict with 'go_terms' and 'pubmed_ids' sets
    """
    valid_go_terms: set[str] = set()
    valid_pubmed_ids: set[str] = set()

    # From Neo4j results
    for result in neo4j_results:
        result_str = str(result)
        go_matches = re.findall(r'GO[_:]?\d{7}', result_str, re.IGNORECASE)
        for g in go_matches:
            valid_go_terms.add(g.upper().replace(':', '_'))

    # Literature PMIDs are no longer in agent input (spliced post-hoc) — valid_pubmed_ids
    # stays empty so any PMID the agent emits is correctly flagged and stripped.
    if raw_agent_output:
        go_matches = re.findall(r'GO[_:]?\d{7}', raw_agent_output, re.IGNORECASE)
        for g in go_matches:
            valid_go_terms.add(g.upper().replace(':', '_'))

    return {
        'go_terms': valid_go_terms,
        'pubmed_ids': valid_pubmed_ids
    }


def check_hallucination(summary: str, neo4j_results: List[dict],
                         raw_agent_output: str = "") -> dict:
    """
    Check if GO terms and PubMed IDs in the summary actually exist in retrieved data.

    Args:
        summary: The final summary text to check
        neo4j_results: List of Neo4j query results
        raw_agent_output: Raw output from sub-agents

    Returns:
        dict with hallucination report including 'is_clean', 'hallucinated_go_terms',
        'hallucinated_pubmed_ids', and human-readable 'report'.
    """
    found_pubmed = []
    if raw_agent_output:
        found_pubmed = re.findall(r'PubMed[^0-9]*(\d{6,9})', raw_agent_output, re.IGNORECASE)
    emit("hallucination_check_start", {
        "raw_agent_output_length": len(raw_agent_output) if raw_agent_output else 0,
        "pubmed_ids_in_output": found_pubmed,
    })

    summary_ids = extract_ids_from_text(summary)
    valid_ids = extract_ids_from_retrieved_data(neo4j_results, raw_agent_output)

    hallucinated_go = [g for g in summary_ids['go_terms'] if g not in valid_ids['go_terms']]
    hallucinated_pubmed = [p for p in summary_ids['pubmed_ids'] if p not in valid_ids['pubmed_ids']]

    is_clean = len(hallucinated_go) == 0 and len(hallucinated_pubmed) == 0

    # Build report
    lines = ["=" * 60, "HALLUCINATION CHECK REPORT", "=" * 60]
    lines.append(f"\n📊 Summary contains:")
    lines.append(f"   - {len(summary_ids['go_terms'])} GO terms: "
                 f"{summary_ids['go_terms'][:5]}{'...' if len(summary_ids['go_terms']) > 5 else ''}")
    lines.append(f"   - {len(summary_ids['pubmed_ids'])} PubMed IDs: "
                 f"{summary_ids['pubmed_ids'][:5]}{'...' if len(summary_ids['pubmed_ids']) > 5 else ''}")
    lines.append(f"\n📚 Retrieved data contains:")
    lines.append(f"   - {len(valid_ids['go_terms'])} GO terms")
    lines.append(f"   - {len(valid_ids['pubmed_ids'])} PubMed IDs")

    if is_clean:
        lines.append(f"\n✅ NO HALLUCINATIONS DETECTED")
    else:
        lines.append(f"\n⚠️  HALLUCINATIONS DETECTED:")
        if hallucinated_go:
            lines.append(f"   ❌ Fake GO terms: {hallucinated_go}")
        if hallucinated_pubmed:
            lines.append(f"   ❌ Fake PubMed IDs: {hallucinated_pubmed}")

    lines.append("=" * 60)

    return {
        'summary_ids': summary_ids,
        'valid_ids': {
            'go_terms': list(valid_ids['go_terms']),
            'pubmed_ids': list(valid_ids['pubmed_ids'])
        },
        'hallucinated_go_terms': hallucinated_go,
        'hallucinated_pubmed_ids': hallucinated_pubmed,
        'is_clean': is_clean,
        'report': '\n'.join(lines)
    }


def remove_hallucinated_ids(summary: str, fake_go_terms: list,
                             fake_pubmed_ids: list) -> str:
    """
    Remove hallucinated GO terms and PubMed IDs from the summary text.

    Args:
        summary: The summary text to clean
        fake_go_terms: List of GO term IDs to remove (e.g., ['GO_0005789'])
        fake_pubmed_ids: List of PubMed IDs to remove (e.g., ['34012112'])

    Returns:
        Cleaned summary with fake IDs removed
    """
    cleaned = summary

    # Remove fake PubMed IDs
    for pmid in fake_pubmed_ids:
        cleaned = re.sub(r'\s*\[PubMed\s*ID:\s*' + re.escape(pmid) + r'\]', '', cleaned)
        cleaned = re.sub(r'\s*\[PMID:\s*' + re.escape(pmid) + r'\]', '', cleaned)
        cleaned = re.sub(r'\s*\(PubMed\s*ID:\s*' + re.escape(pmid) + r'\)', '', cleaned)
        cleaned = re.sub(r'\s*PubMed\s*ID:\s*' + re.escape(pmid) + r'\b', '', cleaned)

    # Remove fake GO terms
    for go_term in fake_go_terms:
        go_id = go_term.replace('_', ':') if '_' in go_term else go_term
        go_id_underscore = go_term.replace(':', '_') if ':' in go_term else go_term

        for go_variant in [go_id, go_id_underscore]:
            cleaned = re.sub(r'\s*\(' + re.escape(go_variant) + r'\)', '', cleaned)
            cleaned = re.sub(
                r'[^,.\n]*?' + re.escape(go_variant) + r'\)\s*,?\s*',
                '', cleaned
            )

    # Clean up artifacts
    cleaned = re.sub(r'\s{2,}', ' ', cleaned)
    cleaned = re.sub(r',\s*,', ',', cleaned)
    cleaned = re.sub(r',\s*\.', '.', cleaned)
    cleaned = re.sub(r'\(\s*\)', '', cleaned)
    cleaned = re.sub(r':\s*\.', '.', cleaned)
    cleaned = re.sub(r':\s*$', '.', cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r'including\s*[.,]', 'including:', cleaned)
    cleaned = re.sub(r'\s+([.,])', r'\1', cleaned)

    return cleaned.strip()

