"""
Tests for cgate.db — the register, the ledger, and the human-only decision rule.

The design says the prohibition on machine decisions is "enforced in the
database schema rather than by convention". This file is that claim: if it is
deleted, the design is asserting something nothing checks.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from cgate import db
from cgate.model import Status


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "register.db"))
    yield c
    c.close()


@pytest.fixture()
def pursuit(conn):
    client = db.upsert_entity(conn, "Example Agency International", "authority",
                              jurisdiction="INT", created_by="tester")
    db.open_pursuit(conn, "P-0001-DEMO", client, "Schools detailed design",
                    submission_deadline="2026-12-01", notified_by="BD Manager")
    return "P-0001-DEMO"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_connect_creates_schema(tmp_path):
    path = str(tmp_path / "r.db")
    c = db.connect(path)
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"entity", "pursuit", "pursuit_party", "check_result",
            "decision", "ledger"} <= tables
    assert db.register(c) == []


def test_check_result_vocabulary_is_model_status(conn, pursuit):
    """Every model.Status value must be insertable, and 'verified' must not be.

    The schema and the enum drifted apart once already: SQL accepted 'verified'
    while model.Status had deliberately removed any word meaning cleared.
    """
    for status in Status:
        db.record_check_result(conn, pursuit, 1, status.value, {"note": "x"})

    with pytest.raises(sqlite3.IntegrityError):
        db.record_check_result(conn, pursuit, 1, "verified", {})
    with pytest.raises(sqlite3.IntegrityError):
        db.record_check_result(conn, pursuit, 1, "cleared", {})


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def test_ledger_chain_verifies(conn, pursuit):
    for i in range(5):
        db.append(conn, "test_event", "system:test", {"i": i}, pursuit_id=pursuit)
    report = db.verify_chain(conn)
    assert report.ok, str(report)
    assert report.rows >= 5


def test_ledger_requires_an_actor(conn):
    with pytest.raises(db.LedgerError):
        db.append(conn, "orphan_event", "  ", {})


def test_ledger_rejects_update_and_delete(conn, pursuit):
    db.append(conn, "e", "system:test", {}, pursuit_id=pursuit)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE ledger SET event = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM ledger")


def test_chain_detects_an_edited_row(conn, pursuit):
    """An edit made out of band — trigger dropped, row rewritten — is detected.

    This is the threat the chain exists for. The triggers stop the application
    and a casual sqlite3 prompt; they do not stop someone who drops them first.
    """
    for i in range(3):
        db.append(conn, "e", "system:test", {"i": i}, pursuit_id=pursuit)

    conn.execute("DROP TRIGGER ledger_no_update")
    conn.execute("UPDATE ledger SET payload = ? WHERE seq = 2",
                 (json.dumps({"i": 999}),))

    report = db.verify_chain(conn)
    assert not report.ok
    assert report.first_bad_seq == 2
    assert "edited" in report.reason


def test_chain_detects_a_removed_row(conn, pursuit):
    for i in range(4):
        db.append(conn, "e", "system:test", {"i": i}, pursuit_id=pursuit)

    conn.execute("DROP TRIGGER ledger_no_delete")
    conn.execute("DELETE FROM ledger WHERE seq = 2")

    report = db.verify_chain(conn)
    assert not report.ok
    assert "removed" in report.reason or "reordered" in report.reason


def test_field_separator_prevents_collision():
    """("ab","c") and ("a","bc") must not hash alike."""
    a = db._row_hash(None, "t", None, "ab", "c", "{}", "1", "1")
    b = db._row_hash(None, "t", None, "a", "bc", "{}", "1", "1")
    assert a != b


# ---------------------------------------------------------------------------
# Decisions — the line the system does not cross
# ---------------------------------------------------------------------------


def test_decision_refuses_a_system_actor(conn, pursuit):
    with pytest.raises(db.DecisionError, match="system does not decide"):
        db.record_decision(conn, pursuit, "clearance", "looks fine",
                           actor_name="system:adjudicate", actor_role="CO",
                           signature_ref="sig-1")


def test_decision_refuses_an_unknown_role(conn, pursuit):
    with pytest.raises(db.DecisionError, match="actor_role"):
        db.record_decision(conn, pursuit, "clearance", "reasons",
                           actor_name="A Person", actor_role="BD Manager",
                           signature_ref="sig-1")


@pytest.mark.parametrize("kwargs", [
    {"actor_name": "   "},
    {"signature_ref": "   "},
    {"reasons": "  "},
])
def test_decision_refuses_incomplete_accountability(conn, pursuit, kwargs):
    base = dict(decision="clearance", reasons="stated reasons",
                actor_name="A Person", actor_role="CO", signature_ref="sig-1")
    base.update(kwargs)
    with pytest.raises(db.DecisionError):
        db.record_decision(conn, pursuit, **base)


def test_sql_check_blocks_a_direct_insert_without_a_signature(conn, pursuit):
    """Bypassing record_decision() must not bypass the rule."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decision (pursuit_id, decision, reasons, actor_name, "
            "actor_role, signature_ref, decided_at) VALUES (?,?,?,?,?,?,?)",
            (pursuit, "clearance", "r", "A Person", "CO", "", "2026-08-28"),
        )


