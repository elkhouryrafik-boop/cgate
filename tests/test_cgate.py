"""
Tests for the properties this tool is not allowed to lose.

These are not coverage tests. Each one guards a specific claim made in
docs/ARCHITECTURE.md or in config/checklist.example.yaml — that findings are grounded in
the source, that the system never clears anything, that an unchecked item cannot
look like a clean one, and that the tool does not change its own behaviour
without a named human and a version bump.
"""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cgate import lexicon as lx
from cgate.adjudicate import ChecklistError, _assert_enforceable, adjudicate, load_checklist
from cgate.extract import classify_modality, extract, spans_for
from cgate.ground import gate, verify
from cgate.ingest import ingest
from cgate.model import Modality, SpanRef, Status
from cgate.segment import segment

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(ROOT, "bench", "fixtures")
CHECKLIST = os.path.join(ROOT, "config/checklist.example.yaml")


@pytest.fixture(scope="module")
def integrity():
    doc = ingest(os.path.join(FIXTURES, "gc_integrity.txt"))
    clauses = segment(doc)
    return doc, clauses, extract(doc, clauses)


# ---------------------------------------------------------------------------
# Grounding: the one defence a language model cannot talk its way past
# ---------------------------------------------------------------------------


def test_every_extracted_requirement_is_verbatim_in_its_cited_span(integrity):
    doc, _clauses, reqs = integrity
    assert reqs
    for r in reqs:
        ok, reason = verify(doc, r)
        assert ok, f"{r.req_id} failed grounding: {reason}\n{r.text[:120]}"


def test_gate_deletes_a_finding_whose_quote_is_not_in_the_source(integrity):
    doc, _clauses, reqs = integrity
    invented = copy.deepcopy(reqs[0])
    invented.req_id = "fabricated"
    invented.text = "The Contractor shall surrender its firstborn to the Purchaser."

    kept, report = gate(doc, [invented])
    assert kept == []
    assert report.dropped == 1
    assert report.reasons == {"quote_not_in_cited_span": 1}


def test_gate_deletes_a_finding_whose_span_does_not_resolve(integrity):
    doc, _clauses, reqs = integrity
    broken = copy.deepcopy(reqs[0])
    broken.spans = [SpanRef(doc.doc_id, "no.such.block", 0, 10)]

    kept, report = gate(doc, [broken])
    assert kept == []
    assert report.reasons == {"span_unresolvable": 1}


def test_spans_round_trip_to_the_same_characters(integrity):
    doc, clauses, _reqs = integrity
    clause = max(clauses, key=lambda c: len(c.text))
    start, end = 10, min(len(clause.text) - 1, 220)
    spans = spans_for(clause, start, end)
    recovered = " ".join(doc.text_of(s) or "" for s in spans)
    assert clause.text[start:end] in recovered


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_plural_subcontractors_is_matched():
    """The R4 anchor once read (?:or|ing|s|ed)? — "or" then \\b failed on the
    trailing "s", so every clause using the plural was invisible. Section B of
    the checklist is built on exactly those clauses."""
    hits = lx.match_categories(
        "The Contractor will ensure that its sub-contractors do not engage in "
        "collusive conduct."
    )
    assert "R4" in {cid for cid, _ in hits}


def test_prohibition_beats_obligation():
    modality, _ = classify_modality("The Contractor shall not offer any gift.")
    assert modality is Modality.PROHIBITION


def test_client_right_is_not_read_as_our_permission():
    modality, _ = classify_modality("The Purchaser reserves the right to terminate the Contract.")
    assert modality is Modality.RIGHT_OF_UN


def test_non_local_exception_scope_is_recorded_as_unresolved(integrity):
    _doc, _clauses, reqs = integrity
    scoped = [e for r in reqs for e in r.exceptions]
    for e in scoped:
        assert isinstance(e.scope_resolved, bool)
        assert e.scope_note
        if not e.scope_resolved:
            # An unresolved carve-out must reach adjudication, not be swallowed.
            owner = next(r for r in reqs if e in r.exceptions)
            assert "exception_scope_unresolved" in owner.triggers


