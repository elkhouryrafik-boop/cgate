"""
Tests for cgate.registry — declarations and cross-pursuit collisions.

These cover checklist items 6, 7 and 9: the items that cover the highest-consequence
integrity risk: a subcontractor bidding independently on the same procurement.
"""

from __future__ import annotations

import pytest

from cgate import db, registry
from cgate.registry import DECLARABLE_ROLES


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "register.db"))
    yield c
    c.close()


@pytest.fixture()
def world(conn):
    """Two live pursuits for the same client, sharing one subcontractor."""
    agency = db.upsert_entity(conn, "Example Agency", "authority", created_by="t")
    other = db.upsert_entity(conn, "Ministry of Public Works", "authority", created_by="t")
    sub = db.upsert_entity(conn, "Shared Subcontractor LLC", created_by="t")
    solo = db.upsert_entity(conn, "Exclusive Partner Ltd", created_by="t")

    db.open_pursuit(conn, "P-1", agency, "Schools detailed design",
                    submission_deadline="2026-12-01", notified_by="BD",
                    tender_reference="RFP-2026/117", notified_at="2026-08-01T09:00:00")
    db.open_pursuit(conn, "P-2", agency, "Second Example Agency package",
                    submission_deadline="2026-12-15", notified_by="BD",
                    tender_reference="RFP-2026/117", notified_at="2026-08-05T09:00:00")
    db.open_pursuit(conn, "P-3", other, "Unrelated ministry job",
                    submission_deadline="2026-11-20", notified_by="BD",
                    tender_reference="MOPW-77", notified_at="2026-08-06T09:00:00")

    db.add_party(conn, "P-1", sub, "subcontractor")
    db.add_party(conn, "P-2", sub, "consortium_partner")
    db.add_party(conn, "P-3", sub, "subcontractor")
    db.add_party(conn, "P-1", solo, "consortium_partner")
    return {"sub": sub, "solo": solo, "agency": agency}


AS_OF = "2026-08-10"


# ---------------------------------------------------------------------------
# Collisions
# ---------------------------------------------------------------------------


def test_same_tender_reference_is_the_most_material_collision(conn, world):
    cols = registry.collisions_for(conn, "P-1", world["sub"], "subcontractor",
                                   as_of=AS_OF)
    assert len(cols) == 2
    assert cols[0].severity == "same_procurement"
    assert cols[0].other_pursuit_id == "P-2"
    assert cols[1].severity == "concurrent"


def test_a_party_on_one_pursuit_has_no_collisions(conn, world):
    cols = registry.collisions_for(conn, "P-1", world["solo"], "consortium_partner",
                                   as_of=AS_OF)
    assert cols == []


def test_closed_pursuits_do_not_collide(conn, world):
    """A determined and closed pursuit is not a live one."""
    db.record_decision(conn, "P-2", "clearance", "r", "A Person", "CO", "sig")
    db.record_decision(conn, "P-2", "entry_closed", "r", "A Person", "CO", "sig")
    cols = registry.collisions_for(conn, "P-1", world["sub"], "subcontractor",
                                   as_of=AS_OF)
    assert [c.other_pursuit_id for c in cols] == ["P-3"]


def test_pursuits_past_their_deadline_do_not_collide(conn, world):
    cols = registry.collisions_for(conn, "P-1", world["sub"], "subcontractor",
                                   as_of="2026-12-10")
    assert [c.other_pursuit_id for c in cols] == ["P-2"]


def test_standing_reports_every_party(conn, world):
    parties = registry.standing(conn, "P-1", as_of=AS_OF)
    assert {p.legal_name for p in parties} == {
        "Shared Subcontractor LLC", "Exclusive Partner Ltd"}


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def test_a_party_with_no_declaration_blocks(conn, world):
    parties = registry.standing(conn, "P-1", as_of=AS_OF)
    blocked = registry.blocking(parties)
    assert len(blocked) == 2
    assert all(p.declaration_state == "missing" for p in blocked)


def test_signing_a_declaration_unblocks_that_party(conn, world):
    registry.sign_declaration(conn, "P-1", world["solo"], "consortium_partner",
                              "DECL-2026-014", signatory="A. Rowan",
                              signed_at="2026-08-09")
    parties = registry.standing(conn, "P-1", as_of=AS_OF)
    blocked = {p.legal_name for p in registry.blocking(parties)}
    assert blocked == {"Shared Subcontractor LLC"}
    assert db.verify_chain(conn).ok


