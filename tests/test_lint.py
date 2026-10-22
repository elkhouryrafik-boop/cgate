"""
Tests for the checks that read between clauses.

Two properties are load-bearing and each has tests on both sides of it.

*It fires.* Every check has a positive case built from the shape it exists to
catch, so a refactor that quietly stops finding two values for one quantity
fails here rather than in a tender.

*It does not fire on the document working correctly.* Seven posts with seven
different experience minima, a weighting stated twice as 70/30, a minimum
technical score in the evaluation section — all of these look like defects to a
careless implementation, and the negative tests are what keep the operating
point honest rather than merely loud.
"""

from __future__ import annotations

import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cgate.lint import ground_defects, lint
from cgate.model import Defect, SpanRef
from cgate.spec import compile_spec
from tests.test_spec import doc_of


def defects_of(*rows: tuple[str, str], check: str | None = None) -> list:
    d = doc_of(*rows)
    out = lint(d, compile_spec(d)).defects
    return [x for x in out if check is None or x.check == check]


# ---------------------------------------------------------------------------
# value_conflict
# ---------------------------------------------------------------------------


def test_two_values_for_one_quantity_are_reported():
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
    assert {"USD 750,000", "USD 900,000"} == set(found[0].quotes)


def test_rows_of_one_table_are_a_list_not_a_disagreement():
    found = defects_of(
        ("heading", "4.4 Key Personnel"),
        ("body", "The bidder shall propose the following key personnel."),
        ("table", "Position | Minimum qualification and experience"),
        ("table", "Team Leader | Registered engineer with at least 15 years' experience"),
        ("table", "Structural Engineer | Registered engineer with at least 12 years' experience"),
        ("table", "WASH Engineer | Registered engineer with at least 8 years' experience"),
        check="value_conflict",
    )
    assert found == []


def test_the_same_pair_of_values_stated_twice_is_agreement():
    found = defects_of(
        ("heading", "2. Proposal Data Sheet"),
        ("body", "The technical proposal shall carry 70% and the financial "
                 "proposal 30% of the combined score."),
        ("heading", "5.4 Combined Scoring"),
        ("body", "Proposals must be ranked on 70% technical and 30% financial."),
        check="value_conflict",
    )
    assert found == []


def test_background_prose_is_not_load_bearing():
    # Two schools with two enrolments are not two answers to one question.
    found = defects_of(
        ("heading", "1. Introduction and Background"),
        ("body", "Bunyika Primary School is a day school enrolling approximately "
                 "412 learners in eleven classrooms."),
        ("body", "Kalenga Primary School is a day school enrolling approximately "
                 "355 learners in nine classrooms."),
        check="value_conflict",
    )
    assert found == []


def test_special_conditions_resolve_the_conflict_they_create():
    found = defects_of(
        ("heading", "ANNEX A GENERAL TERMS AND CONDITIONS"),
        ("heading", "ARTICLE 12 - DELAY AND LIQUIDATED DAMAGES"),
        ("body", "The Consultant shall pay liquidated damages up to a maximum "
                 "of 10% of the contract price."),
        ("heading", "ANNEX B SPECIAL CONDITIONS OF CONTRACT"),
        ("body", "Liquidated damages shall be capped at a maximum of 30% of the "
                 "contract price."),
        check="value_conflict",
    )
    assert len(found) == 1
    assert found[0].resolution == "30%"
    assert found[0].severity == "medium"


# ---------------------------------------------------------------------------
# date_arithmetic
# ---------------------------------------------------------------------------


def test_the_documents_own_dates_pick_the_governing_value():
    found = defects_of(
        ("table", "Item | Detail"),
        ("table", "Proposal submission deadline | 27 November 2026, 16:00 hrs"),
        ("table", "Proposal validity period | 90 days from the submission deadline"),
        ("heading", "8. Bid Validity and Bid Security"),
        ("body", "Bids shall remain valid for 120 days from the submission deadline."),
        ("body", "The bid security must be valid through 24 April 2027 (that is, "
                 "28 days beyond the bid validity period)."),
        check="date_arithmetic",
    )
    assert len(found) == 1
    assert found[0].resolution == "120 days"


