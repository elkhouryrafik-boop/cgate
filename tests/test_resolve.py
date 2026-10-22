"""
Tests for cgate.resolve — is the instrument this contract incorporates
actually in front of us?

Both directions matter, and they are not symmetric. `resolve.py`'s own
docstring states the asymmetry:

    A false "not attached" is the more dangerous error [for usability] —
    it sends a reviewer looking for a document they are already holding.

    A false "attached" is quieter and worse. It would tell the Compliance
    Officer that the content of an instrument is known when it is not, which
    is exactly the assumption checklist item 12 exists to prevent.

So every loosening of the title matcher needs a matching test on the refusing
side. The tests below are paired deliberately: the ones under "must attach"
are the real policy documents the tender incorporates, and the ones
under "must still refuse" are the shapes that would let some unrelated
all-caps text get falsely credited as satisfying an incorporation.
"""

from __future__ import annotations

import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cgate.model import Block, Clause, Document, SpanRef       # noqa: E402
from cgate.resolve import (                                     # noqa: E402
    _looks_like_a_title,
    dedupe_instruments,
    resolve_incorporation,
)


# ---------------------------------------------------------------------------
# helpers — build a Document/Clause pair without touching the filesystem
# ---------------------------------------------------------------------------


def make_doc(path: str, blocks: list[tuple[str, str]], doc_id: str = "d" * 64) -> Document:
    """blocks: [(kind, text), ...] in reading order."""
    d = Document(doc_id=doc_id, path=path, filetype="pdf", n_pages=1)
    for i, (kind, text) in enumerate(blocks):
        d.blocks.append(Block(f"p1.c0.b{i}", 1, 0, i, text, (0, 0, 0, 0), kind))
    d.build_index()
    return d


def filler(n: int, word: str = "content") -> list[tuple[str, str]]:
    """Enough following blocks that a title reads as a document, not a mention.

    resolve.MIN_FOLLOWING_BLOCKS is 10; these are the body that follows a real
    title page.
    """
    return [("body", f"{word} paragraph {i} with enough text to be substantive.") for i in range(n)]


def citing_clause(doc: Document, instrument: str, url: str | None = None) -> tuple[Document, list[Clause]]:
    """A clause in `doc` that incorporates `instrument` by reference."""
    block = doc.blocks[0]
    clause = Clause(
        clause_id="c1",
        number="1.2",
        heading=None,
        path=["1.2"],
        text=block.text,
        spans=[SpanRef(doc.doc_id, block.block_id, 0, len(block.text))],
        page_start=1,
        page_end=1,
        external_refs=[{
            "instrument": instrument,
            "incorporation_marker": "shall comply with",
            "attached": False,
            "url": url,
        }],
    )
    return (doc, [clause])


def resolve_against(instrument: str, annex_blocks: list[tuple[str, str]],
                    annex_path: str = "annex.pdf") -> tuple[bool, list[str]]:
    """Cite `instrument` from a tender, offer `annex_blocks` as the annex.
    Returns (was_attached, notes)."""
    tender = make_doc("tender.pdf", [
        ("body", f"1.2 The Contractor shall comply with the {instrument}, which is "
                 "incorporated into this Contract by reference."),
    ] + filler(12, "tender"), doc_id="a" * 64)
    annex = make_doc(annex_path, annex_blocks, doc_id="b" * 64)

    doc_clauses = citing_clause(tender, instrument)
    notes = resolve_incorporation([tender, annex], [doc_clauses, (annex, [])])
    ref = doc_clauses[1][0].external_refs[0]
    return bool(ref.get("attached")), notes


# ---------------------------------------------------------------------------
# must attach — the real documents a tender incorporates
# ---------------------------------------------------------------------------


def test_attaches_a_plain_heading_title():
    """The baseline that already worked: a clean standalone heading."""
    attached, notes = resolve_against(
        "General Terms and Conditions of Contract",
        [("heading", "ANNEX A GENERAL TERMS AND CONDITIONS OF CONTRACT")] + filler(12),
    )
    assert attached is True
    assert any("located in the bundle" in n for n in notes)


def test_attaches_a_title_carrying_a_document_number_and_dates_tail():
    """UN Policy on Safeguarding: its title page runs the title, document
    number, version and effective date together on one line, past the 140-char
    title cap. The identifying words are still at the front."""
    long_title = (
        "UN POLICY ON SAFEGUARDING Document number: POLICY/XX/2024/001 "
        "v. 6 June 2025 Effective date: 6 June 2025 Mandatory review date: 6 June 2029 "
        "Approved by: the Executive Director Business owner: Division of Human Resources"
    )
    assert len(long_title) > 140, "fixture must exercise the over-cap path"

    attached, _notes = resolve_against(
        "UN's Policy on Safeguarding",
        [("heading", long_title)] + filler(12, "safeguarding"),
    )
    assert attached is True