def test_the_system_cannot_sign_for_a_counterparty(conn, world):
    with pytest.raises(ValueError, match="system actor"):
        registry.sign_declaration(conn, "P-1", world["solo"], "consortium_partner",
                                  "DECL-1", signatory="system:intake")


@pytest.mark.parametrize("ref,who", [("", "A. Rowan"), ("DECL-1", "  ")])
def test_a_declaration_needs_a_reference_and_a_signatory(conn, world, ref, who):
    with pytest.raises(ValueError):
        registry.sign_declaration(conn, "P-1", world["solo"], "consortium_partner",
                                  ref, signatory=who)


def test_cannot_file_a_declaration_for_a_party_not_on_the_pursuit(conn, world):
    with pytest.raises(ValueError, match="no party"):
        registry.sign_declaration(conn, "P-1", world["sub"], "agent",
                                  "DECL-9", signatory="Someone")


def test_the_signatory_is_recorded_under_their_own_name(conn, world):
    registry.sign_declaration(conn, "P-1", world["solo"], "consortium_partner",
                              "DECL-2026-014", signatory="A. Rowan")
    rows = [r for r in db.ledger_rows(conn, "P-1")
            if r["event"] == "declaration_signed"]
    assert rows[0]["actor"] == "A. Rowan"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def test_evidence_uses_the_keys_the_checklist_names(conn, world):
    ev = registry.evidence(registry.standing(conn, "P-1", as_of=AS_OF))
    assert set(ev) == {"signed_declaration", "registry_collision_report", "triggers"}
    assert "declaration_missing" in ev["triggers"]
    assert "registry_collision" in ev["triggers"]


def test_items_6_7_9_never_auto_clear_even_on_complete_evidence(conn, world):
    """The design rule under test. Complete evidence advances to a human, not past one."""
    from cgate.adjudicate import adjudicate, load_checklist
    from cgate.model import Document, Status

    # A pursuit with one party, fully declared and colliding with nothing.
    clean = db.upsert_entity(conn, "Clean Partner Ltd", created_by="t")
    db.open_pursuit(conn, "P-9", world["agency"], "Clean job",
                    submission_deadline="2026-12-01", notified_by="BD",
                    tender_reference="CLEAN-1", notified_at="2026-08-01T09:00:00")
    db.add_party(conn, "P-9", clean, "subcontractor")
    registry.sign_declaration(conn, "P-9", clean, "subcontractor", "DECL-CLEAN",
                              signatory="A Person", signed_at="2026-08-02")

    ev = registry.evidence(registry.standing(conn, "P-9", as_of=AS_OF))
    assert ev["triggers"] == []

    checklist = load_checklist("config/checklist.example.yaml")
    doc = Document(doc_id="d" * 64, path="x.pdf", filetype="pdf", n_pages=1)
    bundle = {"6": ev, "7": ev, "9": ev}
    items = {i.item_id: i for i in adjudicate(checklist, doc, [], bundle)}

    # Items 7 and 9 rest on mechanisms 1 and 2 alone, which this module supplies
    # in full. Clean evidence advances them to a person and stops there.
    for iid in (7, 9):
        assert items[iid].status is Status.REQUIRES_HUMAN, f"item {iid}"
        assert items[iid].never_auto_clear

    # Item 6 names four mechanisms. This module is two of them, so its evidence
    # is incomplete on its own and must not read as clean. Completing item 6
    # requires the attendance record and the conflictscan report as well —
    # see test_gate.py.
    assert items[6].status is Status.FLAG
    assert "attendance_record" in items[6].notes[0]
    assert "conflictscan_report" in items[6].notes[0]


def test_a_collision_flags_the_item(conn, world):
    from cgate.adjudicate import adjudicate, load_checklist
    from cgate.model import Document, Status

    ev = registry.evidence(registry.standing(conn, "P-1", as_of=AS_OF))
    checklist = load_checklist("config/checklist.example.yaml")
    doc = Document(doc_id="d" * 64, path="x.pdf", filetype="pdf", n_pages=1)
    items = {i.item_id: i for i in adjudicate(checklist, doc, [], {"6": ev})}
    assert items[6].status is Status.FLAG
    assert "registry_collision" in items[6].triggers
    assert "declaration_missing" in items[6].triggers


def test_declarable_roles_cover_every_role_the_checklist_names():
    """Items 6, 7 and 9 name subcontractors, consortium partners and agents."""
    assert {"subcontractor", "consortium_partner", "agent"} <= set(DECLARABLE_ROLES)