# ---------------------------------------------------------------------------
# The line the system does not cross
# ---------------------------------------------------------------------------


def test_status_vocabulary_has_no_clearance():
    values = {s.value for s in Status}
    for forbidden in ("cleared", "clear", "satisfied", "pass", "passed", "compliant"):
        assert forbidden not in values


def test_checklist_prohibitions_are_all_enforced_in_code():
    """config/checklist.example.yaml claims these are enforced in code, not by convention."""
    checklist = load_checklist(CHECKLIST)
    assert checklist["prohibited_actions"]


def test_unknown_prohibition_stops_the_run():
    _assert_enforceable({"prohibited_actions": ["issue_compliance_clearance"]})
    with pytest.raises(ChecklistError):
        _assert_enforceable({"prohibited_actions": ["do_something_we_cannot_enforce"]})


def test_never_auto_clear_items_never_report_evidence_present(integrity):
    doc, _clauses, reqs = integrity
    checklist = load_checklist(CHECKLIST)
    # Full evidence for items 6, 7 and 9 — they must still land on a human.
    bundle = {
        "6": {"signed_declaration": ["x"], "registry_collision_report": [],
              "attendance_record": [], "conflictscan_report": {}},
        "7": {"signed_declaration": ["x"]},
        "9": {"signed_declaration": ["x"]},
    }
    results = {r.item_id: r for r in adjudicate(checklist, doc, reqs, bundle)}
    for iid in (6, 7, 9):
        assert results[iid].never_auto_clear
        assert results[iid].status is not Status.EVIDENCE_PRESENT
        assert results[iid].status is Status.REQUIRES_HUMAN


def test_missing_evidence_is_unresolved_not_evidence_present(integrity):
    doc, _clauses, reqs = integrity
    checklist = load_checklist(CHECKLIST)
    results = {r.item_id: r for r in adjudicate(checklist, doc, reqs, {})}
    for iid in (1, 2, 3, 5, 8, 13, 14):
        assert results[iid].status is Status.UNRESOLVED
        assert results[iid].notes, "an unchecked item must say why it was not checked"


def test_attestation_states(integrity):
    doc, _clauses, reqs = integrity
    checklist = load_checklist(CHECKLIST)

    missing = {r.item_id: r for r in adjudicate(checklist, doc, reqs, {})}
    assert missing[10].status is Status.UNRESOLVED
    assert "attestation_missing" in missing[10].triggers

    unsigned = {r.item_id: r for r in adjudicate(
        checklist, doc, reqs, {"10": {"attestation": {"signed": False, "attestor": "A"}}})}
    assert unsigned[10].status is Status.FLAG

    signed = {r.item_id: r for r in adjudicate(
        checklist, doc, reqs,
        {"10": {"attestation": {"signed": True, "attestor": "A", "date": "2026-08-20"}}})}
    assert signed[10].status is Status.EVIDENCE_PRESENT


def test_item_12_unresolved_when_nothing_was_extracted(integrity):
    doc, _clauses, _reqs = integrity
    checklist = load_checklist(CHECKLIST)
    results = {r.item_id: r for r in adjudicate(checklist, doc, [], {})}
    assert results[12].status is Status.UNRESOLVED
    assert "extraction_unresolved" in results[12].triggers


def test_client_rules_do_not_advance_items_about_the_firm(integrity):
    """A well-drafted contract must not clear a checklist item about the firm."""
    doc, _clauses, reqs = integrity
    checklist = load_checklist(CHECKLIST)
    results = {r.item_id: r for r in adjudicate(checklist, doc, reqs, {})}
    assert results[3].requirements, "R1 provisions should be attached as context"
    assert results[3].status is Status.UNRESOLVED


