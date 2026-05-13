"""
Functional Data REST API client for PanKgraph islet assay data.

Base URL: https://functional.pankgraph.org

Endpoints:
    GET /health
    GET /api/data/summary
    GET /api/data/donors
    GET /api/charts/cohort-traces
    GET /api/charts/trait-summary
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional
from urllib.parse import urlencode

import anthropic
import requests

logger = logging.getLogger(__name__)

FUNCTIONAL_BASE_URL = "https://functional.pankgraph.org"

_session: Optional[requests.Session] = None

# Directory of this file — used to locate sibling JSON spec files
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))

# Per-endpoint allowed params and their types
_ALLOWED_ENDPOINTS: dict[str, dict] = {
    "/api/data/summary": {
        "allowed_params": {"donor_ids"},
        "param_types": {"donor_ids": str},
    },
    "/api/data/donors": {
        "allowed_params": {"donor_ids", "disease", "sex", "center", "race",
                           "age_min", "age_max", "bmi_min", "bmi_max"},
        "param_types": {
            "donor_ids": str, "disease": str, "sex": str, "center": str, "race": str,
            "age_min": float, "age_max": float, "bmi_min": float, "bmi_max": float,
        },
    },
    "/api/charts/cohort-traces": {
        "allowed_params": {"trace_type", "donor_ids", "disease", "sex", "center", "race",
                           "age_min", "age_max", "bmi_min", "bmi_max"},
        "param_types": {
            "trace_type": str, "donor_ids": str, "disease": str, "sex": str,
            "center": str, "race": str,
            "age_min": float, "age_max": float, "bmi_min": float, "bmi_max": float,
        },
    },
    "/api/charts/trait-summary": {
        "allowed_params": {"trait", "limit", "donor_ids", "disease", "sex", "center", "race",
                           "age_min", "age_max", "bmi_min", "bmi_max"},
        "param_types": {
            "trait": str, "limit": int, "donor_ids": str, "disease": str, "sex": str,
            "center": str, "race": str,
            "age_min": float, "age_max": float, "bmi_min": float, "bmi_max": float,
        },
    },
}

# Loaded lazily from sibling JSON files
_API_SPEC: dict = {}
_TRAIT_LIST: list[str] = []
_INTERP_FULL: dict = {}  # full parsed interpretation JSON, used by build_functional_data_glossary


def _load_specs() -> None:
    """Load API spec and trait list from sibling JSON files."""
    global _API_SPEC, _TRAIT_LIST, _INTERP_FULL
    if _API_SPEC:
        return
    # Note: filename has a typo upstream ("funciton" not "function") — preserved
    api_spec_path = os.path.join(_SKILL_DIR, "funciton_API_skill.json")
    interp_path = os.path.join(_SKILL_DIR, "functional_data_interpretation_skill.json")
    try:
        with open(api_spec_path) as f:
            _API_SPEC = json.load(f)
    except Exception as exc:
        logger.warning("Could not load functional data API spec: %s", exc)
        _API_SPEC = {}
    try:
        with open(interp_path) as f:
            interp = json.load(f)
        _TRAIT_LIST = [entry["feature"] for entry in interp.get("feature_dictionary", [])]
        _INTERP_FULL = interp
    except Exception as exc:
        logger.warning("Could not load functional data interpretation spec: %s", exc)
        _TRAIT_LIST = []
        _INTERP_FULL = {}


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"Accept": "application/json"})
    return _session


def health_check(timeout: int = 5) -> bool:
    """Return True if the functional data API is reachable."""
    try:
        resp = _get_session().get(f"{FUNCTIONAL_BASE_URL}/health", timeout=timeout)
        return resp.status_code < 500
    except Exception:
        return False


def call_endpoint(endpoint: str, params: dict, timeout: int = 20) -> dict:
    """GET an API endpoint with the given query params. Returns the JSON response body."""
    url = f"{FUNCTIONAL_BASE_URL}{endpoint}"
    resp = _get_session().get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def extract_endpoint_and_params(
    question: str,
    prior_donor_ids: list[str] | None,
) -> dict:
    """Use Claude Sonnet to map a natural language question to {endpoint, params}.

    ``prior_donor_ids``, if non-empty, overrides any donor_ids the LLM may suggest.

    Returns::
        {"endpoint": "/api/...", "params": {...}, "rationale": "..."}
    """
    _load_specs()

    trait_list_str = (
        "\n".join(f"  - {t}" for t in _TRAIT_LIST)
        if _TRAIT_LIST
        else "  (traits unavailable)"
    )

    system_prompt = f"""You are an assistant that maps a natural language question to a \
PanKgraph Functional Data API call.

## API Specification
{json.dumps(_API_SPEC, indent=2)}

## Available Trait Names (for /api/charts/trait-summary)
{trait_list_str}

