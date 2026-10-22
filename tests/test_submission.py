"""
Tests for cgate.submission — the examiner's marks on the firm's own document.

The invariant most of this file exists to defend: **a commendation is earned by
a check that ran, never produced by a check that did not.** A green mark that
means "nothing was looked at here" is the most dangerous output this system
could emit, because a reviewer trusts the margin more than the report.
"""

from __future__ import annotations

import docx
import pytest

from cgate import submission as sub
from cgate.submission import COMMENDATION, CORRECTION, QUERY


def make_submission(path, paragraphs, *, author=None):
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if author:
        d.core_properties.author = author
    d.save(str(path))
    return str(path)


ORDINARY = (
    "Our approach to the topographic survey follows established practice for "
    "works of this scale and is described in the sections that follow."
)

REGISTER = [
    {"name": "Alex Rowan", "status": "active", "role": "Team Leader"},
    {"name": "Jordan Pell", "status": "active", "role": "Senior Engineer"},
    {"name": "Morgan Hale", "status": "resigned", "role": "Engineer"},
]


# ---------------------------------------------------------------------------
# The central rule
# ---------------------------------------------------------------------------


def test_no_sources_means_no_commendations(tmp_path):
    """A thin run marks claims and nothing else. It must not congratulate."""
    p = make_submission(tmp_path / "s.docx", [ORDINARY, "We will mobilise in June."])
    r = sub.review_submission(p)
    assert r.by_kind(COMMENDATION) == []
    assert len(r.rubric.checks_skipped) == 4
    assert "never made is not a passage that was found sound" in r.notes[0]


def test_every_skipped_check_names_what_it_needed(tmp_path):
    p = make_submission(tmp_path / "s.docx", [ORDINARY])
    r = sub.review_submission(p)
    for skipped in r.rubric.checks_skipped:
        assert skipped.needs
        assert skipped.consequence


def test_a_clean_paragraph_gets_no_mark_at_all(tmp_path):
    """Silence is the correct output for an unexamined ordinary paragraph."""
    p = make_submission(tmp_path / "s.docx", [ORDINARY])
    r = sub.review_submission(p)
    assert r.marks == []


# ---------------------------------------------------------------------------
# Personnel
# ---------------------------------------------------------------------------


def test_no_register_produces_no_personnel_faults(tmp_path):
    """Absence of a source must not manufacture a fault list."""
    p = make_submission(tmp_path / "s.docx", [
        "Key personnel: Alex Rowan, Team Leader, and Jordan Pell, Senior Engineer.",
    ])
    r = sub.review_submission(p)
    assert [m for m in r.marks if m.rule == "personnel_register"] == []
    skipped = [s for s in r.rubric.checks_skipped if s.rule == "personnel_register"]
    assert skipped and "name(s) were detected" in skipped[0].consequence


def test_a_person_on_the_register_is_commended(tmp_path):
    p = make_submission(tmp_path / "s.docx", [
        "Key personnel: Alex Rowan, Team Leader, leads the survey.",
    ])
    r = sub.review_submission(p, staff_register=REGISTER)
    good = [m for m in r.by_kind(COMMENDATION) if m.rule == "personnel_register"]
    assert len(good) == 1
    assert "Alex Rowan" in good[0].heading
    assert "Team Leader" in good[0].detail
    assert "staff register" in good[0].evidence
    assert r.rubric.personnel_verified == ["Alex Rowan"]


def test_a_person_not_on_the_register_is_corrected(tmp_path):
    p = make_submission(tmp_path / "s.docx", [
        "Key personnel: Riley Stone, Senior Engineer, will lead the works.",
    ])
    r = sub.review_submission(p, staff_register=REGISTER)
    bad = [m for m in r.by_kind(CORRECTION) if m.rule == "personnel_register"]
    assert len(bad) == 1
    assert "person_not_in_register" in bad[0].triggers
    assert 13 in bad[0].checklist_items and 14 in bad[0].checklist_items


def test_an_inactive_person_is_corrected_differently(tmp_path):
    p = make_submission(tmp_path / "s.docx", [
        "Key personnel: Morgan Hale, Engineer, is proposed for this assignment.",
    ])
    r = sub.review_submission(p, staff_register=REGISTER)
    bad = [m for m in r.by_kind(CORRECTION) if m.rule == "personnel_register"]
    assert "status_inactive" in bad[0].triggers
    assert "resigned" in bad[0].detail


