"""
Tests for cgate.workflow — the 3.10 state machine and its clocks.
"""

from __future__ import annotations

import datetime as dt

import pytest

from cgate import db, workflow
from cgate.workflow import (
    CLEARED, CLOSED, HELD, INFORMATION_REQUESTED, NOTIFIED, UNDER_REVIEW,
    WorkflowError, add_business_days, business_days_between,
)

CO = dict(actor_name="Alex Reviewer", actor_role="CO", signature_ref="SIG-1")


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "register.db"))
    yield c
    c.close()


def make_pursuit(conn, pid="P-1", *, notified_at="2026-08-03T09:00:00",
                 deadline="2026-09-30", track="standard", **kw):
    client = db.upsert_entity(conn, "Example Agency", "authority", created_by="t")
    db.open_pursuit(conn, pid, client, "A submission", submission_deadline=deadline,
                    notified_by="BD", notified_at=notified_at, review_track=track, **kw)
    return pid


# ---------------------------------------------------------------------------
# Business-day arithmetic
# ---------------------------------------------------------------------------


def test_business_days_skip_weekends():
    # Mon 3 Aug 2026 -> Mon 10 Aug 2026 is five business days.
    assert business_days_between("2026-08-03", "2026-08-10") == 5


def test_business_days_are_signed():
    assert business_days_between("2026-08-10", "2026-08-03") == -5


def test_holidays_are_excluded_when_supplied():
    hol = frozenset({dt.date(2026, 8, 6)})
    assert business_days_between("2026-08-03", "2026-08-10", hol) == 4


def test_add_business_days_lands_on_a_weekday():
    d = add_business_days("2026-08-07", 1)      # Friday + 1
    assert d.isoformat() == "2026-08-10"        # Monday
    assert d.weekday() < 5


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


def test_a_new_pursuit_is_notified(conn):
    p = make_pursuit(conn)
    assert workflow.state_of(conn, p) == NOTIFIED


def test_running_a_review_moves_it_under_review(conn):
    p = make_pursuit(conn)
    db.record_check_result(conn, p, 12, "flag", {"note": "x"})
    assert workflow.state_of(conn, p) == UNDER_REVIEW


def test_state_follows_the_decision_history(conn):
    p = make_pursuit(conn)
    workflow.record(conn, p, "information_requested", "need the declaration", **CO)
    assert workflow.state_of(conn, p) == INFORMATION_REQUESTED
    workflow.record(conn, p, "hold", "unresolved collision", **CO)
    assert workflow.state_of(conn, p) == HELD
    workflow.record(conn, p, "hold_lifted", "collision explained", **CO)
    assert workflow.state_of(conn, p) == UNDER_REVIEW


# ---------------------------------------------------------------------------
# Transitions the register must refuse
# ---------------------------------------------------------------------------


def test_cannot_close_an_entry_with_no_outcome(conn):
    p = make_pursuit(conn)
    with pytest.raises(WorkflowError, match="no recorded outcome"):
        workflow.record(conn, p, "entry_closed", "done", **CO)


def test_cannot_lift_a_hold_that_was_never_placed(conn):
    p = make_pursuit(conn)
    with pytest.raises(WorkflowError, match="no hold is in place"):
        workflow.record(conn, p, "hold_lifted", "n/a", **CO)


def test_a_closed_entry_is_not_reopened(conn):
    p = make_pursuit(conn)
    workflow.record(conn, p, "clearance", "clean", **CO)
    workflow.record(conn, p, "entry_closed", "filed", **CO)
    assert workflow.state_of(conn, p) == CLOSED
    with pytest.raises(WorkflowError, match="closed"):
        workflow.record(conn, p, "hold", "second thoughts", **CO)


def test_a_cleared_pursuit_can_still_be_held(conn):
    """New information after clearance must be recordable."""
    p = make_pursuit(conn)
    workflow.record(conn, p, "clearance", "clean", **CO)
    workflow.record(conn, p, "hold", "new information", **CO)
    assert workflow.state_of(conn, p) == HELD


def test_workflow_inherits_the_human_only_rule(conn):
    """workflow.record delegates rather than reimplementing who may decide."""
    p = make_pursuit(conn)
    with pytest.raises(db.DecisionError, match="system does not decide"):
        workflow.record(conn, p, "clearance", "looks fine",
                        actor_name="system:adjudicate", actor_role="CO",
                        signature_ref="SIG-1")


