"""
Tests for the surfaces added around the review: the margin reading, the evidence
request, and the local host the Word add-in talks to.

The first test in this file is the one that matters. Every other guarantee in
this system is a design decision that could be revisited; "the software does not
tell anyone a clause is compliant" is the promise the firm makes to a Vendor
Review Committee, and it has to fail a build, not a code review.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cgate import datarequest, reading                      # noqa: E402
from cgate.adjudicate import iter_items, load_checklist     # noqa: E402
from cgate.model import (                                   # noqa: E402
    Actor, Exception_, ItemResult, Modality, Requirement, ReviewResult, SpanRef, Status,
)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKLIST = os.path.join(HERE, "config/checklist.example.yaml")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def make_req(**kw) -> Requirement:
    base = dict(
        req_id="req0001",
        category="R1",
        modality=Modality.OBLIGATION,
        actor=Actor.CONTRACTOR,
        text="The Contractor shall not engage in any corrupt practice.",
        spans=[SpanRef("d" * 64, "p1.c0.b0", 0, 55)],
        clause_id="c1",
        clause_number="6.8",
        page=12,
        checklist_items=[3, 8],
        evidence_quote="The Contractor shall not engage in any corrupt practice.",
        matched=["shall not", "corrupt practice"],
    )
    base.update(kw)
    return Requirement(**base)


def carve_out(resolved: bool) -> Exception_:
    return Exception_(
        marker="except as",
        text="except as otherwise provided in the Special Conditions",
        spans=[SpanRef("d" * 64, "p1.c0.b0", 10, 40)],
        scope_resolved=resolved,
        scope_note="points outside this clause",
        target_ref=None if resolved else "Special Conditions",
    )


def make_result(items, requirements=None) -> ReviewResult:
    return ReviewResult(
        doc_id="a" * 64,
        doc_path=r"C:\tenders\solicitation.pdf",
        checklist_version="1.0",
        tool_version="0.1.0",
        generated_at="2026-08-20T09:00:00",
        requirements=requirements or [],
        items=items,
    )


def item(item_id, status, section="A", handling="automated", never=False) -> ItemResult:
    return ItemResult(
        item_id=item_id, section=section, text=f"Item {item_id}", handling=handling,
        status=status, notes=["reason it is open"], never_auto_clear=never,
    )


# ---------------------------------------------------------------------------
# The line the system is not allowed to cross
# ---------------------------------------------------------------------------

# Verdict words only. "approved" is deliberately absent: an *approved associate*
# is the staff register's own term for a person's employment status, and
# `cgate learn --approve` is a named human approving a lexicon change. Neither
# says anything about a clause. A word list that flags those teaches whoever hits
# the failure to widen the exemption, which is how this guarantee would rot.
FORBIDDEN = ("compliant", "non-compliant", "cleared", "clearance is",
             "passed", "satisfied")

ADDIN = pathlib.Path(__file__).resolve().parent.parent / "addin"

# The task pane is roughly 320px wide and is read by someone with the
# solicitation open beside it. It carried 3,527 characters of visible text and
# read as a wall.
#
# Two budgets, because the wall was made of one of them and not the other. A
# button that says "Remove markup" is an affordance: it costs a reader nothing
# and shortening it to "Remove" makes the pane worse. A paragraph explaining
# what the tool may not conclude is prose, and prose in a sidebar is what
# nobody reads. So the tight ratchet is on prose alone — any visible string of
# 40 characters or more — and the loose one only stops a hundred new labels
# arriving.
#
# Both are ratchets: lower them when copy is cut, never raise one to make a
# change fit. If a change needs more prose than this allows, the question to
# answer first is which existing prose it replaces. Findings and clause quotes
# are rendered at runtime from the document and are not the pane's to budget.
PROSE_MIN = 40
PANE_PROSE_BUDGET = 400
PANE_COPY_BUDGET = 1100


def test_no_surface_ever_says_a_clause_is_compliant():
    """`config/checklist.example.yaml` prohibits issuing a clearance and `_assert_enforceable`
    enforces it for adjudication. This is the same guarantee for the surfaces a
    person actually reads: the margin note, and the evidence request."""
    reqs = [
        make_req(),
        make_req(req_id="req0002", modality=Modality.PROHIBITION, exceptions=[carve_out(False)]),
        make_req(req_id="req0003", modality=Modality.RIGHT_OF_UN, actor=Actor.UN),
        make_req(req_id="req0004", modality=Modality.PERMISSION, triggers=["modality_unresolved"]),
        make_req(req_id="req0005", incorporation={"target": "the GTC", "retrieved": False}),
    ]
    status = {3: Status.UNRESOLVED, 8: Status.EVIDENCE_PRESENT}

    surfaces = [reading.comment_text(r, status) for r in reqs]
    for r in reqs:
        surfaces.extend(str(v) for v in reading.read(r, status).values())

    result = make_result([item(i, Status.UNRESOLVED) for i in range(1, 16)], reqs)
    surfaces.append(datarequest.render(result, load_checklist(CHECKLIST)))

    for text in surfaces:
        low = text.lower()
        for word in FORBIDDEN:
            # Every occurrence, not just the first. The disclaimers themselves
            # contain these words — "this is not a compliance clearance",
            # "never cleared automatically" — so the test has to tell a denial
            # from an assertion rather than stop at the first hit.
            at = low.find(word)
            while at != -1:
                context = low[max(0, at - 45): at + len(word) + 25]
                assert any(neg in context for neg in
                           ("not a", "never", "does not", "no ", "cannot", "prohibit")), \
                    f"surface asserts {word!r}: …{context}…"
                at = low.find(word, at + 1)


def test_no_status_means_clean():
    assert not hasattr(Status, "CLEARED")
    assert not hasattr(Status, "SATISFIED")
    assert "cleared" not in {s.value for s in Status}


# ---------------------------------------------------------------------------
# Exposure bands
# ---------------------------------------------------------------------------


def test_unresolved_carve_out_scope_is_the_sharpest_reason():
    level, why = reading.exposure(make_req(exceptions=[carve_out(False), carve_out(True)]))
    assert level == reading.HIGH
    assert "carve-out" in why.lower()


def test_resolved_carve_out_does_not_escalate():
    level, _ = reading.exposure(make_req(exceptions=[carve_out(True)]), {3: Status.EVIDENCE_PRESENT,
                                                                        8: Status.EVIDENCE_PRESENT})
    assert level == reading.ROUTINE


def test_incorporation_not_retrieved_is_high():
    level, why = reading.exposure(
        make_req(incorporation={"target": "the Anti-Fraud Policy", "retrieved": False}))
    assert level == reading.HIGH
    assert "Anti-Fraud Policy" in why


def test_parse_failure_is_its_own_band_not_a_finding():
    """On a real tender, most provisions carried `modality_unresolved`.
    Filed as HIGH they bury the seven clauses that genuinely incorporate an
    unattached document."""
    level, why = reading.exposure(make_req(triggers=["modality_unresolved"]))
    assert level == reading.UNCLASSIFIED
    assert "limit of the parser" in why
    assert "not a finding about the contract" in why


def test_substantive_trigger_outranks_a_parse_trigger():
    level, _ = reading.exposure(make_req(
        triggers=["modality_unresolved", "conditions_incorporated_by_reference_not_retrieved"]))
    assert level == reading.HIGH


def test_an_unrecognised_trigger_escalates_rather_than_being_ignored():
    """A trigger added to the extractor must not lose its escalation by being
    unknown to this layer."""
    level, why = reading.exposure(make_req(triggers=["some_new_trigger_nobody_mapped"]))
    assert level == reading.HIGH
    assert "does not classify" in why


def test_binds_us_with_the_proving_item_unresolved_is_open():
    level, why = reading.exposure(make_req(), {3: Status.UNRESOLVED, 8: Status.UNRESOLVED})
    assert level == reading.OPEN
    assert "missing" in why


def test_client_right_is_open_not_routine():
    level, _ = reading.exposure(make_req(modality=Modality.RIGHT_OF_UN, actor=Actor.UN))
    assert level == reading.OPEN


def test_binds_line_stays_readable_when_classification_fails():
    both = reading._binds(make_req(modality=Modality.UNKNOWN, actor=Actor.UNKNOWN))
    assert "Unclassified," not in both
    assert "Read it." in both

    no_actor = reading._binds(make_req(actor=Actor.UNKNOWN))
    assert "could not be" in no_actor


def test_every_band_has_a_label_and_a_mark_order():
    assert set(reading.EXPOSURE_LABEL) == set(reading.EXPOSURE_ORDER)
    assert reading.EXPOSURE_ORDER[0] == reading.HIGH


def test_margin_note_answers_the_four_questions_and_disclaims():
    note = reading.comment_text(make_req(), {3: Status.UNRESOLVED})
    for heading in ("BINDS:", "READS AGAINST US:", "HOW THIS WAS FOUND:"):
        assert heading in note
    assert note.startswith("CGATE ")          # how the pane finds its own markup
    assert "a named human decides" in note
    assert "SOP-3.11" in note


def test_reasoning_names_what_it_matched_on():
    """A reviewer cannot disagree with a conclusion whose basis is hidden."""
    found = reading.found(make_req())
    assert "corrupt practice" in found
    assert "not by a language model" in found


def test_manageable_is_empty_rather_than_filler():
    """The line has to earn its place. A reason true of all 95 provisions is
    decoration."""
    assert reading.manageable(make_req(), {3: Status.UNRESOLVED, 8: Status.UNRESOLVED}) == ""
    assert "verbatim" not in reading.manageable(make_req(exceptions=[carve_out(True)]))


# ---------------------------------------------------------------------------
# Evidence request
# ---------------------------------------------------------------------------


def test_every_checklist_item_can_state_what_would_close_it():
    """Six items declare `rule:` or `mechanisms:` and no `evidence:` list. An
    item that appears in the request asking for nothing is worse than one that
    is not listed."""
    checklist = load_checklist(CHECKLIST)
    for _, _, spec in iter_items(checklist):
        if int(spec["id"]) == 12:
            continue                       # decided from the solicitation itself
        keys = datarequest.needed_keys(spec)
        assert keys, f"item {spec['id']} asks for nothing"


def test_request_does_not_re_ask_for_evidence_already_on_file():
    items = [item(1, Status.EVIDENCE_PRESENT), item(2, Status.UNRESOLVED),
             item(12, Status.FLAG), item(6, Status.REQUIRES_HUMAN, section="B", never=True)]
    md = datarequest.render(make_result(items), load_checklist(CHECKLIST))
    outstanding = md.split("## 2.")[0]
    assert "| 2 |" in outstanding
    assert "| 1 |" not in outstanding
    assert "Not being asked for" in md


def test_request_says_what_complete_evidence_will_not_do():
    items = [item(6, Status.UNRESOLVED, section="B", handling="assisted", never=True)]
    md = datarequest.render(make_result(items), load_checklist(CHECKLIST))
    assert "never cleared automatically" in md.lower()
    assert "does not move it to clean" in md


def test_request_carries_the_document_hash():
    md = datarequest.render(make_result([item(1, Status.UNRESOLVED)]), load_checklist(CHECKLIST))
    assert "a" * 64 in md


def test_skeleton_is_valid_json_and_keyed_to_outstanding_items_only():
    items = [item(1, Status.UNRESOLVED), item(3, Status.EVIDENCE_PRESENT),
             item(5, Status.UNRESOLVED)]
    md = datarequest.render(make_result(items), load_checklist(CHECKLIST))
    skel = json.loads(md.split("```json")[1].split("```")[0])
    assert set(skel) == {"1", "5"}
    assert "triggers" in skel["1"]


def test_holders_are_named_and_not_duplicated():
    checklist = load_checklist(CHECKLIST)
    specs = {int(i["id"]): i for _, _, i in iter_items(checklist)}
    for item_id in (1, 3, 6, 8, 10, 13, 14):
        holders = datarequest._holders(specs[item_id])
        assert holders, f"item {item_id} has no named holder"
        assert len(holders) == len(set(holders))
        assert all(h.strip() for h in holders)
    assert datarequest._holders(specs[10]) == ["The BD Manager"]


# ---------------------------------------------------------------------------
# The local host
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from cgate.serve import build_app

    app = build_app(CHECKLIST, str(tmp_path / "store"), str(tmp_path / "reviews"))
    return TestClient(app)


def test_health_reports_the_checklist_in_force(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["checklist"].replace("\\", "/").endswith("config/checklist.example.yaml")


def test_a_document_id_in_a_url_cannot_walk_the_filesystem(client):
    for bad in ("../../etc", "..%2f..%2fetc", "a/b"):
        assert client.get(f"/api/review/{bad}/md").status_code in (400, 404)


def test_unreadable_filetype_is_refused_with_the_reason(client):
    r = client.post("/api/review", files={"files": ("scan.png", b"\x89PNG", "image/png")})
    assert r.status_code == 415
    assert "OCR" in r.json()["detail"]


def test_a_decision_without_a_name_is_not_recorded(client):
    r = client.post("/api/feedback", json={"kind": "confirm", "req_id": "req0001",
                                           "doc_id": "a" * 64, "reviewer": "  "})
    assert r.status_code == 400
    assert "cannot be audited" in r.json()["detail"]


def test_feedback_needs_a_review_on_file(client):
    r = client.post("/api/feedback", json={"kind": "confirm", "req_id": "req0001",
                                           "doc_id": "b" * 64, "reviewer": "A. Reviewer"})
    assert r.status_code == 404


def test_review_and_then_a_decision_on_one_of_its_provisions(client, tmp_path):
    """The whole loop the add-in drives: a document in, a provision confirmed,
    the calibration figure that section 5 of the report needs."""
    src = os.path.join(HERE, "bench", "fixtures", "gc_integrity.txt")
    assert os.path.exists(src), "run `python examples/make_sample.py` first"

    with open(src, "rb") as fh:
        r = client.post("/api/review", files={"files": ("gtc.txt", fh, "text/plain")})
    assert r.status_code == 200
    payload = r.json()

    assert len(payload["items"]) == 15
    assert {i["status"] for i in payload["items"]} <= {
        "evidence_present", "flag", "unresolved", "not_applicable", "requires_human"}
    assert payload["provisions"], "the integrity fixture should yield provisions"
    for p in payload["provisions"]:
        assert p["exposure"] in reading.EXPOSURE_ORDER
        assert p["binds"] and p["against"] and p["found"]
        assert p["quote"]

    for kind in ("md", "json", "request"):
        assert client.get(f"/api/review/{payload['doc_id']}/{kind}").status_code == 200

    req_id = payload["provisions"][0]["req_id"]
    d = client.post("/api/feedback", json={
        "doc_id": payload["doc_id"], "req_id": req_id,
        "kind": "confirm", "reviewer": "A. Reviewer", "reason": "correct on its face",
    })
    assert d.status_code == 200
    assert d.json()["recorded"] is True
    assert d.json()["calibration"], "a recorded decision must produce a calibration figure"


def test_classify_is_the_third_decision_and_carries_a_quote_hash(client, tmp_path):
    """PLAN §6.6: the reviewer names the category of a provision the extractor
    could not classify. It reaches the ledger like any other decision — never
    re-banded client-side — and (PLAN §11) every decision carries the hash of
    the quote it was made about."""
    src = os.path.join(HERE, "bench", "fixtures", "gc_integrity.txt")
    assert os.path.exists(src), "run `python examples/make_sample.py` first"

    with open(src, "rb") as fh:
        payload = client.post("/api/review", files={"files": ("gtc.txt", fh, "text/plain")}).json()
    req_id = payload["provisions"][0]["req_id"]

    r = client.post("/api/feedback", json={
        "doc_id": payload["doc_id"], "req_id": req_id,
        "kind": "classify", "category": "R4", "reviewer": "A. Reviewer",
    })
    assert r.status_code == 200
    assert r.json()["recorded"] is True

    r2 = client.post("/api/feedback", json={
        "doc_id": payload["doc_id"], "req_id": req_id,
        "kind": "classify", "reviewer": "A. Reviewer",
    })
    assert r2.status_code == 400
    assert "category" in r2.json()["detail"]

    from cgate import feedback as fb
    ledger = fb.load_ledger(str(tmp_path / "store"))
    rows = [row for row in ledger if row["kind"] == "classify"]
    assert len(rows) == 1
    assert rows[0]["category"] == "R4"
    assert rows[0]["req_id"] == req_id
    assert len(rows[0]["quote_hash"]) == 64  # sha256 hex

    calib = fb.calibration(ledger)
    assert calib["R4"].confirmed == 1  # a classification counts as an affirming decision


def test_decisions_endpoint_is_registered_before_the_kind_catch_all(client):
    """`GET /api/review/{doc_id}/decisions` must not be swallowed by the
    generic `/api/review/{doc_id}/{kind}` artifact route — see the comment on
    the decisions() handler in serve.py about registration order."""
    r = client.get("/api/review/" + ("a" * 32) + "/decisions")
    assert r.status_code == 200
    assert r.json() == {"decisions": []}


def test_decisions_endpoint_returns_the_last_decision_per_provision(client, tmp_path):
    src = os.path.join(HERE, "bench", "fixtures", "gc_integrity.txt")
    assert os.path.exists(src), "run `python examples/make_sample.py` first"

    with open(src, "rb") as fh:
        payload = client.post("/api/review", files={"files": ("gtc.txt", fh, "text/plain")}).json()
    req_id = payload["provisions"][0]["req_id"]

    client.post("/api/feedback", json={
        "doc_id": payload["doc_id"], "req_id": req_id,
        "kind": "confirm", "reviewer": "A. Reviewer",
    })
    client.post("/api/feedback", json={
        "doc_id": payload["doc_id"], "req_id": req_id,
        "kind": "reject", "reviewer": "A. Reviewer", "reason": "changed my mind on review",
    })

    r = client.get(f"/api/review/{payload['doc_id']}/decisions")
    assert r.status_code == 200
    rows = r.json()["decisions"]
    assert len(rows) == 1  # one entry per req_id — the later one wins
    assert rows[0]["kind"] == "reject"
    assert rows[0]["req_id"] == req_id


def test_items_6_7_9_are_never_reported_as_clean(client):
    """Design rule of the checklist. Whatever the evidence, these three arrive at
    the Compliance Officer."""
    src = os.path.join(HERE, "bench", "fixtures", "gc_integrity.txt")
    assert os.path.exists(src), "run `python examples/make_sample.py` first"
    with open(src, "rb") as fh:
        payload = client.post("/api/review", files={"files": ("gtc.txt", fh, "text/plain")}).json()
    for it in payload["items"]:
        if it["item_id"] in (6, 7, 9):
            assert it["status"] != "evidence_present"


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


def test_the_shipped_manifest_is_one_word_will_load():
    from cgate import sideload

    problems = sideload.validate(sideload.manifest_path())
    assert problems == [], "Word would ignore this manifest and report nothing: " + "; ".join(problems)


def test_a_double_hyphen_in_a_comment_is_caught(tmp_path):
    """This build shipped with `--port` inside an XML comment. That is illegal,
    it made the manifest unparseable, and Word's response was to show no button
    and no error — indistinguishable from never having installed it."""
    from cgate import sideload

    bad = tmp_path / "manifest.xml"
    bad.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!-- change the port with cgate serve --port -->\n'
        '<OfficeApp xmlns="http://schemas.microsoft.com/office/appforoffice/1.1"/>\n',
        encoding="utf-8",
    )
    problems = sideload.validate(str(bad))
    assert problems and "well-formed" in problems[0]


def test_an_empty_appdomains_element_is_caught(tmp_path):
    from cgate import sideload

    bad = tmp_path / "manifest.xml"
    bad.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OfficeApp xmlns="http://schemas.microsoft.com/office/appforoffice/1.1">'
        '<AppDomains /></OfficeApp>\n',
        encoding="utf-8",
    )
    assert any("AppDomains" in p for p in sideload.validate(str(bad)))


def test_a_control_pointing_at_a_missing_resource_is_caught(tmp_path):
    """A resid with no matching resource does not fail loudly either: the ribbon
    drops the control that referenced it."""
    from cgate import sideload

    bad = tmp_path / "manifest.xml"
    bad.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OfficeApp xmlns="http://schemas.microsoft.com/office/appforoffice/1.1">'
        '<Label resid="Button.Nonexistent" /></OfficeApp>\n',
        encoding="utf-8",
    )
    assert any("Button.Nonexistent" in p for p in sideload.validate(str(bad)))


# ---------------------------------------------------------------------------
# The pane's own copy
# ---------------------------------------------------------------------------

def _pane_visible_strings() -> list:
    """The strings a reviewer actually reads in the task pane, one per element.

    Comments, <script> and <style> are stripped, because none of them reach the
    eye. What is left is chrome plus labels -- the text the pane asserts on its
    own authority, as opposed to the findings and clause quotes it renders at
    runtime from the document, which are not the pane's to budget.

    A <details> body is progressive disclosure, which is the remedy here and not
    the disease: PLAN 0.4 requires the reasoning to stay reachable and 0
    requires the pane to stop lecturing. Counting disclosed text would make the
    budget punish the one move that satisfies both. The <summary> is counted,
    because that is what the reader sees at rest.
    """
    import re

    src = (ADDIN / "taskpane.html").read_text(encoding="utf-8")
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", src, flags=re.S | re.I)
    src = re.sub(r"<details[^>]*>(.*?)</details>",
                 lambda m: re.sub(r"</summary>.*", "</summary>", m.group(1), flags=re.S),
                 src, flags=re.S | re.I)

    return [t.strip() for t in re.findall(r">([^<>]+)<", src) if t.strip()]


def _pane_visible_text() -> str:
    """The same strings as one blob, for the total budget and the word scan."""
    return " ".join(_pane_visible_strings())



def test_the_pane_never_says_a_clause_is_compliant():
    """`test_no_surface_ever_says_a_clause_is_compliant` guards the margin note
    and the evidence request — the surfaces rendered in Python. The pane's own
    HTML was never scanned, so the guarantee stopped at the language boundary.
    It does not any more.
    """
    text = _pane_visible_text().lower()
    for word in FORBIDDEN:
        assert word not in text, (
            f"addin/taskpane.html says {word!r}. The pane may report exposure — what a "
            f"clause binds the firm to, and whether anything on file shows the firm can "
            f"meet it — and may never report a verdict."
        )


def test_the_pane_copy_stays_within_its_budget():
    """The pane explains itself in a 320px sidebar, and explanation grows the way
    all documentation grows. A reviewer with minutes needs the findings, not an
    essay about the design rules; they are in the repository
    and the disclaimer is in every margin comment the tool writes.

    Prose is budgeted hard and labels are not, because the wall was prose.
    """
    prose = [s for s in _pane_visible_strings() if len(s) >= PROSE_MIN]
    prose_chars = sum(len(s) for s in prose)
    listed = "\n  ".join(prose)

    assert prose_chars <= PANE_PROSE_BUDGET, (
        f"the pane carries {prose_chars} characters of prose, over the "
        f"{PANE_PROSE_BUDGET} budget:\n  {listed}\n"
        "Move it behind a <details> rather than deleting it: PLAN \u00a70.4 requires "
        "every mark to carry how it was reached."
    )

    chars = len(_pane_visible_text())
    assert chars <= PANE_COPY_BUDGET, (
        f"the pane carries {chars} characters of visible text, over the "
        f"{PANE_COPY_BUDGET} budget."
    )


def test_the_pane_uses_no_browser_modals():
    """`window.confirm`, `window.alert` and `window.prompt` are the one thing in
    an Office task pane that cannot be mistaken for Word. They steal focus, they
    cannot show the clause they are asking about, their buttons read OK and
    Cancel whatever the question was, and some Office hosts suppress them
    outright -- which turns a refusal to proceed into a silent success. There
    were seven. `ask()` in taskpane.js replaced them.
    """
    import re

    for name in ("taskpane.js", "marks.js", "anchor.js"):
        src = (ADDIN / name).read_text(encoding="utf-8")
        # Comments explain why they are gone; only real calls count.
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"(?m)^\s*//.*$", "", code)
        for call in ("window.confirm", "window.alert", "window.prompt"):
            assert call not in code, (
                f"addin/{name} calls {call}. Use ask() instead: it renders the question "
                f"where the answer belongs and resolves to null when the reviewer backs out."
            )


def test_the_governance_note_is_one_sentence_everywhere():
    """A margin comment pointing a reviewer at a document number they cannot
    find is worse than none. The tool cites the review-process notes by title,
    from one Python constant; the two JavaScript comment builders cannot import
    it, so they must carry the same sentence verbatim — a reviewer reading a
    comment in Word and a footer in review.md are sent to the same place."""
    from cgate import GOVERNANCE_REF
    sentence = f"Governed by {GOVERNANCE_REF}."
    assert sentence in reading.comment_text(make_req(), {3: Status.UNRESOLVED})
    for name in ("taskpane.js", "marks.js"):
        assert sentence in (ADDIN / name).read_text(encoding="utf-8"), name
    for name in ("taskpane.js", "marks.js", "taskpane.html", "manifest.xml"):
        assert "3.13" not in (ADDIN / name).read_text(encoding="utf-8"), name