def test_names_outside_a_personnel_context_are_left_alone(tmp_path):
    """Conservative detection. A missed name is visible; an invented one is not."""
    p = make_submission(tmp_path / "s.docx", [
        "The works are located in Exampleton and Sampleville, near the Ministry building.",
    ])
    r = sub.review_submission(p, staff_register=REGISTER)
    assert [m for m in r.marks if m.rule == "personnel_register"] == []


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,rule", [
    ("We are the leading consulting firm in the region.", "claim_superlative"),
    ("Our projects are always delivered on time.", "claim_absolute"),
    ("We have completed more than 400 projects.", "claim_unsourced_quantity"),
])
def test_claims_are_queried_not_corrected(tmp_path, text, rule):
    """A claim is not a fault. It is a thing a reviewer asks you to support."""
    p = make_submission(tmp_path / "s.docx", [text])
    r = sub.review_submission(p)
    marks = [m for m in r.marks if m.rule == rule]
    assert marks, f"{rule} did not fire on {text!r}"
    assert marks[0].kind is QUERY or marks[0].kind == QUERY
    assert 15 in marks[0].checklist_items
    assert "not a finding that the claim is untrue" in marks[0].evidence


def test_a_claim_mark_quotes_the_sentence_not_just_the_phrase(tmp_path):
    p = make_submission(tmp_path / "s.docx", [
        "The firm was founded in 1967. We are the leading engineers in Exampleland. "
        "Our offices are in Sampleville."
    ])
    r = sub.review_submission(p)
    mark = [m for m in r.marks if m.rule == "claim_superlative"][0]
    assert "leading engineers" in mark.quote
    assert "founded in 1967" not in mark.quote


def test_a_certification_with_its_reference_is_not_queried(tmp_path):
    p = make_submission(tmp_path / "s.docx", [
        "The firm is certified to ISO 9001, certificate number XX-2024-118.",
    ])
    r = sub.review_submission(p)
    assert [m for m in r.marks if m.rule == "claim_certification"] == []


# ---------------------------------------------------------------------------
# Shared content
# ---------------------------------------------------------------------------


def test_shared_passage_is_the_sharpest_correction(tmp_path):
    shared = ("The consultant shall mobilise a topographic survey team within "
              "fourteen days of the commencement date and shall maintain that team "
              "on site for the duration of the survey works.")
    ours = make_submission(tmp_path / "ours.docx", [shared, ORDINARY])
    theirs = make_submission(tmp_path / "theirs.docx", [shared, "Their own text."])

    r = sub.review_submission(str(ours), third_party_material=[str(theirs)])
    bad = [m for m in r.by_kind(CORRECTION) if m.rule == "shared_content"]
    assert bad
    assert "supplied to us while the bid was being prepared" in bad[0].detail
    assert set(bad[0].checklist_items) >= {6, 15}
    assert r.rubric.shared_passages >= 1


def test_shared_author_is_reported_even_with_no_shared_text(tmp_path):
    ours = make_submission(tmp_path / "ours.docx", ["Entirely our own words here."],
                           author="S. Quill")
    theirs = make_submission(tmp_path / "theirs.docx", ["Completely different text."],
                             author="S. Quill")
    r = sub.review_submission(str(ours), third_party_material=[str(theirs)])
    bad = [m for m in r.marks if m.rule == "shared_origin"]
    assert bad and "metadata_origin_match" in bad[0].triggers


def test_no_third_party_material_records_the_skip(tmp_path):
    p = make_submission(tmp_path / "s.docx", [ORDINARY])
    r = sub.review_submission(p)
    skipped = [s for s in r.rubric.checks_skipped if s.rule == "shared_content"]
    assert skipped and "independent bidding" in skipped[0].consequence


# ---------------------------------------------------------------------------
# Undeclared parties
# ---------------------------------------------------------------------------


def test_an_undeclared_firm_produces_the_item_11_trigger(tmp_path):
    """`undeclared_party_in_submission` is named in config/checklist.example.yaml and had no producer."""
    p = make_submission(tmp_path / "s.docx", [
        "The survey will be carried out by Northern Survey Contractors LLC "
        "under our supervision.",
    ])
    r = sub.review_submission(p, declared_parties=["Some Other Firm LLC"])
    bad = [m for m in r.marks if m.rule == "undeclared_party"]
    assert bad
    assert "undeclared_party_in_submission" in bad[0].triggers
    assert bad[0].checklist_items == [11]
    assert "undeclared_party_in_submission" in r.rubric.triggers