def test_a_refused_transition_writes_nothing(conn):
    p = make_pursuit(conn)
    before = len(db.ledger_rows(conn, p))
    with pytest.raises(WorkflowError):
        workflow.record(conn, p, "entry_closed", "done", **CO)
    assert len(db.ledger_rows(conn, p)) == before
    assert db.verify_chain(conn).ok


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------


def test_adequate_notification_lead_does_not_breach(conn):
    # 3 Aug -> 30 Sep is far more than ten business days.
    p = make_pursuit(conn)
    c = workflow.clocks(conn, p, as_of="2026-08-05")
    assert c.notification_lead_days >= 10
    assert not any(b.clock == "notification_lead" for b in c.breaches)


def test_late_notification_breaches(conn):
    p = make_pursuit(conn, notified_at="2026-08-03T09:00:00", deadline="2026-08-10")
    c = workflow.clocks(conn, p, as_of="2026-08-04")
    breach = next(b for b in c.breaches if b.clock == "notification_lead")
    assert breach.actual_days == 5
    assert breach.required_days == 10
    assert breach.days_short == 5
    assert "clock_breach" in c.triggers


def test_fast_track_has_a_shorter_lead_but_still_breaches_when_too_late(conn):
    """§7.1: fast-track may not be used to cure a late notification."""
    p = make_pursuit(conn, notified_at="2026-08-03T09:00:00", deadline="2026-08-06",
                     track="fast_track", fast_track_approved_by="ECC Chair",
                     fast_track_approved_at="2026-08-03T10:00:00")
    c = workflow.clocks(conn, p, as_of="2026-08-04")
    breach = next(b for b in c.breaches if b.clock == "notification_lead")
    assert breach.required_days == 5
    assert breach.actual_days == 3
    assert "cure a late notification" in breach.message


def test_review_period_breaches_when_no_determination_is_recorded(conn):
    p = make_pursuit(conn, notified_at="2026-08-03T09:00:00")
    c = workflow.clocks(conn, p, as_of="2026-08-31")
    breach = next(b for b in c.breaches if b.clock == "review_period")
    assert breach.required_days == 7
    assert breach.actual_days > 7


def test_an_information_request_pauses_the_review_clock(conn):
    p = make_pursuit(conn, notified_at="2026-08-03T09:00:00")
    workflow.record(conn, p, "information_requested", "awaiting declaration",
                    decided_at="2026-08-05T09:00:00", **CO)
    c = workflow.clocks(conn, p, as_of="2026-08-14")
    assert c.paused_days > 0
    # Without the pause, 3 Aug -> 14 Aug is nine business days against an
    # allowance of seven. The pause is what keeps it inside.
    assert c.review_days_elapsed <= c.review_days_allowed
    assert not any(b.clock == "review_period" for b in c.breaches)


def test_the_clock_resumes_when_the_information_arrives(conn):
    p = make_pursuit(conn, notified_at="2026-08-03T09:00:00")
    workflow.record(conn, p, "information_requested", "awaiting",
                    decided_at="2026-08-05T09:00:00", **CO)
    workflow.record(conn, p, "hold", "still unresolved",
                    decided_at="2026-08-07T09:00:00", **CO)
    c = workflow.clocks(conn, p, as_of="2026-08-31")
    # The pause is bounded by the next decision, not by today.
    assert c.paused_days == 2


def test_a_recorded_outcome_stops_the_review_clock(conn):
    p = make_pursuit(conn, notified_at="2026-08-03T09:00:00")
    workflow.record(conn, p, "clearance", "clean", decided_at="2026-08-06T09:00:00", **CO)
    c = workflow.clocks(conn, p, as_of="2026-09-30")
    assert not any(b.clock == "review_period" for b in c.breaches)
    assert c.state == CLEARED


def test_a_missing_holiday_calendar_is_stated_not_assumed(conn):
    p = make_pursuit(conn)
    c = workflow.clocks(conn, p, as_of="2026-08-05")
    assert c.holiday_calendar_supplied is False
    assert "under-counted" in workflow.summarise(c)

    c2 = workflow.clocks(conn, p, as_of="2026-08-05",
                         holidays=frozenset({dt.date(2026, 8, 4)}))
    assert c2.holiday_calendar_supplied is True


def test_clocks_serialise_for_the_evidence_bundle(conn):
    p = make_pursuit(conn, notified_at="2026-08-03T09:00:00", deadline="2026-08-10")
    d = workflow.clocks(conn, p, as_of="2026-08-04").to_dict()
    assert d["triggers"] == ["clock_breach"]
    assert d["breaches"][0]["clock"] == "notification_lead"
