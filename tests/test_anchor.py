"""
Tests for cgate.anchor — the anchor plan (see docs/ARCHITECTURE.md).

Deliberately no Word in this file. Everything the planner decides is pure
Python over two strings, and that is the whole point of splitting anchoring
into a plan (host) and an execution (add-in): the hard logic is testable
headless.
"""

from __future__ import annotations

import os

from cgate import anchor
from cgate.reading import EXPOSURE_ORDER

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------------------
# exact
# ---------------------------------------------------------------------------


def test_exact_match_single_occurrence():
    body = "Some preamble text. The Contractor shall not engage in any corrupt practice. Trailing text."
    quote = "The Contractor shall not engage in any corrupt practice."
    p = anchor.plan_one("req0001", quote, body)

    assert p.result == "exact"
    assert p.head is not None
    assert p.tail is None
    assert p.head.occurrences == 1
    assert p.head.ordinal == 1
    assert body[p.head.start:p.head.end] == quote


def test_exact_match_survives_whitespace_and_typography_drift():
    # The PDF-extracted quote is single-line; Word's converted body reflows it
    # across lines and uses a curly apostrophe.
    body = "Preamble.\nThe Contractor shall  not\nengage in the Contractor’s\r\nown corrupt practice.\nMore."
    quote = "The Contractor shall not engage in the Contractor's own corrupt practice."
    p = anchor.plan_one("req0002", quote, body)

    assert p.result == "exact"
    assert p.head is not None


# ---------------------------------------------------------------------------
# ordinal / repetition
# ---------------------------------------------------------------------------


def test_repeated_phrase_resolves_to_the_occurrence_near_the_alignment():
    filler = "X" * 500
    quote = "the Contractor shall comply with the Programme Cooperation Agreement"
    body = f"{quote} first. {filler} {quote} second."
    p = anchor.plan_one("req0003", quote, body)

    assert p.result == "exact"
    assert p.head.occurrences == 2
    # difflib's find on the normalised body returns the first occurrence when
    # the quote is verbatim, so ordinal 1 is correct here.
    assert p.head.ordinal == 1
    assert body[p.head.start:p.head.end] == quote


# ---------------------------------------------------------------------------
# two-point / multi-paragraph
# ---------------------------------------------------------------------------


def test_multi_paragraph_quote_gets_a_two_point_anchor():
    head_txt = "The Contractor shall maintain insurance covering all obligations under this Contract"
    tail_txt = "and shall furnish evidence of such insurance to the Purchaser upon request at any time"
    body = f"Preamble text here that is unrelated filler.\r\n{head_txt}\r\n\r\nUnrelated middle paragraph that has nothing to do with either half.\r\n\r\n{tail_txt}\r\nTrailer."
    quote = head_txt + " " + tail_txt

    p = anchor.plan_one("req0004", quote, body)

    assert p.multi_paragraph is True
    assert p.head is not None and p.tail is not None
    assert p.head.text in body
    assert p.tail.text in body
    assert p.head.start < p.head.end <= p.tail.start < p.tail.end
    assert p.span == (p.head.start, p.tail.end)


# ---------------------------------------------------------------------------
# fragment (partial coverage)
# ---------------------------------------------------------------------------


def test_partial_drift_yields_fragment_not_exact():
    # Half the quote is nowhere in the body; the other half is verbatim and
    # long enough to be a reliable fragment on its own.
    present = "the Contractor shall indemnify and hold the Purchaser harmless from any claim arising " \
              "out of the performance of this Contract in accordance with its terms"
    quote = present + " " + "a clause that was substantially rewritten and does not appear here at all in this form"
    body = f"Preamble. {present} Trailer text that continues on."

    p = anchor.plan_one("req0005", quote, body)

    assert p.result == "fragment"
    assert p.head is not None
    assert p.head.text in body


# ---------------------------------------------------------------------------
# ambiguous / failed
# ---------------------------------------------------------------------------


def test_scattered_short_matches_are_ambiguous_not_fabricated():
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    quote = " ".join(words)
    filler = " " + ("Z" * 80) + " "
    body = filler.join(words)

    p = anchor.plan_one("req0006", quote, body)

    assert p.result == "ambiguous"
    assert p.head is None
    assert p.span is None


