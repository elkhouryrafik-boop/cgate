"""Regressions found by running cgate over real public procurement notices.

Every test here is a false positive that the public-tender evaluation
(`bench/public/evaluate.py`, `docs/public-tender-evaluation.md`) surfaced on a
document nobody on this project wrote. On the 134-notice corpus they accounted
for 36 of 38 findings. Each one is written from the shape that produced it, so a
later "simplification" of the subject or section logic fails here instead of
quietly turning the tool back into a noise generator.

The common thread: cgate's checks assume a document that declares its own
structure. A procurement notice is a summary. These tests pin down what the
tool must do when the assumption does not hold — decline, not guess.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cgate.lint import lint
from cgate.spec import compile_spec
from tests.test_spec import doc_of

from tests.test_lint import defects_of


# ---------------------------------------------------------------------------
# 1. Unit vocabulary is not a subject
# ---------------------------------------------------------------------------


def test_two_amounts_in_different_currencies_are_not_one_quantity():
    """USD 100 and MZN 4,000,000 share `usd/dollar/thousand/unit` and nothing else.

    Before the unit-noise fix this pair was linked on currency vocabulary alone
    and reported as "2 different values for one quantity (dollar, hundr, meticai,
    mzn, stat, thousand, unit)" — a subject made entirely of units, which is
    evidence the figures are *not* the same quantity.
    """
    found = defects_of(
        ("body", "A bid security of USD 100 shall accompany the proposal."),
        ("body", "A proposal security of MZN 4,000,000 is required."),
        check="value_conflict",
    )
    assert found == []


def test_a_real_quantity_conflict_still_fires():
    """The fix must not mute the check. Turnover, stated twice, differently."""
    found = defects_of(
        ("heading", "4.3 Financial Capacity"),
        ("body", "A bidder must demonstrate a minimum average annual turnover "
                 "of USD 750,000 over the last three audited financial years."),
        ("heading", "5. Mandatory Submission Requirements"),
        ("body", "(h) Audited statements demonstrating average annual turnover "
                 "of not less than USD 900,000;"),
        check="value_conflict",
    )
    assert len(found) == 1
    assert "turnover" in found[0].title


def test_unit_words_are_stripped_from_a_subject_but_time_units_are_kept():
    text = "Minimum average annual turnover of USD 750,000."
    subs = [m for m in compile_spec(doc_of(("body", text))).measurands
            if "750" in (m.raw or "")]
    assert subs, "the amount should still be extracted"
    subject = subs[0].subject
    assert "turnover" in subject
    assert "dollar" not in subject and "usd" not in subject

    # Time units are the *unit* of a duration, not part of its subject, and they
    # are deliberately not in the noise set: 90 days and 120 days really are two
    # answers to one question.
    d2 = doc_of(("body", "Bid validity of 90 days from the deadline."))
    dur = [m for m in compile_spec(d2).measurands if m.kind == "duration"]
    assert dur and dur[0].unit == "day"
    assert "valid" in dur[0].subject


# ---------------------------------------------------------------------------
# 2. Clock times are not durations
# ---------------------------------------------------------------------------


def test_a_deadline_time_is_not_a_count_of_hours():
    """`1600 hours (local time)` is 4pm, not a quantity of 1600 hours.

    Two deadlines at different times of day were reported as a contradiction.
    """
    found = defects_of(
        ("body", "Submissions are due no later than 30 October 2026 at 1600 hours "
                 "(local time)."),
        ("body", "Submissions are due no later than 9 November 2026 at 1000 hours "
                 "(local time)."),
        check="value_conflict",
    )
    assert found == []


def test_office_hours_are_not_a_count_of_hours():
    d = doc_of(("body", "Information is available from Monday to Friday, 0800 "
                        "hours to 1630 hours."))
    hours = [m for m in compile_spec(d).measurands if "hour" in str(m.unit).lower()]
    assert hours == [], "office hours should not be read as quantities"


def test_a_real_duration_in_hours_is_still_counted():
    d = doc_of(("body", "The assignment requires 1600 hours of expert input."))
    hours = [m for m in compile_spec(d).measurands if "hour" in str(m.unit).lower()]
    assert hours, "a genuine hour count must survive the clock-time guard"


# ---------------------------------------------------------------------------
# 3. Placement checks decline on a document that labels nothing
# ---------------------------------------------------------------------------


def test_a_bid_ending_rule_in_an_unlabelled_document_is_not_a_finding():
    """Every paragraph of a notice is "front matter", so placement means nothing.

    The check must report that it did not run, not raise a high-severity finding
    per paragraph.
    """
    found = defects_of(
        ("body", "Late Proposals will be rejected."),
        ("body", "Proposals received after the deadline will not be considered."),
        check="disqualifying_rule_off_anchor",
    )
    assert len(found) == 1
    assert "did not run" in found[0].title.lower()
    assert found[0].quotes == []


def test_a_placement_section_must_be_a_place_the_document_declares():
    """An unrecognised heading is not a place. "other" and "front" are not places."""
    # A heading the tool cannot classify produces kind "other", not a place.
    found = defects_of(
        ("heading", "GENERAL INFORMATION FOR INTERESTED FIRMS"),
        ("body", "Late Proposals will be rejected."),
        check="disqualifying_rule_off_anchor",
    )
    assert all("did not run" in d.title.lower() for d in found)


def test_a_title_heading_is_the_document_not_a_section():
    """A notice headed "(CONSULTING SERVICES)" is classified `scope` by its own title.

    Unnumbered and covering the whole text, it is the document's wrapper — a rule
    under it cannot be "filed away from" anything.
    """
    found = defects_of(
        ("heading", "REQUEST FOR EXPRESSIONS OF INTEREST - CONSULTING SERVICES"),
        ("body", "Key Experts will not be evaluated at the shortlisting stage."),
        check="disqualifying_rule_off_anchor",
    )
    assert all("did not run" in d.title.lower() for d in found)


def test_the_numbered_evaluation_section_still_fires():
    """The gate must not silence a genuine misfiling in a structured document."""
    found = defects_of(
        ("heading", "6.4 Financial Evaluation"),
        ("body", "Proposals quoted in another currency, or exclusive of taxes, "
                 "will be treated as non-responsive."),
        check="disqualifying_rule_off_anchor",
    )
    assert len(found) == 1
    assert "did not run" not in found[0].title.lower()


# ---------------------------------------------------------------------------
# 4. Redundancy checks decline on a document that states everything once
# ---------------------------------------------------------------------------


def test_a_bid_security_in_an_unlabelled_notice_is_not_a_single_point_obligation():
    """A notice is *supposed* to state each requirement once."""
    found = defects_of(
        ("body", "All Bids must be accompanied by a Bid Security of USD350,000."),
        check="single_point_obligation",
    )
    assert all("did not run" in d.title.lower() for d in found)


def test_a_declined_check_never_counts_as_a_finding():
    """A "did not run" defect must be recognisable and must carry no quotes."""
    d = doc_of(("body", "Late Proposals will be rejected."))
    out = lint(d, compile_spec(d)).defects
    declined = [x for x in out if "did not run" in x.title.lower()]
    assert declined, "the check should have declined on an unlabelled document"
    for x in declined:
        assert x.quotes == [] and x.spans == []
        assert x.severity == "low"