## Your task
Choose EXACTLY ONE endpoint from:
  /api/data/summary
  /api/data/donors
  /api/charts/cohort-traces
  /api/charts/trait-summary

Rules:
- Omit params that are not mentioned or not relevant.
- For trace_type in /api/charts/cohort-traces, default to "ins_ieq" unless the question \
asks about glucagon (use "gcg_ieq").
- For trait in /api/charts/trait-summary, match the question to the closest entry from \
the Available Trait Names list. Use the exact feature string.
- For limit in /api/charts/trait-summary, default to 8.

Respond with ONLY a JSON object (no markdown fences, no comments) in this exact format:
{{"endpoint": "/api/...", "params": {{}}, "rationale": "one sentence"}}"""

    user_msg = f"Question: {question}"
    if prior_donor_ids:
        user_msg += (
            f"\nNote: donor_ids from prior pipeline step (use as override): "
            f"{','.join(prior_donor_ids[:200])}"
        )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()
        # Strip any accidental markdown fences
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        sel = json.loads(text)
        # Override donor_ids with prior entities
        if prior_donor_ids:
            sel.setdefault("params", {})
            sel["params"]["donor_ids"] = ",".join(prior_donor_ids[:200])
        return sel
    except Exception as exc:
        logger.warning("Claude endpoint extraction failed: %s", exc)
        # Fallback: summary endpoint
        result: dict = {
            "endpoint": "/api/data/summary",
            "params": {},
            "rationale": "fallback to summary (extraction error)",
        }
        if prior_donor_ids:
            result["params"]["donor_ids"] = ",".join(prior_donor_ids[:200])
        return result


def _validate_selection(sel: dict) -> tuple[bool, str]:
    """Validate endpoint and params against the allow-list.

    Coerces types in-place and strips unknown params (rather than hard-failing).
    Returns (ok, error_message).
    """
    endpoint = sel.get("endpoint")
    if endpoint not in _ALLOWED_ENDPOINTS:
        return False, f"Unknown endpoint: {endpoint!r}"

    spec = _ALLOWED_ENDPOINTS[endpoint]
    params: dict = sel.get("params", {}) or {}

    # Strip unknown params silently
    for k in list(params.keys()):
        if k not in spec["allowed_params"]:
            del params[k]

    # Coerce types
    for k in list(params.keys()):
        target_type = spec["param_types"].get(k)
        if target_type and not isinstance(params[k], target_type):
            try:
                params[k] = target_type(params[k])
            except (ValueError, TypeError):
                del params[k]

    # Clamp limit for trait-summary to 1..8
    if endpoint == "/api/charts/trait-summary" and "limit" in params:
        params["limit"] = max(1, min(8, int(params["limit"])))

    sel["params"] = params
    return True, ""


# ---------------------------------------------------------------------------
# Per-endpoint response summarizers
# ---------------------------------------------------------------------------

def _auc_trapezoid(times: list, values: list) -> float:
    """Trapezoidal AUC over time series. Skips any None entries."""
    if len(times) != len(values) or len(times) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(times)):
        v0, v1 = values[i - 1], values[i]
        if v0 is None or v1 is None:
            continue
        dt = times[i] - times[i - 1]
        total += dt * (float(v1) + float(v0)) / 2.0
    return round(total, 4)


def _summarize_summary(payload: dict) -> dict:
    """Convert /api/data/summary response to rows-shaped result (one row per trait)."""
    summary = payload.get("summary", {})
    traits = payload.get("traits", [])
    available = summary.get("available_donors")
    rows = [{"trait": t, "available_donors": available} for t in traits]
    return {
        "rows": rows,
        "row_count": len(rows),
        "available_donors": available,
        "trace_types": summary.get("trace_types"),
        "options": payload.get("options", {}),
        "ranges": payload.get("ranges", {}),
    }


def _summarize_donors(payload: dict) -> dict:
    """Convert /api/data/donors response to rows-shaped result."""
    donors = payload.get("donors", [])
    rows = []
    for d in donors:
        row = dict(d)
        # Ensure a donor_id key exists for downstream entity extraction
        if "donor_id" not in row:
            for alias in ("id", "center_donor_id", "hpap_id"):
                if alias in row:
                    row["donor_id"] = row[alias]
                    break
        rows.append(row)
    return {
        "rows": rows,
        "row_count": payload.get("count", len(rows)),
    }


def _summarize_cohort_traces(payload: dict, k: int = 8) -> dict:
    """Convert /api/charts/cohort-traces to a token-budget-safe rows shape.

    Drops per-donor raw ``values[]`` arrays. Keeps ``mean[]``, ``times[]``,
    ``stimuli[]``. Returns per-donor summary rows:
    ``{donor_id, auc, peak, peak_time}`` sorted by AUC descending, capped at k.
    """
    times: list = payload.get("times", [])
    mean: list = payload.get("mean", [])
    stimuli: list = payload.get("stimuli", [])
    series: list = payload.get("series", [])
    trace_type: str = payload.get("trace_type", "")
    y_label: str = payload.get("y_label", "")

    donor_rows = []
    for s in series:
        donor_id = s.get("donor_id", "")
        raw_values: list = s.get("values", []) or []
        # Strip None entries (some donors may have gaps in their time series)
        paired = [(t, v) for t, v in zip(times, raw_values) if v is not None]
        clean_times = [p[0] for p in paired]
        values = [p[1] for p in paired]
        auc = _auc_trapezoid(clean_times, values) if len(values) >= 2 else 0.0
        if values:
            peak_idx = int(values.index(max(values)))
            peak = round(float(max(values)), 4)
            peak_time = clean_times[peak_idx] if peak_idx < len(clean_times) else None
        else:
            peak = None
            peak_time = None
        donor_rows.append({
            "donor_id": donor_id,
            "auc": auc,
            "peak": peak,
            "peak_time": peak_time,
        })

    donor_rows.sort(key=lambda x: x.get("auc") or 0, reverse=True)

    return {
        "rows": donor_rows[:k],
        "row_count": len(series),
        "trace_type": trace_type,
        "y_label": y_label,
        "times": times,
        "mean": mean,
        "stimuli": stimuli,
    }


def _summarize_trait_summary(payload: dict) -> dict:
    """Convert /api/charts/trait-summary response to rows-shaped result."""
    items = payload.get("items", [])
    trait = payload.get("trait", "")
    rows = []
    for item in items:
        row = dict(item)
        # Normalize label to donor_id for downstream entity extraction (best-effort)
        if "donor_id" not in row and "label" in row:
            row["donor_id"] = row["label"]
        rows.append(row)
    return {
        "rows": rows,
        "row_count": len(rows),
        "trait": trait,
    }


# ---------------------------------------------------------------------------
# Glossary builder — for format/reasoning agents
# ---------------------------------------------------------------------------

def build_functional_data_glossary(neo4j_results: list[dict]) -> str:
    """Return a Markdown glossary block for any functional_data results present.

    Smart-filtered: scans result rows and top-level trait fields for feature
    strings, matches them against feature_dictionary entries, and emits only
    the matched subset. Always includes core_rules and global_terms (small,
    always biologically relevant). Falls back to all 20 features when no
    matches are found.

    Returns "" if no functional_data source is present in neo4j_results.
    """
    _load_specs()
    if not _INTERP_FULL:
        return ""

    # Check whether any functional_data result is present
    has_fd = any(
        isinstance(entry.get("result"), dict)
        and entry["result"].get("source") == "functional_data"
        for entry in neo4j_results
    )
    if not has_fd:
        return ""

    feature_dict = _INTERP_FULL.get("feature_dictionary", [])
    # Build a casefold lookup: casefold(feature) -> entry
    _feature_lookup: dict[str, dict] = {e["feature"].casefold(): e for e in feature_dict}

    # Collect candidate feature strings from results
    candidates: set[str] = set()
    for entry in neo4j_results:
        result = entry.get("result", {})
        if not isinstance(result, dict) or result.get("source") != "functional_data":
            continue
        # Top-level trait key (trait-summary endpoint)
        if result.get("trait"):
            candidates.add(str(result["trait"]).casefold())
        # Row keys and string values
        for row in result.get("rows", []):
            if not isinstance(row, dict):
                continue
            for k, v in row.items():
                candidates.add(str(k).casefold())
                if isinstance(v, str):
                    candidates.add(v.casefold())

    matched = [_feature_lookup[c] for c in candidates if c in _feature_lookup]
    if not matched:
        matched = feature_dict  # full-glossary fallback

    lines: list[str] = ["\n=== FUNCTIONAL DATA GLOSSARY ==="]

    # Global terms
    global_terms: dict = _INTERP_FULL.get("global_terms", {})
    if global_terms:
        lines.append("\n## Global terms")
        for term, defn in global_terms.items():
            lines.append(f"- **{term}**: {defn}")

    # Core rules
    core_rules: list = _INTERP_FULL.get("core_rules", [])
    if core_rules:
        lines.append("\n## Core interpretation rules")
        for i, rule in enumerate(core_rules, 1):
            lines.append(f"{i}. {rule}")

    # Matched feature entries
    lines.append(f"\n## Trait dictionary ({'matched to your results' if matched is not feature_dict else 'full — no exact match found'})")
    for e in matched:
        lines.append(
            f"- **{e['feature']}** — hormone={e.get('hormone','?')}, "
            f"condition={e.get('condition','?')}, "
            f"measurement_type={e.get('measurement_type','?')}, "
            f"unit={e.get('unit','?')}, "
            f"normalization={e.get('normalization','?')}. "
            f"Interpretation: {e.get('interpretation','')}"
        )

    return "\n".join(lines)
