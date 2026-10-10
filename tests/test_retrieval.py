"""Retrieval evaluation: each question must surface the expected clause in the top 3."""
import pytest

from ridewise.ingest import build_chunks, ensure_index, index_is_fresh
from ridewise.retrieval import search_policy


@pytest.fixture(scope="module", autouse=True)
def index():
    ensure_index()


EVAL_SET = [
    ("refund if I cancel 20 hours before pickup", ["RW-POL-03 Annexure 1", "RW-POL-02 §1"]),
    ("GOLD grace period for late return", ["RW-POL-04 Annexure A"]),
    ("battery exactly 10 percent fee", ["RW-POL-05 Figure 2"]),
    ("what is a no-show", ["RW-POL-01 §7"]),
    ("refund if I do not show up for pickup", ["RW-POL-01 §7"]),
    ("can I charge the EV at home", ["RW-POL-05 §5 FAQ: Can I charge"]),
    ("when is my security deposit returned", ["RW-POL-01 §5"]),
    ("KYC pending can I pick up the vehicle", ["RW-POL-01 §2"]),
    ("lost key charge", ["RW-POL-04 §3"]),
    ("support phone number", ["RW-POL-01 §9"]),
    ("range of Ola S1 Pro", ["RW-POL-05 §1"]),
    ("battery ran out in the middle of my trip", ["RW-POL-05 §5 FAQ: What happens if the battery"]),
    ("can I return to a different hub", ["RW-POL-01 §3"]),
    ("do silver customers get any benefits", ["RW-POL-04 Annexure A", "RW-POL-01 §6"]),
    ("steps to charge the scooter", ["RW-POL-05 Figure 1"]),
    ("late fee per hour for KTM Duke 200", ["RW-POL-04 §1"]),
    ("how is the rental charge calculated for 27 hours", ["RW-POL-01 §4"]),
    ("damage claim disputed am I charged", ["RW-POL-04 §3"]),
]


@pytest.mark.parametrize("query, expected", EVAL_SET)
def test_expected_clause_in_top3(query, expected):
    sources = [h["source"] for h in search_policy(query, k=3)]
    for exp in expected:
        assert any(s.startswith(exp) for s in sources), f"{exp!r} not in {sources}"


def test_doc_filter_restricts_results():
    hits = search_policy("refund schedule", k=5, doc_ids=["RW-POL-03"])
    assert hits and {h["doc_id"] for h in hits} == {"RW-POL-03"}


def test_empty_query_returns_nothing():
    assert search_policy("   ") == []


def test_results_carry_citation_and_scope():
    hit = search_policy("refund 48 hours before pickup", k=1)[0]
    assert hit["source"].startswith("RW-POL-03 Annexure 1") and "transcribed" in hit["source"]
    assert "on or after 1 July 2026" in hit["applies_to"]


def test_chunks_are_sections_with_tables_as_markdown():
    chunks = build_chunks()
    assert 35 <= len(chunks) <= 50
    rental = next(c for c in chunks if c["source_label"].startswith("RW-POL-01 §4"))
    assert rental["text"].count("|Booked duration|Calculation|Rental charge|") == 1   # split table merged
    assert "|27 hours|1 × ₹700 + 3 × ₹80|₹940|" in rental["text"]
    late = next(c for c in chunks if c["source_label"].startswith("RW-POL-04 §1"))
    assert "|Ultraviolette F77|EV bike|₹200|₹300|" in late["text"]
    # image-only annexures come only from the verbatim transcription
    annex = [c for c in chunks if "Annexure" in c["section"]]
    assert annex and all(c["transcribed"] for c in annex)


def test_index_is_fresh_after_build():
    assert index_is_fresh()