def test_a_declared_firm_is_not_flagged(tmp_path):
    p = make_submission(tmp_path / "s.docx", [
        "The survey will be carried out by Northern Survey Contractors LLC.",
    ])
    r = sub.review_submission(
        p, declared_parties=["NORTHERN SURVEY CONTRACTORS S.A.R.L."])
    assert [m for m in r.marks if m.rule == "undeclared_party"] == []


# ---------------------------------------------------------------------------
# Coverage and the rubric
# ---------------------------------------------------------------------------


def _requirement(text, clause="6.8", page=11):
    from cgate.model import Actor, Modality, Requirement
    return Requirement(
        req_id="r1", category="R5", modality=Modality.OBLIGATION,
        actor=Actor.CONTRACTOR, text=text, spans=[], clause_id="c1",
        clause_number=clause, page=page,
    )


def test_an_answered_requirement_is_commended_with_its_limit_stated(tmp_path):
    req = _requirement(
        "The Bidder shall submit a detailed quality assurance methodology "
        "covering inspection, testing and non-conformance reporting.")
    p = make_submission(tmp_path / "s.docx", [
        "Our quality assurance methodology covers inspection, testing and "
        "non-conformance reporting at every stage of the detailed design.",
    ])
    r = sub.review_submission(p, solicitation_requirements=[req])
    good = [m for m in r.by_kind(COMMENDATION) if m.rule == "requirement_coverage"]
    assert good
    assert "6.8" in good[0].heading
    # The limit of what term overlap can prove must be stated on the mark itself.
    assert "not that the answer is sufficient" in good[0].evidence


def test_an_unanswered_requirement_is_on_the_rubric_and_never_in_the_margin(tmp_path):
    """An omission has no span. Anchoring one would be a fabrication."""
    req = _requirement(
        "The Bidder shall submit a detailed decommissioning and site "
        "reinstatement plan including waste disposal routes.")
    p = make_submission(tmp_path / "s.docx", [ORDINARY])
    r = sub.review_submission(p, solicitation_requirements=[req])

    assert r.rubric.mandatory_unaddressed
    assert r.rubric.mandatory_unaddressed[0]["clause"] == "6.8"
    assert [m for m in r.marks if m.rule == "requirement_coverage"] == []
    assert "mandatory_requirement_unaddressed" in r.rubric.triggers


def test_only_deliverable_obligations_are_tested_for_coverage(tmp_path):
    """"shall indemnify" is an obligation but nothing a proposal answers."""
    from cgate.model import Actor, Modality, Requirement
    indemnity = Requirement(
        req_id="r2", category="R7", modality=Modality.OBLIGATION,
        actor=Actor.CONTRACTOR,
        text="The Contractor shall indemnify and hold harmless the Organisation.",
        spans=[], clause_id="c2", clause_number="9.1", page=14,
    )
    p = make_submission(tmp_path / "s.docx", [ORDINARY])
    r = sub.review_submission(p, solicitation_requirements=[indemnity])
    assert r.rubric.mandatory_total == 0


def test_a_requirement_too_vague_to_test_is_declared_untestable(tmp_path):
    req = _requirement("The Bidder shall submit the Form.")
    p = make_submission(tmp_path / "s.docx", [ORDINARY])
    r = sub.review_submission(p, solicitation_requirements=[req])
    entry = r.rubric.mandatory_unaddressed[0]
    assert entry["testable"] is False
    assert "by hand" in entry["why"]


def test_the_rubric_carries_no_score(tmp_path):
    """No mark out of ten. An aggregate would hide the one thing that matters."""
    p = make_submission(tmp_path / "s.docx", [ORDINARY])
    d = sub.review_submission(p).rubric.to_dict()
    for forbidden in ("score", "grade", "percentage", "rating", "confidence"):
        assert not any(forbidden in k.lower() for k in d), forbidden


# ---------------------------------------------------------------------------
# Ordering and shape
# ---------------------------------------------------------------------------


def test_corrections_come_before_queries_before_commendations(tmp_path):
    p = make_submission(tmp_path / "s.docx", [
        "Key personnel: Alex Rowan, Team Leader.",
        "We are the leading engineers in the region.",
        "Key personnel also include Riley Stone, Engineer.",
    ])
    r = sub.review_submission(p, staff_register=REGISTER)
    kinds = [m.kind for m in r.marks]
    assert kinds == sorted(kinds, key=lambda k: sub.MARK_ORDER.index(k))


