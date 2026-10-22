"""
Tests for the §6.5 reply grammar — the margin as a decision surface.

Parsing lives in cgate/feedback.py precisely so it is testable without Word:
the add-in only forwards a comment's own text and a reply's raw text; this
module decides what was meant, or that it could not tell.
"""

from __future__ import annotations

import os

import pytest

from cgate import feedback as fb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHECKLIST = os.path.join(ROOT, "config/checklist.example.yaml")


# ---------------------------------------------------------------------------
# parse_reply
# ---------------------------------------------------------------------------


def test_confirm_parses_case_insensitively():
    assert fb.parse_reply("confirm") == {"kind": "confirm"}
    assert fb.parse_reply("Confirm") == {"kind": "confirm"}
    assert fb.parse_reply("  CONFIRM  ") == {"kind": "confirm"}


def test_reject_carries_the_reason_verbatim():
    r = fb.parse_reply("Reject: this is boilerplate, not a real obligation on us")
    assert r == {"kind": "reject", "reason": "this is boilerplate, not a real obligation on us"}


def test_reject_reason_is_read_from_the_first_line_only():
    # PLAN §6.5: "Reply grammar, first line" — a reason does not continue past
    # a line break; that keeps a reviewer's aside on a later line from being
    # folded into the recorded reason.
    r = fb.parse_reply("Reject: wrong because it is boilerplate\nsee clause 4.2 for context")
    assert r == {"kind": "reject", "reason": "wrong because it is boilerplate"}


def test_classify_normalises_category_case_and_spacing():
    assert fb.parse_reply("Classify: r4") == {"kind": "classify", "category": "R4"}
    assert fb.parse_reply("classify:R12") == {"kind": "classify", "category": "R12"}


def test_ordinary_remarks_do_not_parse():
    assert fb.parse_reply("I'll check with legal on this one") is None
    assert fb.parse_reply("Confirmed by phone with the PM") is None  # not exactly "confirm"
    assert fb.parse_reply("") is None
    assert fb.parse_reply(None) is None


def test_only_the_first_line_is_read_as_grammar():
    # A reviewer's aside on a later line must not accidentally parse as
    # something else.
    assert fb.parse_reply("Confirm\nalso, tell the PM about this") == {"kind": "confirm"}


# ---------------------------------------------------------------------------
# req_id_from_comment
# ---------------------------------------------------------------------------


def test_req_id_extracted_with_hash_prefix():
    content = "CGATE 3f2a9c1b1e0d · req0017 · READ THE CLAUSE ITSELF · p.10, clause 1.2\n\nBINDS: ..."
    assert fb.req_id_from_comment(content) == "req0017"


def test_req_id_extracted_without_hash_prefix():
    content = "CGATE req0017 · READ THE CLAUSE ITSELF · p.10, clause 1.2"
    assert fb.req_id_from_comment(content) == "req0017"


def test_req_id_none_for_a_non_cgate_comment():
    assert fb.req_id_from_comment("Please double check this figure.") is None
    assert fb.req_id_from_comment("") is None


# ---------------------------------------------------------------------------
# POST /api/feedback/reply — end to end
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from cgate.serve import build_app

    app = build_app(CHECKLIST, str(tmp_path / "store"), str(tmp_path / "reviews"))
    return TestClient(app)


def _review(client):
    src = os.path.join(ROOT, "bench", "fixtures", "gc_integrity.txt")
    assert os.path.exists(src), "run `python examples/make_sample.py` first"
    with open(src, "rb") as fh:
        return client.post("/api/review", files={"files": ("gtc.txt", fh, "text/plain")}).json()


def test_a_parsing_reply_is_recorded_with_comment_reply_source(client, tmp_path):
    review = _review(client)
    req_id = review["provisions"][0]["req_id"]
    comment_content = f"CGATE {review['doc_id'][:12]} · {req_id} · READ THE CLAUSE ITSELF · p.1"

    r = client.post("/api/feedback/reply", json={
        "doc_id": review["doc_id"], "reviewer": "A. Reviewer",
        "comment_content": comment_content,
        "reply_text": "Reject: this is generic boilerplate",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["parsed"] is True
    assert body["recorded"] is True
    assert body["kind"] == "reject"

    ledger = fb.load_ledger(str(tmp_path / "store"))
    row = [x for x in ledger if x["req_id"] == req_id][0]
    assert row["kind"] == "reject"
    assert row["source"] == "comment-reply"
    assert row["reason"] == "this is generic boilerplate"


def test_a_non_parsing_reply_is_reported_not_dropped(client):
    review = _review(client)
    req_id = review["provisions"][0]["req_id"]
    comment_content = f"CGATE {req_id} · READ THE CLAUSE ITSELF · p.1"

    r = client.post("/api/feedback/reply", json={
        "doc_id": review["doc_id"], "reviewer": "A. Reviewer",
        "comment_content": comment_content,
        "reply_text": "I'll check with legal",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["parsed"] is False
    assert body["reason"]


def test_a_reply_on_a_comment_that_is_not_cgates_is_reported_not_recorded(client):
    review = _review(client)
    r = client.post("/api/feedback/reply", json={
        "doc_id": review["doc_id"], "reviewer": "A. Reviewer",
        "comment_content": "Just a normal Word comment, not ours.",
        "reply_text": "Confirm",
    })
    assert r.status_code == 200
    assert r.json()["parsed"] is False


def test_reply_needs_a_reviewer_name(client):
    review = _review(client)
    r = client.post("/api/feedback/reply", json={
        "doc_id": review["doc_id"], "reviewer": "  ",
        "comment_content": "CGATE req0001 · X · p.1",
        "reply_text": "Confirm",
    })
    assert r.status_code == 400
