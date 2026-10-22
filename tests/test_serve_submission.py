"""
Integration tests for the submission endpoints the Word add-in talks to.

These exercise the whole path the pane uses: upload the open document, get
marks back, then ask where in Word's own body text each mark should be placed.
"""

from __future__ import annotations

import io
import json

import docx
import pytest

from fastapi.testclient import TestClient

from cgate.serve import build_app


@pytest.fixture()
def client(tmp_path):
    app = build_app("config/checklist.example.yaml", str(tmp_path / "store"), str(tmp_path / "work"))
    return TestClient(app)


def docx_bytes(paragraphs, *, author=None) -> bytes:
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if author:
        d.core_properties.author = author
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


SUBMISSION = [
    "Technical Proposal.",
    "We are the leading consulting firm in the region.",
    "Key personnel: Alex Rowan, Team Leader, and Riley Stone, Resident Engineer.",
    "Laboratory testing will be undertaken by Granite Materials Testing Ltd.",
]

REGISTER = [{"name": "Alex Rowan", "status": "active", "role": "Team Leader"}]


def post_submission(client, *, register=None, parties=None, material=None):
    files = [("files", ("proposal.docx", docx_bytes(SUBMISSION), DOCX_MIME))]
    for i, m in enumerate(material or []):
        files.append(("files", (f"material-{i}.docx", m, DOCX_MIME)))
    if register is not None:
        files.append(("staff_register",
                      ("register.json", json.dumps(register).encode(), "application/json")))
    data = {"name": "proposal.docx"}
    if parties is not None:
        data["parties"] = json.dumps(parties)
    return client.post("/api/submission", files=files, data=data)


DOCX_MIME = ("application/vnd.openxmlformats-officedocument."
             "wordprocessingml.document")


# ---------------------------------------------------------------------------


def test_a_bare_submission_marks_claims_and_admits_the_rest(client):
    r = post_submission(client)
    assert r.status_code == 200
    body = r.json()

    assert body["counts"]["query"] >= 1
    assert body["counts"]["commendation"] == 0, "nothing was checked; nothing is verified"
    skipped = {s["rule"] for s in body["rubric"]["checks_skipped"]}
    assert {"personnel_register", "shared_content", "undeclared_parties"} <= skipped


def test_supplying_a_register_switches_on_the_personnel_check(client):
    r = post_submission(client, register=REGISTER)
    body = r.json()
    rules = {m["rule"] for m in body["marks"]}
    assert "personnel_register" in rules
    assert body["counts"]["commendation"] >= 1
    assert body["rubric"]["personnel_verified"] == ["Alex Rowan"]
    assert "Riley Stone" in body["rubric"]["personnel_unverified"]


def test_supplying_parties_switches_on_item_11(client):
    r = post_submission(client, parties=["Northern Survey Contractors LLC"])
    body = r.json()
    undeclared = [m for m in body["marks"] if m["rule"] == "undeclared_party"]
    assert any("Granite Materials Testing" in m["heading"] for m in undeclared)
    assert "undeclared_party_in_submission" in body["rubric"]["triggers"]


def test_a_bad_register_is_refused_rather_than_ignored(client):
    files = [("files", ("proposal.docx", docx_bytes(SUBMISSION), DOCX_MIME)),
             ("staff_register", ("register.json", b"{not json", "application/json"))]
    r = client.post("/api/submission", files=files)
    assert r.status_code == 400
    assert "not valid JSON" in r.json()["detail"]


def test_a_register_that_is_not_a_list_is_refused(client):
    files = [("files", ("proposal.docx", docx_bytes(SUBMISSION), DOCX_MIME)),
             ("staff_register", ("register.json", b'{"name": "x"}', "application/json"))]
    r = client.post("/api/submission", files=files)
    assert r.status_code == 400
    assert "list" in r.json()["detail"]


def test_an_unreadable_filetype_is_refused(client):
    files = [("files", ("scan.png", b"\x89PNG\r\n", "image/png"))]
    r = client.post("/api/submission", files=files)
    assert r.status_code == 415


def test_an_unknown_solicitation_is_refused_not_silently_skipped(client):
    files = [("files", ("proposal.docx", docx_bytes(SUBMISSION), DOCX_MIME))]
    r = client.post("/api/submission", files=files,
                    data={"solicitation_doc_id": "f" * 64})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------


def test_the_anchor_plan_finds_marks_in_the_word_side_text(client):
    body = post_submission(client, register=REGISTER).json()
    doc_id = body["submission_id"]

    # What Word would hand the pane: the same prose, reflowed the way a
    # converted document arrives.
    word_text = "\r".join(SUBMISSION)
    r = client.post("/api/submission/anchor-plan",
                    json={"doc_id": doc_id, "body_text": word_text})
    assert r.status_code == 200
    plan = r.json()
    assert plan["anchors"], "no mark could be placed in the document"

    kinds = {a["band"] for a in plan["anchors"]}
    assert kinds <= {"correction", "query", "commendation"}