def test_unrelated_quote_fails():
    quote = "9876543210 9876543210 9876543210"
    body = "The quick brown fox jumps over the lazy dog, repeatedly, in prose that shares no digits."
    p = anchor.plan_one("req0007", quote, body)

    assert p.result == "failed"


def test_empty_quote_fails():
    p = anchor.plan_one("req0008", "", "some body text")
    assert p.result == "failed"
    assert "no quote text" in p.reason


def test_empty_body_fails():
    p = anchor.plan_one("req0009", "some quote", "   ")
    assert p.result == "failed"


# ---------------------------------------------------------------------------
# the 255-char cap and word-boundary cutting
# ---------------------------------------------------------------------------


def test_long_quote_is_capped_and_cut_at_a_word_boundary():
    words = [f"word{i:04d}" for i in range(60)]  # ~540 chars with spaces
    long_text = " ".join(words)
    body = f"Preamble. {long_text} Trailer."
    p = anchor.plan_one("req0010", long_text, body)

    assert p.result == "exact"
    assert p.tail is None  # single paragraph, no break — one fragment
    assert len(p.head.text) <= anchor.MAX_FRAGMENT_CHARS
    # Cut at a word boundary: the fragment is itself a run of whole words.
    assert p.head.text == p.head.text.strip()
    for tok in p.head.text.split(" "):
        assert tok in words or tok == ""


def test_caret_is_stripped_from_the_fragment():
    quote = "the Contractor shall comply with clause 6.2^ of the General Conditions in full"
    body = f"Preamble. {quote} Trailer."
    p = anchor.plan_one("req0011", quote, body)

    assert p.head is not None
    assert "^" not in p.head.text


# ---------------------------------------------------------------------------
# coalescing overlapping anchors (§4.1.4 / §9.6)
# ---------------------------------------------------------------------------


def test_overlapping_anchors_coalesce_and_keep_the_worst_band():
    body = "Preamble. The Contractor shall comply with clause 6.2 of the General Conditions in full detail. Trailer."
    outer = "The Contractor shall comply with clause 6.2 of the General Conditions in full detail."
    inner = "shall comply with clause 6.2 of the General Conditions"

    plans = [
        anchor.plan_one("req0020", outer, body),
        anchor.plan_one("req0021", inner, body),
    ]
    bands = {"req0020": "routine", "req0021": "high"}

    anchors = anchor.coalesce(plans, bands)

    assert len(anchors) == 1
    assert set(anchors[0].req_ids) == {"req0020", "req0021"}
    assert anchors[0].band == "high"  # worst of routine/high, per EXPOSURE_ORDER
    assert EXPOSURE_ORDER.index("high") < EXPOSURE_ORDER.index("routine")


def test_non_overlapping_anchors_stay_separate():
    body = "First clause about insurance is here. " + ("filler " * 20) + "Second clause about indemnity is here."
    a = anchor.plan_one("req0030", "First clause about insurance is here.", body)
    b = anchor.plan_one("req0031", "Second clause about indemnity is here.", body)

    anchors = anchor.coalesce([a, b], {"req0030": "open", "req0031": "open"})

    assert len(anchors) == 2
    assert {tuple(x.req_ids) for x in anchors} == {("req0030",), ("req0031",)}


def test_ambiguous_and_failed_provisions_are_excluded_from_coalescing():
    body = "The Contractor shall comply with clause 6.2 in full."
    good = anchor.plan_one("req0040", "The Contractor shall comply with clause 6.2 in full.", body)
    bad = anchor.plan_one("req0041", "", body)  # failed: empty quote

    anchors = anchor.coalesce([good, bad], {"req0040": "routine", "req0041": "routine"})

    assert len(anchors) == 1
    assert anchors[0].req_ids == ["req0040"]


# ---------------------------------------------------------------------------
# the top-level plan() the API and bench call
# ---------------------------------------------------------------------------