def test_attaches_a_title_glued_to_the_front_of_a_body_paragraph():
    """UN Supplier Code of Conduct: the PDF has no isolated title block — the
    title runs straight into the first paragraph of body text."""
    glued = (
        "UN SUPPLIER CODE OF CONDUCT A. Principles: The values enshrined "
        "in the founding charter — respect for fundamental human rights, social "
        "justice and human dignity, and respect for the equal rights of men and women — "
        "serve as the overarching framework for this Code."
    )
    attached, _notes = resolve_against(
        "UN Supplier Code of Conduct",
        [("body", glued)] + filler(12, "supplier code"),
    )
    assert attached is True


# ---------------------------------------------------------------------------
# must still refuse — the dangerous direction (§ item 12)
# ---------------------------------------------------------------------------


def test_refuses_when_the_instrument_is_simply_absent():
    attached, notes = resolve_against(
        "UN's Policy on Safeguarding",
        [("heading", "ANNEX 2 PRICE SCHEDULE")] + filler(12, "pricing"),
    )
    assert attached is False
    assert any("NOT in the bundle" in n for n in notes)


def test_refuses_a_title_with_nothing_substantial_following_it():
    """Naming an instrument in a list is not attaching it. A title block with
    fewer than MIN_FOLLOWING_BLOCKS after it is a mention."""
    attached, _notes = resolve_against(
        "UN Supplier Code of Conduct",
        [
            ("heading", "DOCUMENTS INCORPORATED BY REFERENCE"),
            ("body", "UN SUPPLIER CODE OF CONDUCT"),
            ("body", "UN Policy on Safeguarding"),
        ],
    )
    assert attached is False


def test_refuses_a_bare_mention_inside_a_list_of_annexes():
    """The exact shape the loosened body-block path could get wrong: an
    all-caps instrument name at the start of a line that is a list entry, not
    a document."""
    attached, _notes = resolve_against(
        "UN Supplier Code of Conduct",
        [
            ("heading", "ANNEX 2 PRICE SCHEDULE"),
            ("body", "UN SUPPLIER CODE OF CONDUCT is available at www.example.invalid and forms "
                     "part of this solicitation by reference only."),
        ] + filler(12, "pricing"),
    )
    assert attached is False, (
        "a one-line reference inside another document must never count as that "
        "document being attached"
    )


def test_refuses_when_only_the_citing_sentence_itself_matches():
    """The clause that cites an instrument must not satisfy its own citation."""
    tender = make_doc("tender.pdf", [
        ("heading", "UN SUPPLIER CODE OF CONDUCT"),
    ] + filler(12, "tender"), doc_id="a" * 64)

    block = tender.blocks[0]
    clause = Clause(
        clause_id="c1", number="1.2", heading=None, path=["1.2"], text=block.text,
        spans=[SpanRef(tender.doc_id, block.block_id, 0, len(block.text))],
        page_start=1, page_end=1,
        external_refs=[{
            "instrument": "UN Supplier Code of Conduct",
            "incorporation_marker": "shall comply with",
            "attached": False,
        }],
    )
    resolve_incorporation([tender], [(tender, [clause])])
    assert not clause.external_refs[0].get("attached")


def test_refuses_an_unrelated_all_caps_run_at_the_front_of_a_paragraph():
    """Loosening must not let any capitalised opening phrase match anything."""
    attached, _notes = resolve_against(
        "UN's Policy on Safeguarding",
        [("body", "IMPORTANT NOTICE TO BIDDERS: submissions received after the closing "
                  "date will be rejected without consideration by the evaluation panel.")]
        + filler(12, "notice"),
    )
    assert attached is False


# ---------------------------------------------------------------------------
# _looks_like_a_title unit level
# ---------------------------------------------------------------------------


def test_looks_like_a_title_accepts_headings_and_caps_lines():
    assert _looks_like_a_title("Annex A General Terms", "heading") is True
    assert _looks_like_a_title("GENERAL TERMS AND CONDITIONS", "body") is True


def test_looks_like_a_title_rejects_ordinary_prose():
    assert _looks_like_a_title(
        "The Contractor shall comply with all applicable laws and regulations.", "body"
    ) is False


def test_looks_like_a_title_rejects_a_line_with_no_letters():
    assert _looks_like_a_title("1.2.3 — 4.5.6", "body") is False


# ---------------------------------------------------------------------------
# dedupe_instruments — one document, cited two ways, is one escalation
# ---------------------------------------------------------------------------


def test_dedupe_collapses_a_contained_name():
    out = dedupe_instruments({
        "UN Supplier Code of Conduct": "www.example.invalid",
        "Supplier Code of Conduct": None,
    })
    assert list(out) == ["UN Supplier Code of Conduct"]


def test_dedupe_keeps_genuinely_different_instruments():
    out = dedupe_instruments({
        "UN Supplier Code of Conduct": None,
        "UN's Policy on Safeguarding": None,
    })
    assert len(out) == 2