def test_the_offset_does_not_join_the_conflict_it_resolves():
    found = defects_of(
        ("table", "Item | Detail"),
        ("table", "Proposal submission deadline | 27 November 2026, 16:00 hrs"),
        ("table", "Proposal validity period | 90 days from the submission deadline"),
        ("heading", "8. Bid Validity and Bid Security"),
        ("body", "Bids shall remain valid for 120 days from the submission deadline."),
        ("body", "The bid security must be valid through 24 April 2027 (that is, "
                 "28 days beyond the bid validity period)."),
        check="value_conflict",
    )
    assert len(found) == 1
    assert "28 days" not in found[0].quotes


# ---------------------------------------------------------------------------
# table_sum
# ---------------------------------------------------------------------------


def test_a_percentage_column_that_does_not_total_is_reported():
    found = defects_of(
        ("heading", "7.2 Payment Schedule"),
        ("body", "Payment will be made against certified milestones as follows:"),
        ("table", "# | Milestone | % of Contract Price"),
        ("table", "1 | Advance payment on contract signature | 15"),
        ("table", "2 | Written acceptance of the Inception Report (D1) | 20"),
        ("table", "3 | Written acceptance of the final design package (D4) | 25"),
        ("table", "4 | Award of the works contract by the Foundation | 15"),
        ("table", "5 | Certification of practical completion (D7) | 15"),
        ("table", "6 | Acceptance of the Final Completion Report (D8) | 5"),
        check="table_sum",
    )
    assert len(found) == 1
    assert "95" in found[0].title


def test_a_percentage_column_that_totals_is_not_reported():
    found = defects_of(
        ("heading", "7.2 Payment Schedule"),
        ("body", "Payment will be made against certified milestones as follows:"),
        ("table", "# | Milestone | % of Contract Price"),
        ("table", "1 | Advance payment on contract signature | 20"),
        ("table", "2 | Written acceptance of the Inception Report (D1) | 30"),
        ("table", "3 | Written acceptance of the final design package (D4) | 50"),
        check="table_sum",
    )
    assert found == []


# ---------------------------------------------------------------------------
# foreign_trigger
# ---------------------------------------------------------------------------


def test_a_milestone_nobody_on_our_side_can_cause_is_reported():
    found = defects_of(
        ("heading", "7.2 Payment Schedule"),
        ("body", "Payment will be made against certified milestones as follows:"),
        ("table", "# | Milestone | % of Contract Price"),
        ("table", "1 | Written acceptance of the Inception Report (D1) | 40"),
        ("table", "2 | Award of the works contract by the Foundation | 60"),
        check="foreign_trigger",
    )
    assert len(found) == 1
    assert "Award of the works contract" in found[0].quotes[0]


def test_a_milestone_naming_our_deliverable_is_not_reported():
    found = defects_of(
        ("heading", "7.2 Payment Schedule"),
        ("body", "Payment will be made against certified milestones as follows:"),
        ("table", "# | Milestone | % of Contract Price"),
        ("table", "1 | Written acceptance of the Inception Report (D1) by the Foundation | 50"),
        ("table", "2 | Acceptance of the Final Report (D8) by the Foundation | 50"),
        check="foreign_trigger",
    )
    assert found == []


# ---------------------------------------------------------------------------
# disqualifying_rule_off_anchor
# ---------------------------------------------------------------------------


def test_a_bid_ending_rule_in_an_evaluation_section_is_reported():
    found = defects_of(
        ("heading", "6.4 Financial Evaluation"),
        ("body", "Proposals quoted in another currency, or exclusive of taxes, "
                 "will be treated as non-responsive."),
        check="disqualifying_rule_off_anchor",
    )
    assert len(found) == 1


def test_a_bid_ending_rule_where_bidders_read_is_not_reported():
    found = defects_of(
        ("heading", "5. Mandatory Submission Requirements"),
        ("body", "Any proposal that does not include items (a) to (f) will be "
                 "rejected at opening as non-responsive."),
        check="disqualifying_rule_off_anchor",
    )
    assert found == []


def test_a_minimum_score_belongs_in_the_evaluation_section():
    found = defects_of(
        ("heading", "6.3 Minimum Technical Score"),
        ("body", "A technical proposal scoring below seventy percent (70%) of the "
                 "available points will be rejected."),
        check="disqualifying_rule_off_anchor",
    )
    assert found == []