def test_every_mark_carries_its_reasoning(tmp_path):
    p = make_submission(tmp_path / "s.docx", [
        "Key personnel: Riley Stone, Engineer. We are the leading firm.",
    ])
    r = sub.review_submission(p, staff_register=REGISTER)
    assert r.marks
    for m in r.marks:
        assert m.evidence.strip(), f"{m.mark_id} has no evidence"
        assert m.detail.strip()
        assert m.heading.strip()


def test_marks_serialise_with_a_label_and_a_glyph(tmp_path):
    p = make_submission(tmp_path / "s.docx", ["We are the best in the market."])
    d = sub.review_submission(p).to_dict()
    assert d["counts"] == {"correction": 0, "query": 1, "commendation": 0}
    assert d["marks"][0]["glyph"] == "?"
    assert d["marks"][0]["label"] == "Query"


def test_an_unreadable_submission_marks_nothing_and_says_so(tmp_path):
    bad = tmp_path / "broken.docx"
    bad.write_bytes(b"not a document at all")
    r = sub.review_submission(str(bad))
    assert r.marks == []
    assert r.notes and "could not be read" in r.notes[0]


# ---------------------------------------------------------------------------
# Firm detection — the false positives that made the check unusable
# ---------------------------------------------------------------------------


def test_our_own_letterhead_is_not_an_undeclared_counterparty(tmp_path):
    """A proposal names its author constantly, often partially."""
    p = make_submission(tmp_path / "s.docx", [
        "Example Engineering Ltd, Sampleville.",
        "Prepared by Example Engineering Ltd.",
    ])
    r = sub.review_submission(
        p, declared_parties=["Someone Else LLC"],
        firm_names=["Example Engineering Ltd"])
    assert [m for m in r.marks if m.rule == "undeclared_party"] == []


@pytest.mark.parametrize("title", [
    "Resident Engineer", "Senior Engineer", "Team Leader",
    "Project Manager", "Design Consultants",
])
def test_a_role_title_is_not_a_firm(tmp_path, title):
    p = make_submission(tmp_path / "s.docx", [
        f"Riley Stone, {title}, is responsible for supervision on site.",
    ])
    r = sub.review_submission(p, declared_parties=["Anything LLC"])
    names = [m.heading for m in r.marks if m.rule == "undeclared_party"]
    assert names == [], names


def test_a_weak_suffix_needs_two_words_in_front(tmp_path):
    """"Northern Survey Contractors" is a firm; "our contractors" is not."""
    from cgate.submission import _candidate_firms
    assert _candidate_firms("engaged Northern Survey Contractors for the works")
    assert not _candidate_firms("we will instruct our contractors accordingly")


def test_a_strong_suffix_needs_only_one(tmp_path):
    from cgate.submission import _candidate_firms
    found = [n for n, _, _ in _candidate_firms("testing by Granite Ltd on site")]
    assert "Granite Ltd" in found


def test_a_declared_party_named_more_fully_is_still_declared(tmp_path):
    p = make_submission(tmp_path / "s.docx", [
        "The survey is by Northern Survey Contractors LLC of Sampleville.",
    ])
    r = sub.review_submission(p, declared_parties=["Northern Survey Contractors"])
    assert [m for m in r.marks if m.rule == "undeclared_party"] == []


def test_a_genuinely_undeclared_firm_still_reports(tmp_path):
    """The fixes must not have made the check silent."""
    p = make_submission(tmp_path / "s.docx", [
        "Laboratory testing will be undertaken by Granite Materials Testing Ltd.",
    ])
    r = sub.review_submission(
        p, declared_parties=["Northern Survey Contractors LLC"],
        firm_names=["Example Engineering Ltd"])
    names = [m.heading for m in r.marks if m.rule == "undeclared_party"]
    assert names == ["Granite Materials Testing Ltd is not declared"]


def test_a_docx_mark_is_located_by_paragraph_not_a_fake_page(tmp_path):
    """A .docx has no pages until Word renders it. "p.1" on every mark lies."""
    p = make_submission(tmp_path / "s.docx", [
        "Opening paragraph of no interest.",
        "Second paragraph, also unremarkable.",
        "We are the leading engineers in the region.",
    ])
    r = sub.review_submission(p)
    mark = [m for m in r.marks if m.rule == "claim_superlative"][0]
    assert mark.locator.startswith("\u00b6")
    assert "p." not in mark.locator
