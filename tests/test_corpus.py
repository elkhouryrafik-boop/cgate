"""
Tests for the reference corpus.

None of these touch the network. A test suite that fetches is a test suite that
fails on a train, and the fetch itself is the least interesting part: what has
to hold is that a held instrument can be traced back to a publisher and a
moment, that a title match never becomes a claim to have read the document, and
that a publisher re-saving a PDF is not reported as the document changing.

The last of those is not fussiness. `verify --remote` exists to catch an
instrument moving under a submission that cites it — the ISO 37001:2016 case.
A check that shouts at every re-render is a check people stop reading, and then
it catches nothing at all.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cgate import corpus as cp

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


@pytest.fixture
def fake(tmp_path):
    """A corpus root with a manifest and no fetching."""
    entries = [
        dict(id="example-gtc-services-2025",
             title="General Terms and Conditions of Contract (Services)",
             issuer="Exemplar", category="general_conditions",
             url="https://example.invalid/a.pdf", licence="public_access",
             edition_hint="version date 10 March 2025", sha256="a" * 64,
             retrieved_at="2026-08-30T08:00:00Z", filename="a.pdf", bytes=1000,
             declared="VERSION DATE: 10 March 2025 GENERAL TERMS AND CONDITIONS"),
        dict(id="example-gtc-services-prior",
             title="General Terms and Conditions of Contract (Services), archived edition",
             issuer="Exemplar", category="general_conditions",
             url="https://example.invalid/b.pdf", licence="public_access",
             edition_hint="ARCHIVE, pre-10 March 2025", sha256="b" * 64,
             retrieved_at="2026-08-30T08:00:00Z", filename="b.pdf", bytes=1000,
             declared="ARCHIVE PREVIOUS GTCS"),
        dict(id="sampleton-gc-services-2024",
             title="General Conditions of Contract — Provision of Services",
             issuer="Sampleton", category="general_conditions",
             url="https://example.invalid/c.pdf", licence="public_access",
             edition_hint="Sep. 2024 V2", sha256="c" * 64,
             retrieved_at="2026-08-30T08:00:00Z", filename="c.pdf", bytes=1000,
             declared="General Conditions of Contract"),
        dict(id="un-supplier-code-of-conduct",
             title="Example Supplier Code of Conduct",
             issuer="Example Secretariat", category="integrity",
             url="https://example.invalid/d.pdf", licence="public_access",
             edition_hint="Rev.07, September 2024", sha256="d" * 64,
             retrieved_at="2026-08-30T08:00:00Z", filename="d.pdf", bytes=1000,
             declared="Example Supplier Code of Conduct Rev.07"),
    ]
    (tmp_path / "manifest.json").write_text(
        json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
    return str(tmp_path)


# ---------------------------------------------------------------------------
# The shipped source list
# ---------------------------------------------------------------------------


def test_every_source_declares_a_licence_and_a_reason():
    sources, unavailable = cp.load_sources(os.path.join(ROOT, "corpus"))
    assert sources
    for s in sources:
        assert s.get("licence"), f"{s['id']} has no licence field"
        assert s.get("why"), f"{s['id']} does not say why it is held"
        assert s["url"].startswith("https://"), f"{s['id']} is not fetched over TLS"
    # The corpus must never read as complete. Instruments that bind the firm
    # and cannot be fetched are listed, with what they block.
    assert unavailable
    for u in unavailable:
        assert u["licence"] == "licensed_purchase_required"
        assert u.get("blocks")


def test_source_ids_are_unique():
    sources, unavailable = cp.load_sources(os.path.join(ROOT, "corpus"))
    ids = [s["id"] for s in sources] + [u["id"] for u in unavailable]
    assert len(ids) == len(set(ids))


def test_iso_37001_is_named_as_a_gap_rather_than_quietly_absent():
    # Reviews may rest on it and it cannot be fetched. A corpus that
    # simply omitted it would read as though nothing were missing.
    _, unavailable = cp.load_sources(os.path.join(ROOT, "corpus"))
    assert any("37001" in u["id"] for u in unavailable)


# ---------------------------------------------------------------------------
# Matching: offers candidates, never decides
# ---------------------------------------------------------------------------


def test_a_generic_name_returns_every_plausible_instrument(fake):
    found = cp.candidates("General Terms and Conditions", root=fake)
    assert len(found) > 1
    assert cp.lookup("General Terms and Conditions", root=fake) is None


def test_the_issuer_narrows_it(fake):
    found = cp.candidates("General Conditions of Contract", issuer_hint="Sampleton",
                          root=fake)
    assert [e.id for e in found] == ["sampleton-gc-services-2024"]


def test_a_live_edition_outranks_an_archived_one(fake):
    found = cp.candidates("General Terms and Conditions", issuer_hint="Exemplar",
                          root=fake)
    assert found[0].id == "example-gtc-services-2025"


def test_an_unrelated_policy_matches_nothing(fake):
    # "Exemplar" alone must not pair a safeguarding policy with terms of contract.
    assert cp.candidates("Exemplar's Policy on Safeguarding", root=fake) == []
    assert cp.candidates("Exemplar's Policy Prohibiting and Combatting Fraud "
                         "and Corruption", root=fake) == []


def test_a_distinctive_name_resolves_to_one(fake):
    e = cp.lookup("UN Supplier Code of Conduct", root=fake)
    assert e is not None and e.id == "un-supplier-code-of-conduct"


def test_an_instrument_we_do_not_hold_returns_nothing(fake):
    assert cp.candidates("Financial Regulations and Rules", root=fake) == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Exemplar invites", "Exemplar"),
        ("Sampleton, Capital City", "Sampleton"),
        ("Fictional Development Foundation, Exampleland", None),   # not ours
        ("Exemplar and Sampleton jointly", None),                    # ambiguous
    ],
)
def test_issuer_is_read_off_the_document(text, expected):
    assert cp.issuer_in(text) == expected


# ---------------------------------------------------------------------------
# Re-issue is not revision
# ---------------------------------------------------------------------------


def test_a_resaved_file_is_not_an_edition_change():
    same = "Exemplar Policy on Anti-Fraud and Corruption POLICY/XX/2025/001 v1.0"
    assert cp._same_declaration(same, same.replace("  ", " "))
    assert cp._same_declaration(same, same.upper())


def test_a_new_edition_is_an_edition_change():
    assert not cp._same_declaration(
        "Example Supplier Code of Conduct Rev.07 September 2024",
        "Example Supplier Code of Conduct Rev.08 January 2027",
    )


def test_two_empty_first_pages_are_not_evidence_of_sameness():
    # Scanned covers extract to nothing. Treating that as agreement would make
    # every image-only PDF permanently unchanged.
    assert not cp._same_declaration("", "")


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_the_shipped_manifest_records_provenance_for_every_entry():
    path = os.path.join(ROOT, "corpus", "manifest.json")
    if not os.path.exists(path):
        pytest.skip("corpus not fetched in this checkout")
    for e in cp.load_manifest(os.path.join(ROOT, "corpus")):
        if not e.ok():
            continue
        assert len(e.sha256) == 64
        assert e.retrieved_at.endswith("Z")
        assert e.final_url.startswith("https://")
        assert e.bytes > 0
        assert e.licence


def test_fetched_files_are_not_tracked_by_git():
    # The publishers serve these without authentication. That is not a licence
    # to redistribute them, and a repository is redistribution.
    gitignore = os.path.join(ROOT, ".gitignore")
    if not os.path.exists(gitignore):
        pytest.skip("no tool-level .gitignore in this checkout")
    assert "corpus/files/" in open(gitignore, encoding="utf-8").read()