def test_the_anchor_plan_refuses_an_unknown_document(client):
    r = client.post("/api/submission/anchor-plan",
                    json={"doc_id": "a" * 64, "body_text": "some text"})
    assert r.status_code == 404


def test_the_anchor_plan_requires_body_text(client):
    body = post_submission(client).json()
    r = client.post("/api/submission/anchor-plan",
                    json={"doc_id": body["submission_id"], "body_text": "  "})
    assert r.status_code == 400


def test_a_stored_review_can_be_read_back(client):
    body = post_submission(client, register=REGISTER).json()
    again = client.get(f"/api/submission/{body['submission_id']}")
    assert again.status_code == 200
    assert again.json()["submission_id"] == body["submission_id"]


def test_marks_with_no_quote_are_never_offered_for_anchoring(client):
    """A shared document author belongs to the file, not to a sentence."""
    material = [docx_bytes(["Unrelated third-party text."], author="S. Quill")]
    files = [("files", ("proposal.docx",
                        docx_bytes(SUBMISSION, author="S. Quill"), DOCX_MIME)),
             ("files", ("material.docx", material[0], DOCX_MIME))]
    body = client.post("/api/submission", files=files).json()

    origin = [m for m in body["marks"] if m["rule"] == "shared_origin"]
    assert origin, "the shared author should have been reported"
    assert origin[0]["quote"] == ""

    plan = client.post("/api/submission/anchor-plan",
                       json={"doc_id": body["submission_id"],
                             "body_text": "\r".join(SUBMISSION)}).json()
    planned = set(plan["results"].keys())
    assert origin[0]["mark_id"] not in planned


# ---------------------------------------------------------------------------
# Drafting a fix: the endpoint, and the two refusals that matter
# ---------------------------------------------------------------------------


def _submission_review(client):
    r = client.post("/api/submission",
                    files={"files": ("proposal.docx", docx_bytes(SUBMISSION),
                                     "application/vnd.openxmlformats-officedocument"
                                     ".wordprocessingml.document")})
    assert r.status_code == 200
    return r.json()


def test_a_fix_is_only_ever_proposed_for_a_submission_on_file(client):
    """PLAN §0.3: never silently modify the contract. An endpoint that drafted
    replacement text for any passage would let the pane suggest edits to the
    client's tender, which is indefensible however it is presented. The mark has
    to come from a stored review of OUR OWN paper."""
    r = client.post("/api/draft", json={"doc_id": "0" * 64, "mark_id": "m1"})
    assert r.status_code == 404
    assert "our own outgoing paper" in r.json()["detail"]


def test_a_commendation_has_nothing_to_fix(client):
    review = _submission_review(client)
    marks = review["marks"]
    if not any(m["kind"] == "commendation" for m in marks):
        pytest.skip("no commendation in this fixture")
    m = next(m for m in marks if m["kind"] == "commendation")
    r = client.post("/api/draft", json={"doc_id": review["submission_id"], "mark_id": m["mark_id"]})
    assert r.status_code == 400


def test_an_unknown_mark_is_refused(client):
    review = _submission_review(client)
    r = client.post("/api/draft", json={"doc_id": review["submission_id"], "mark_id": "nope"})
    assert r.status_code == 404


def test_the_model_being_down_is_retryable_and_declining_is_not(client, monkeypatch):
    """The pane renders these differently, and should: "the host is down" invites
    another press, "the model would have had to invent a source" does not."""
    from cgate import draft as draft_

    review = _submission_review(client)
    mark = next(m for m in review["marks"] if (m.get("quote") or "").strip()
                and m["kind"] != "commendation")

    def down(*a, **k):
        raise draft_.DraftUnavailable("no local model host", retryable=True)

    monkeypatch.setattr(draft_, "draft_fix", down)
    r = client.post("/api/draft", json={"doc_id": review["submission_id"], "mark_id": mark["mark_id"]})
    assert r.status_code == 503

    def declined(*a, **k):
        raise draft_.DraftUnavailable("the model declined to draft", retryable=False)

    monkeypatch.setattr(draft_, "draft_fix", declined)
    r = client.post("/api/draft", json={"doc_id": review["submission_id"], "mark_id": mark["mark_id"]})
    assert r.status_code == 422


def test_a_draft_arrives_labelled_as_a_suggestion(client, monkeypatch):
    from cgate import draft as draft_

    review = _submission_review(client)
    mark = next(m for m in review["marks"] if (m.get("quote") or "").strip()
                and m["kind"] != "commendation")

    monkeypatch.setattr(draft_, "draft_fix", lambda m, **k: draft_.Draft(
        mark_id=m["mark_id"], original=m["quote"],
        proposed="A more careful sentence.", model="qwen3:8b"))

    r = client.post("/api/draft", json={"doc_id": review["submission_id"], "mark_id": mark["mark_id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "model"
    assert body["model"] == "qwen3:8b"
    # No surface may render a draft without saying what it is.
    assert "not a finding" in body["disclaimer"]
    assert "accept the tracked change" in body["disclaimer"]