def test_all_fifteen_items_are_adjudicated(integrity):
    doc, _clauses, reqs = integrity
    checklist = load_checklist(CHECKLIST)
    results = adjudicate(checklist, doc, reqs, {})
    assert [r.item_id for r in results] == list(range(1, 16))


# ---------------------------------------------------------------------------
# Learning: accumulates freely, changes behaviour only on approval
# ---------------------------------------------------------------------------


def test_ledger_alone_changes_nothing(tmp_path):
    from cgate import feedback as fb

    store = str(tmp_path)
    before = copy.deepcopy(lx.CATEGORIES["R5"]["anchors"])
    for _ in range(5):
        fb.record(store, fb.Decision(kind="reject", category="R5",
                                     matched=["thing of value"], quote="q"))
    assert fb.proposals(fb.load_ledger(store)), "5 rejections should support a proposal"
    assert fb.apply_overrides(store) == "0"
    assert lx.CATEGORIES["R5"]["anchors"] == before


def test_approval_bumps_the_version_and_records_the_approver(tmp_path):
    from cgate import feedback as fb

    store = str(tmp_path)
    for _ in range(4):
        fb.record(store, fb.Decision(kind="reject", category="R9",
                                     matched=["security"], quote="q"))
    props = fb.proposals(fb.load_ledger(store))
    assert props
    version = fb.approve(store, props[:1], approver="Compliance Officer")
    assert version == "1"

    data = fb.load_overrides(store)
    assert data["history"][-1]["approved_by"] == "Compliance Officer"
    assert data["history"][-1]["changes"]


def test_calibration_is_measurement_only(tmp_path):
    from cgate import feedback as fb

    store = str(tmp_path)
    for _ in range(3):
        fb.record(store, fb.Decision(kind="confirm", category="R1", quote="q"))
    fb.record(store, fb.Decision(kind="reject", category="R1", quote="q"))
    fb.record(store, fb.Decision(kind="miss", category="R1", quote="q"))

    c = fb.calibration(fb.load_ledger(store))["R1"]
    assert c.confirmed == 3 and c.rejected == 1 and c.missed == 1
    assert c.precision == pytest.approx(0.75)
    assert c.recall == pytest.approx(0.75)


def test_a_miss_seen_once_is_not_a_pattern(tmp_path):
    from cgate import feedback as fb

    store = str(tmp_path)
    fb.record(store, fb.Decision(kind="miss", category="R5",
                                 quote="any gifts, favours or hospitality"))
    assert not [p for p in fb.proposals(fb.load_ledger(store)) if p.kind == "add_anchor"]


# ---------------------------------------------------------------------------
# The benchmark is part of the contract with the reviewer
# ---------------------------------------------------------------------------


def test_bench_passes_and_the_gate_drops_nothing():
    from cgate.bench import run_fixture
    import glob

    for path in sorted(glob.glob(os.path.join(FIXTURES, "*.txt"))):
        res = run_fixture(path)
        assert res.dropped == 0, (
            f"{res.name}: the grounding gate dropped a rule-derived finding, which "
            f"means an offset bug in extract.spans_for — {res.drop_reasons}"
        )
        assert not res.misses, f"{res.name} regressed: {[m['quote'] for m in res.misses]}"
        assert len(res.false_fires) <= res.max_false_fires, (
            f"{res.name} fired on forbidden categories {res.false_fires}"
        )


def test_review_json_is_serialisable(integrity):
    from cgate.model import ReviewResult

    doc, _clauses, reqs = integrity
    checklist = load_checklist(CHECKLIST)
    result = ReviewResult(
        doc_id=doc.doc_id, doc_path=doc.path, checklist_version="1.0",
        tool_version="test", generated_at="now",
        requirements=reqs, items=adjudicate(checklist, doc, reqs, {}),
    )
    payload = json.loads(result.to_json())
    assert len(payload["items"]) == 15
    assert payload["requirements"][0]["modality"] in {m.value for m in Modality}
