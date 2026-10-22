"""
cgate.workflow — the gate's state machine and its statutory clocks.

Implements the timelines and transitions of an example review procedure (SOP-3.10):

    notified -> under_review -> information_requested (clock paused)
                             -> cleared | conditionally_cleared | held
                             -> closed

Clocks (3.10 §4.3, §5.3), in business days:

    notification lead   10 standard, 5 fast-track, before the submission deadline
    review              7 standard, 3 fast-track, from notification
    ECC Chair approval  1, for fast-track activation

Why the state machine is worth having
-------------------------------------
Two rules in 3.10 are trivially stated and easy to break under deadline
pressure, and both are the kind of thing an investigator asks about afterwards:

* **Fast-track may not be used to cure a late notification (§7.1).** The SQL
  CHECK on `pursuit` already refuses a fast-track row without a recorded ECC
  Chair approval, so the approval cannot be back-filled onto a track already in
  use. This module adds the second half: it reports when a fast-track pursuit
  was notified so late that even the fast-track lead time was missed, which is
  precisely the situation §7.1 anticipates.

* **An entry cannot be closed before it has an outcome, and a hold cannot be
  lifted that was never placed.** Those are ordering facts about a register,
  and `check_transition()` refuses them.

What the clock arithmetic assumes, and what it does not
-------------------------------------------------------
Business days are Monday to Friday. Public holidays are supplied by the caller
(`Clocks.holidays`) and default to none, because this system may be used across
several countries whose calendars differ and none of which the
tool is entitled to guess. A breach computed without a holiday list is a
conservative reading — it can under-count elapsed days, never over-count — and
`Clocks.holiday_calendar_supplied` records which reading a report is showing,
so a CO is never told a deadline was met on the strength of an assumption the
tool made for itself.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass, field

from . import db, settings

# ---------------------------------------------------------------------------
# States and transitions
# ---------------------------------------------------------------------------

NOTIFIED = "notified"
UNDER_REVIEW = "under_review"
INFORMATION_REQUESTED = "information_requested"
CLEARED = "cleared"
CONDITIONALLY_CLEARED = "conditionally_cleared"
HELD = "held"
CLOSED = "closed"

# Maps a `decision.decision` value to the state it puts the pursuit in.
STATE_FOR_DECISION = {
    "information_requested": INFORMATION_REQUESTED,
    "clearance": CLEARED,
    "conditional_clearance": CONDITIONALLY_CLEARED,
    "hold": HELD,
    "hold_lifted": UNDER_REVIEW,
    "entry_closed": CLOSED,
}

# Outcomes that constitute a determination under 3.10 §6.
OUTCOMES = {"clearance", "conditional_clearance", "hold"}

ALLOWED: dict[str, set[str]] = {
    NOTIFIED: {"information_requested", "clearance", "conditional_clearance", "hold"},
    UNDER_REVIEW: {"information_requested", "clearance", "conditional_clearance", "hold"},
    INFORMATION_REQUESTED: {"clearance", "conditional_clearance", "hold",
                            "information_requested"},
    CLEARED: {"hold", "entry_closed"},
    CONDITIONALLY_CLEARED: {"hold", "entry_closed"},
    HELD: {"hold_lifted", "entry_closed"},
    CLOSED: set(),
}

# Clock values come from config/procedure.example.yaml (see cgate.settings);
# the built-in defaults are 10 / 5 / 7 / 3 business days.
STANDARD_NOTIFICATION_DAYS = settings.clock("standard_notification_days")
FASTTRACK_NOTIFICATION_DAYS = settings.clock("fasttrack_notification_days")
STANDARD_REVIEW_DAYS = settings.clock("standard_review_days")
FASTTRACK_REVIEW_DAYS = settings.clock("fasttrack_review_days")


class WorkflowError(RuntimeError):
    """A transition that 3.10 does not permit."""


# ---------------------------------------------------------------------------
# Business-day arithmetic
# ---------------------------------------------------------------------------


def _date(value: str | _dt.date) -> _dt.date:
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(str(value)[:10])


def business_days_between(
    start: str | _dt.date,
    end: str | _dt.date,
    holidays: frozenset[_dt.date] = frozenset(),
) -> int:
    """Business days from `start` to `end`, excluding `start`, including `end`.

    Negative when `end` precedes `start`, so a caller can distinguish "three
    days left" from "three days late" without a second call.
    """
    a, b = _date(start), _date(end)
    sign = 1 if b >= a else -1
    lo, hi = (a, b) if sign == 1 else (b, a)
    days = 0
    cur = lo
    while cur < hi:
        cur += _dt.timedelta(days=1)
        if cur.weekday() < 5 and cur not in holidays:
            days += 1
    return days * sign


def add_business_days(
    start: str | _dt.date,
    n: int,
    holidays: frozenset[_dt.date] = frozenset(),
) -> _dt.date:
    cur = _date(start)
    remaining = n
    while remaining > 0:
        cur += _dt.timedelta(days=1)
        if cur.weekday() < 5 and cur not in holidays:
            remaining -= 1
    return cur


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------


@dataclass
class Breach:
    clock: str
    required_days: int
    actual_days: int
    message: str

    @property
    def days_short(self) -> int:
        return self.required_days - self.actual_days


@dataclass
class Clocks:
    pursuit_id: str
    review_track: str
    notified_at: str
    submission_deadline: str
    notification_lead_days: int
    notification_required_days: int
    review_due: str
    review_days_elapsed: int
    review_days_allowed: int
    paused_days: int
    state: str
    holiday_calendar_supplied: bool
    breaches: list[Breach] = field(default_factory=list)

    @property
    def triggers(self) -> list[str]:
        return ["clock_breach"] if self.breaches else []

    def to_dict(self) -> dict:
        return {
            "pursuit_id": self.pursuit_id,
            "review_track": self.review_track,
            "state": self.state,
            "notified_at": self.notified_at,
            "submission_deadline": self.submission_deadline,
            "notification_lead_days": self.notification_lead_days,
            "notification_required_days": self.notification_required_days,
            "review_due": self.review_due,
            "review_days_elapsed": self.review_days_elapsed,
            "review_days_allowed": self.review_days_allowed,
            "paused_days": self.paused_days,
            "holiday_calendar_supplied": self.holiday_calendar_supplied,
            "breaches": [
                {"clock": b.clock, "required_days": b.required_days,
                 "actual_days": b.actual_days, "message": b.message}
                for b in self.breaches
            ],
            "triggers": self.triggers,
        }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def history(conn: sqlite3.Connection, pursuit_id: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM decision WHERE pursuit_id = ? ORDER BY decided_at, id",
        (pursuit_id,),
    ))


def state_of(conn: sqlite3.Connection, pursuit_id: str) -> str:
    """Current state, derived from the decision history.

    Derived rather than stored: a stored state field can disagree with the
    decisions that produced it, and in a register that has to be defensible the
    decisions are the truth.
    """
    rows = history(conn, pursuit_id)
    if not rows:
        reviewed = conn.execute(
            "SELECT 1 FROM check_result WHERE pursuit_id = ? LIMIT 1", (pursuit_id,)
        ).fetchone()
        return UNDER_REVIEW if reviewed else NOTIFIED
    return STATE_FOR_DECISION.get(rows[-1]["decision"], UNDER_REVIEW)


def check_transition(conn: sqlite3.Connection, pursuit_id: str, decision: str) -> str:
    """Raise if `decision` is not permitted from the current state."""
    current = state_of(conn, pursuit_id)
    if decision not in STATE_FOR_DECISION:
        raise WorkflowError(f"unknown decision: {decision!r}")
    if decision not in ALLOWED[current]:
        if current == CLOSED:
            raise WorkflowError(
                f"pursuit {pursuit_id} is closed. A closed entry is not reopened; "
                "a change of position is a new pursuit with its own notification."
            )
        if decision == "entry_closed":
            raise WorkflowError(
                f"pursuit {pursuit_id} is {current} and has no recorded outcome. "
                "3.10 §10 requires the register to carry the outcome before the "
                "entry is closed."
            )
        if decision == "hold_lifted":
            raise WorkflowError(
                f"pursuit {pursuit_id} is {current}; no hold is in place to lift."
            )
        raise WorkflowError(
            f"{decision!r} is not permitted from {current!r}. "
            f"Permitted: {', '.join(sorted(ALLOWED[current])) or 'none'}."
        )
    return current


def record(
    conn: sqlite3.Connection,
    pursuit_id: str,
    decision: str,
    reasons: str,
    actor_name: str,
    actor_role: str,
    signature_ref: str,
    *,
    conditions: str | None = None,
    decided_at: str | None = None,
    checklist_ver: str = "?",
) -> int:
    """Record a determination, refusing transitions 3.10 does not allow.

    Everything `db.record_decision` refuses, this refuses too — it delegates
    rather than reimplementing, so there is one definition of who may decide.
    """
    check_transition(conn, pursuit_id, decision)
    return db.record_decision(
        conn, pursuit_id, decision, reasons, actor_name, actor_role,
        signature_ref, conditions=conditions, decided_at=decided_at,
        checklist_ver=checklist_ver,
    )


# ---------------------------------------------------------------------------
# Clock evaluation
# ---------------------------------------------------------------------------


def clocks(
    conn: sqlite3.Connection,
    pursuit_id: str,
    *,
    as_of: str | None = None,
    holidays: frozenset[_dt.date] | None = None,
) -> Clocks:
    """Evaluate every clock 3.10 places on a pursuit."""
    row = conn.execute("SELECT * FROM pursuit WHERE id = ?", (pursuit_id,)).fetchone()
    if row is None:
        raise WorkflowError(f"no such pursuit: {pursuit_id}")

    hol = holidays if holidays is not None else frozenset()
    today = _date(as_of or db._now())
    fast = row["review_track"] == "fast_track"
    need_lead = FASTTRACK_NOTIFICATION_DAYS if fast else STANDARD_NOTIFICATION_DAYS
    allow_review = FASTTRACK_REVIEW_DAYS if fast else STANDARD_REVIEW_DAYS

    lead = business_days_between(row["notified_at"], row["submission_deadline"], hol)

    # An information request pauses the review clock (3.10 §5.3). Pause runs
    # from the request to the next decision, or to today if still outstanding.
    rows = history(conn, pursuit_id)
    paused = 0
    for i, d in enumerate(rows):
        if d["decision"] != "information_requested":
            continue
        resumed = rows[i + 1]["decided_at"] if i + 1 < len(rows) else today
        paused += max(0, business_days_between(d["decided_at"], resumed, hol))

    state = state_of(conn, pursuit_id)
    outcome = next((d for d in rows if d["decision"] in OUTCOMES), None)
    end = _date(outcome["decided_at"]) if outcome else today
    elapsed = max(0, business_days_between(row["notified_at"], end, hol) - paused)
    due = add_business_days(row["notified_at"], allow_review + paused, hol)

    breaches: list[Breach] = []
    if lead < need_lead:
        breaches.append(Breach(
            clock="notification_lead",
            required_days=need_lead,
            actual_days=lead,
            message=(
                f"Notified {lead} business day(s) before the submission deadline; "
                f"3.10 §4.3 requires {need_lead} on the "
                f"{'fast-track' if fast else 'standard'} track."
                + (" §7.1 prohibits fast-track being used to cure a late "
                   "notification, so this cannot be resolved by changing track."
                   if fast else "")
            ),
        ))
    if outcome is None and elapsed > allow_review:
        breaches.append(Breach(
            clock="review_period",
            required_days=allow_review,
            actual_days=elapsed,
            message=(
                f"{elapsed} business day(s) elapsed since notification with no "
                f"recorded determination; 3.10 §5.3 allows {allow_review}"
                + (f" (excluding {paused} paused day(s))" if paused else "") + "."
            ),
        ))

    return Clocks(
        pursuit_id=pursuit_id,
        review_track=row["review_track"],
        notified_at=row["notified_at"],
        submission_deadline=row["submission_deadline"],
        notification_lead_days=lead,
        notification_required_days=need_lead,
        review_due=due.isoformat(),
        review_days_elapsed=elapsed,
        review_days_allowed=allow_review,
        paused_days=paused,
        state=state,
        holiday_calendar_supplied=bool(hol),
        breaches=breaches,
    )


def summarise(c: Clocks) -> str:
    lines = [
        f"  state: {c.state} ({c.review_track})",
        f"  notification lead: {c.notification_lead_days} business day(s), "
        f"{c.notification_required_days} required",
        f"  review: {c.review_days_elapsed} of {c.review_days_allowed} day(s) used"
        + (f", {c.paused_days} paused" if c.paused_days else "")
        + f", due {c.review_due}",
    ]
    if not c.holiday_calendar_supplied:
        lines.append("  no holiday calendar supplied — elapsed days may be "
                     "under-counted, never over-counted")
    for b in c.breaches:
        lines.append(f"  BREACH {b.clock}: {b.message}")
    return "\n".join(lines)