def test_a_real_decision_records_and_reaches_the_register(conn, pursuit):
    db.record_decision(conn, pursuit, "conditional_clearance",
                       reasons="Cleared subject to a signed declaration from the "
                               "named subcontractor.",
                       actor_name="Alex Reviewer", actor_role="CO",
                       signature_ref="EEL-CO-2026-014",
                       conditions="Declaration on file before submission.")
    rows = db.register(conn)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "conditional_clearance"
    assert rows[0]["decided_by"] == "Alex Reviewer"
    assert rows[0]["signature_ref"] == "EEL-CO-2026-014"

    assert db.verify_chain(conn).ok
    events = [r["event"] for r in db.ledger_rows(conn, pursuit)]
    assert "decision_recorded" in events


def test_decisions_are_immutable(conn, pursuit):
    db.record_decision(conn, pursuit, "hold", "reasons", "A Person", "CO", "sig-1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE decision SET decision = 'clearance'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM decision")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("a,b", [
    ("Example Engineering Ltd.", "EXAMPLE ENGINEERING LTD"),
    ("Acme Engineering Ltd", "acme engineering limited"),
    ("Beta Group Holding", "Beta"),
])
def test_entity_id_folds_legal_forms(a, b):
    assert db.entity_id(a) == db.entity_id(b)


def test_entity_id_separates_genuinely_different_names():
    assert db.entity_id("Acme Engineering") != db.entity_id("Acme Consulting")


def test_entity_id_separates_jurisdictions():
    assert db.entity_id("Acme", "XX") != db.entity_id("Acme", "YY")


def test_upsert_accumulates_aliases(conn):
    eid = db.upsert_entity(conn, "Acme Engineering LLC", jurisdiction="XX",
                           aliases=["Acme Eng"], created_by="tester")
    again = db.upsert_entity(conn, "ACME ENGINEERING L.L.C.", jurisdiction="XX",
                             aliases=["Acme Group"], created_by="tester")
    assert eid == again
    names = db.entity_names(db.get_entity(conn, eid))
    assert "Acme Eng" in names and "Acme Group" in names


def test_fast_track_cannot_exist_without_a_recorded_approver(conn):
    """3.10 §7.1 forbids fast-track curing a late notification.

    The constraint is that the approval must exist at the moment the track is
    set, which SQL can express and a procedure cannot.
    """
    client = db.upsert_entity(conn, "Some Client", "authority", created_by="t")
    with pytest.raises(sqlite3.IntegrityError):
        db.open_pursuit(conn, "P-BAD", client, "t", "2026-12-01", "BD",
                        review_track="fast_track")

    db.open_pursuit(conn, "P-OK", client, "t", "2026-12-01", "BD",
                    review_track="fast_track",
                    fast_track_approved_by="ECC Chair",
                    fast_track_approved_at="2026-08-28T09:00:00")
    assert db.verify_chain(conn).ok


def test_party_recorded_without_a_declaration(conn, pursuit):
    """A party with no declaration must still appear. Absence is the finding."""
    sub = db.upsert_entity(conn, "Subcontractor LLC", created_by="t")
    db.add_party(conn, pursuit, sub, "subcontractor")
    rows = db.parties(conn, pursuit)
    assert len(rows) == 1
    assert rows[0]["declaration_ref"] is None


def test_record_review_writes_items_and_one_ledger_row(conn, pursuit):
    from cgate.model import ItemResult, ReviewResult

    result = ReviewResult(
        doc_id="a" * 64, doc_path="/x/tender.pdf", checklist_version="1.0",
        tool_version="0.1.0", generated_at="2026-08-28T00:00:00",
        items=[
            ItemResult(1, "A", "Client identity", "automated", Status.UNRESOLVED),
            ItemResult(12, "C", "Integrity rules", "automated", Status.FLAG,
                       triggers=["parser_disagreement"]),
        ],
        stats={"pages": 40},
    )
    n = db.record_review(conn, pursuit, result)
    assert n == 2

    rows = list(conn.execute(
        "SELECT item_id, status FROM check_result WHERE pursuit_id = ?", (pursuit,)))
    assert {(r["item_id"], r["status"]) for r in rows} == {
        ("1", "unresolved"), ("12", "flag")}

    runs = [r for r in db.ledger_rows(conn, pursuit) if r["event"] == "review_run"]
    assert len(runs) == 1
    assert runs[0]["actor"].startswith("system:")
    assert db.verify_chain(conn).ok