# ---------------------------------------------------------------------------
# personnel_requirement_off_anchor
# ---------------------------------------------------------------------------


def test_a_duty_on_a_named_post_stated_away_from_that_post_is_reported():
    found = defects_of(
        ("heading", "4.4 Key Personnel"),
        ("body", "The bidder shall propose the following key personnel."),
        ("table", "Position | Minimum qualification and experience"),
        ("table", "Team Leader | Registered professional with at least 15 years' "
                  "experience in design and supervision of public buildings"),
        ("heading", "3.3 Deliverables and Indicative Schedule"),
        ("body", "Community consultations are expected to be conducted in Swahili, "
                 "and the Team Leader shall be conversant in Swahili."),
        check="personnel_requirement_off_anchor",
    )
    assert len(found) == 1
    assert "Team Leader" in found[0].title


def test_a_duty_the_personnel_table_already_carries_is_not_reported():
    found = defects_of(
        ("heading", "4.4 Key Personnel"),
        ("body", "The bidder shall propose the following key personnel."),
        ("table", "Position | Minimum qualification and experience"),
        ("table", "Clerk of Works | Full-time site presence at each school during "
                  "the works, at least 5 years' experience"),
        ("heading", "3.2 Part B - Supervision"),
        ("body", "The Consultant shall maintain full-time site presence through a "
                 "clerk of works at each school for the duration of the works."),
        check="personnel_requirement_off_anchor",
    )
    assert found == []


# ---------------------------------------------------------------------------
# single_point_obligation
# ---------------------------------------------------------------------------


def test_a_requirement_stated_once_outside_the_requirements_is_reported():
    found = defects_of(
        ("heading", "3.2 Part B - Construction Supervision Services"),
        ("body", "Following award of the works contract, the Consultant shall:"),
        ("body", "8. Maintain professional indemnity insurance with a limit of "
                 "indemnity of not less than USD 1,500,000 per claim."),
        check="single_point_obligation",
    )
    assert len(found) == 1
    assert "indemnity insurance" in found[0].quotes[0]


def test_a_task_with_no_certificate_in_it_is_not_a_single_point_obligation():
    found = defects_of(
        ("heading", "3.2 Part B - Construction Supervision Services"),
        ("body", "Following award of the works contract, the Consultant shall:"),
        ("body", "5. Chair monthly site meetings and issue minutes within five "
                 "working days."),
        check="single_point_obligation",
    )
    assert found == []


# ---------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------


def test_the_reference_check_reports_that_it_did_not_run():
    # Two numbered provisions is not an index. Saying nothing here would be
    # indistinguishable from saying the references are sound.
    found = defects_of(
        ("heading", "1. Scope"),
        ("body", "The Consultant shall comply with Section 9.4 in all respects."),
        check="dangling_reference",
    )
    assert len(found) == 1
    assert "did not run" in found[0].title


# ---------------------------------------------------------------------------
# grounding
# ---------------------------------------------------------------------------


def test_a_defect_whose_span_does_not_resolve_is_deleted():
    d = doc_of(("body", "The Consultant shall submit a report."))
    bad = Defect(
        defect_id="X999",
        check="value_conflict",
        severity="high",
        title="invented",
        detail="",
        spans=[SpanRef(d.doc_id, "d.b99", 0, 10)],
        quotes=["nothing here"],
    )
    kept, dropped = ground_defects(d, [bad])
    assert kept == []
    assert dropped == 1


def test_every_defect_the_checks_raise_survives_grounding():
    d = doc_of(
        ("heading", "4.3 Financial Capacity"),
        ("body", "A bidder must demonstrate a minimum average annual turnover "
                 "of USD 750,000 over the last three audited financial years."),
        ("heading", "5. Mandatory Submission Requirements"),
        ("body", "(h) Audited statements demonstrating average annual turnover "
                 "of not less than USD 900,000;"),
    )
    spec = lint(d, compile_spec(d))
    assert spec.defects
    for x in spec.defects:
        for sp in x.spans:
            assert d.text_of(sp)
