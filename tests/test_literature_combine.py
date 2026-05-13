"""Hardcoded literature merge — six canonical scenarios."""
from __future__ import annotations


def _glkb_ok(refs_n: int = 5):
    return {
        "status": "success",
        "response": "CFTR encodes a chloride channel critical for ion homeostasis [PMID: 1].",
        "references": [
            {"pmid": str(i), "title": f"Paper {i}", "url": f"https://pubmed/{i}",
             "journal": "NEJM", "date": "2020", "authors": ["A B"]}
            for i in range(1, refs_n + 1)
        ],
        "session_id": "g",
        "execution_time": 25.0,
    }


def _glkb_fail():
    return {"status": "failed", "response": "", "references": [],
            "session_id": "", "execution_time": 0.0}


def _hirn_ok():
    return {
        "status": "success",
        "summary": "HIRN literature shows CFTR involvement in beta-cell stress.",
        "raw_passages": [
            {"pmid": "99", "title": "HIRN paper", "passage": "Beta cells...",
             "url": "https://pubmed/99"},
            {"pmid": "100", "title": "Another", "passage": "Stress response...",
             "url": "https://pubmed/100"},
        ],
    }


def _hirn_empty():
    return {"status": "success", "summary": "", "raw_passages": []}


def test_glkb_primary_with_enough_refs():
    from literature_runner import combine_literature_block
    out = combine_literature_block(_glkb_ok(5), _hirn_ok(), use_literature=True)
    assert "## Literature Evidence" in out
    assert "CFTR encodes a chloride channel" in out
    assert "## References" in out
    # GLKB had >=3 refs so HIRN supplement should NOT appear
    assert "Additional HIRN Evidence" not in out
    # All 5 GLKB refs should be in the references section
    for i in range(1, 6):
        assert f"PMID: {i}" in out or f"pubmed/{i}" in out


def test_glkb_thin_hirn_supplements():
    from literature_runner import combine_literature_block
    out = combine_literature_block(_glkb_ok(2), _hirn_ok(), use_literature=True)
    assert "## Literature Evidence" in out
    assert "Additional HIRN Evidence" in out
    # HIRN PMIDs should appear in References
    assert "99" in out and "100" in out


def test_glkb_failed_hirn_only():
    from literature_runner import combine_literature_block
    out = combine_literature_block(_glkb_fail(), _hirn_ok(), use_literature=True)
    assert "## Literature Evidence" in out
    assert "HIRN literature shows" in out
    assert "99" in out


def test_both_failed_returns_empty():
    from literature_runner import combine_literature_block
    out = combine_literature_block(_glkb_fail(), _hirn_empty(), use_literature=True)
    assert out == ""


def test_use_literature_false_returns_empty():
    from literature_runner import combine_literature_block
    out = combine_literature_block(_glkb_ok(), _hirn_ok(), use_literature=False)
    assert out == ""


def test_references_are_deduped_by_pmid():
    from literature_runner import combine_literature_block
    glkb = _glkb_ok(2)
    # HIRN cites the same PMID as one of GLKB's refs
    hirn = {"status": "success", "summary": "shared", "raw_passages": [
        {"pmid": "1", "title": "Duplicate", "passage": "x", "url": "https://pubmed/1"},
        {"pmid": "555", "title": "New", "passage": "y", "url": "https://pubmed/555"},
    ]}
    out = combine_literature_block(glkb, hirn, use_literature=True)
    # PMID 1 should appear exactly once in the References list
    assert out.count("PMID: 1\n") <= 1
    assert "555" in out