def test_plan_reports_stats_and_keys_results_by_req_id():
    body = "The Contractor shall comply with clause 6.2 in full. Unrelated trailer text follows here."
    provisions = [
        {"req_id": "req0050", "quote": "The Contractor shall comply with clause 6.2 in full.", "band": "open"},
        {"req_id": "req0051", "quote": "9876543210 9876543210 9876543210", "band": "routine"},
    ]
    result = anchor.plan(provisions, body)

    assert result["stats"]["total"] == 2
    assert result["stats"]["exact"] == 1
    assert result["stats"]["failed"] == 1
    assert result["stats"]["anchored"] == 1
    assert result["stats"]["anchor_rate"] == 0.5
    assert set(result["results"].keys()) == {"req0050", "req0051"}
    assert len(result["anchors"]) == 1
    assert result["anchors"][0]["req_ids"] == ["req0050"]


def test_plan_on_empty_provision_list():
    result = anchor.plan([], "some body text")
    assert result["stats"]["total"] == 0
    assert result["stats"]["anchor_rate"] == 0.0
    assert result["anchors"] == []


# ---------------------------------------------------------------------------
# runs against a saved copy of the generated RFP's body text (no Word needed)
# ---------------------------------------------------------------------------


def test_planner_runs_against_the_generated_tender_fixture():
    # Written by `python examples/make_sample.py`. Missing is a failure, not a skip.
    fixture = os.path.join(HERE, "fixtures", "tender_body.txt")
    assert os.path.exists(fixture), "run `python examples/make_sample.py` first"

    with open(fixture, encoding="utf-8") as fh:
        body = fh.read()

    quote = "Bids shall remain valid for 120 days from the submission deadline"
    p = anchor.plan_one("req_fixture", quote, body)
    assert p.result == "exact"

    # A plainly-absent quote still fails cleanly rather than fabricating a fragment.
    absent = anchor.plan_one("req_fixture_absent", "0987654321" * 20, body)
    assert absent.result == "failed"


# ---------------------------------------------------------------------------
# POST /api/anchor-plan (serve.py) — the host side of §4.1, end to end
# ---------------------------------------------------------------------------

CHECKLIST = os.path.join(ROOT, "config/checklist.example.yaml")


def _client(tmp_path):
    pytest = __import__("pytest")
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from cgate.serve import build_app

    app = build_app(CHECKLIST, str(tmp_path / "store"), str(tmp_path / "reviews"))
    return TestClient(app)


def test_anchor_plan_needs_a_review_on_file(tmp_path):
    client = _client(tmp_path)
    r = client.post("/api/anchor-plan", json={"doc_id": "a" * 64, "body_text": "some text"})
    assert r.status_code == 404


def test_anchor_plan_rejects_empty_body_text(tmp_path):
    client = _client(tmp_path)
    r = client.post("/api/anchor-plan", json={"doc_id": "a" * 64, "body_text": "   "})
    assert r.status_code == 400


def test_anchor_plan_end_to_end_from_a_real_review(tmp_path):
    src = os.path.join(ROOT, "bench", "fixtures", "gc_integrity.txt")
    assert os.path.exists(src), "run `python examples/make_sample.py` first"

    client = _client(tmp_path)
    with open(src, "rb") as fh:
        review = client.post("/api/review", files={"files": ("gtc.txt", fh, "text/plain")}).json()
    assert review["provisions"], "the integrity fixture should yield provisions"

    with open(src, encoding="utf-8") as fh:
        body_text = fh.read()

    r = client.post("/api/anchor-plan", json={"doc_id": review["doc_id"], "body_text": body_text})
    assert r.status_code == 200
    plan = r.json()

    assert plan["stats"]["total"] == len(review["provisions"])
    assert set(plan["results"].keys()) == {p["req_id"] for p in review["provisions"]}
    # A .txt fixture round-trips through ingest with no PDF-conversion drift,
    # so this is close to the best case: most provisions should anchor.
    assert plan["stats"]["anchor_rate"] > 0.5
    for a in plan["anchors"]:
        assert a["req_ids"]
        assert a["band"] in EXPOSURE_ORDER
