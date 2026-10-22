"""
Tests for the compiler.

The claim this module has to keep true is that nothing here consults a list of
subjects. A quantity is compiled because it is a quantity; a sentence is
compiled because it carries a modal. Every test below is written against a
document about something the tool has never heard of, so that a test passing
for the wrong reason — because the fixture happens to talk about bribery — is
not possible.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cgate.model import Block, Document, Modality, Actor
from cgate.spec import (
    _literals,
    compile_spec,
    content_stems,
    modality_of,
    actor_of,
    section_at,
    sections,
    sentences,
    stem,
    subject_of,
    tables,
)


def doc_of(*rows: tuple[str, str]) -> Document:
    """Build a Document from (kind, text) pairs, in reading order."""
    d = Document(doc_id="d" * 64, path="memo.docx", filetype="docx", n_pages=1)
    for i, (kind, text) in enumerate(rows):
        bid = f"d.t{i}.r0" if kind == "table" else f"d.b{i}"
        d.blocks.append(Block(bid, 1, 0, i, text, (0, 0, 0, 0), kind))
    d.build_index()
    return d


# ---------------------------------------------------------------------------
# Stemming: small enough to reproduce by hand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word,expected",
    [
        ("validity", "valid"),
        ("valid", "valid"),
        ("days", "day"),
        ("day", "day"),
        ("turnover", "turnover"),
        ("demonstrating", "demonstrat"),
        ("demonstrate", "demonstrat"),
        ("average", "averag"),
        ("less", "less"),          # -ss is never stripped
        ("policies", "policy"),
    ],
)
def test_stem_pairs_that_must_collide(word, expected):
    assert stem(word) == expected


def test_stopwords_and_short_words_are_not_stems():
    assert content_stems("of the a shall must be in") == []


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------


def test_money_percent_duration_date_and_count():
    text = ("A bidder must hold cover of USD 1,500,000, pay 0.5% per week for "
            "ninety (90) days from 24 April 2027, and complete two (2) audits.")
    kinds = {k: (v, u, raw) for _, _, k, v, u, raw in _literals(text)}
    assert kinds["money"][0] == 1_500_000.0
    assert kinds["money"][1] == "USD"
    assert kinds["percent"][0] == 0.5
    assert kinds["duration"][0] == 90.0
    assert kinds["date"][2] == "24 April 2027"
    assert kinds["count"][0] == 2.0


def test_numbers_that_address_the_document_are_not_quantities():
    for text in ("as set out in Section 4.2 above",
                 "see Annex 3 for the form",
                 "Article 6.8 applies",
                 "the 2024 edition of the manual"):
        assert _literals(text) == [], text


def test_a_worded_number_and_its_digits_are_one_literal_not_two():
    lits = _literals("a bid security of USD 5,000 (five thousand United States Dollars)")
    assert len([x for x in lits if x[2] == "money"]) == 1


def test_subject_is_ordered_by_proximity_to_the_number():
    text = "A bidder must demonstrate a minimum average annual turnover of USD 750,000."
    s, e = text.index("USD"), text.index("USD") + len("USD 750,000")
    subject = subject_of(text, s, e)
    assert subject[0] == "turnover"
    assert subject.index("turnover") < subject.index("bidder")


def test_two_phrasings_of_one_quantity_share_their_nearest_words():
    a = "A bidder must demonstrate a minimum average annual turnover of USD 750,000"
    b = "statements demonstrating average annual turnover of not less than USD 900,000"
    sa = subject_of(a, a.index("USD"), len(a))
    sb = subject_of(b, b.index("USD"), len(b))
    assert {"turnover", "annual", "averag"} <= set(sa[:4]) & set(sb[:4])


# ---------------------------------------------------------------------------
# Sentences
# ---------------------------------------------------------------------------


def test_a_full_stop_inside_a_number_does_not_end_a_sentence():
    text = "Cover of USD 1,500,000. The Consultant shall submit evidence."
    assert len(sentences(text)) == 2


def test_offsets_resolve_back_to_the_source():
    text = "One sentence here. Another sentence follows."
    for s, e, sent in sentences(text):
        assert text[s:e] == sent


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def test_section_kind_is_read_off_the_heading():
    d = doc_of(
        ("heading", "5. Mandatory Submission Requirements"),
        ("body", "Each proposal shall comprise the following."),
        ("heading", "6.4 Financial Evaluation"),
        ("body", "Proposals in another currency will be treated as non-responsive."),
    )
    secs = sections(d)
    assert [s.kind for s in secs] == ["submission", "evaluation"]


def test_an_unrecognised_subsection_inherits_its_parent():
    d = doc_of(
        ("heading", "7. Contract Terms and Conditions"),
        ("heading", "7.9 Sundry Matters of No Obvious Name"),
        ("body", "The Consultant shall do something."),
    )
    secs = sections(d)
    assert secs[1].kind == secs[0].kind


def test_section_at_finds_the_block_owner():
    d = doc_of(
        ("heading", "4. Eligibility and Qualification Criteria"),
        ("body", "Bidders must be legally constituted."),
    )
    assert section_at(sections(d), 1).kind == "eligibility"


# ---------------------------------------------------------------------------
# Modality and actor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence,expected",
    [
        ("The Consultant shall maintain cover.", Modality.OBLIGATION),
        ("The bidder shall not subcontract.", Modality.PROHIBITION),
        ("The Foundation may cancel the process.", Modality.PERMISSION),
        ("To be eligible, a bidder must be registered.", Modality.OBLIGATION),
        ("Rehabilitation began in 1978.", Modality.UNKNOWN),
    ],
)
def test_modality(sentence, expected):
    assert modality_of(sentence) is expected


def test_a_sentence_that_voids_a_bid_is_binding_without_a_modal():
    # The blind-test currency rule carried no modal verb and ended the bid.
    s = "Proposals quoted in another currency will be treated as non-responsive."
    assert modality_of(s) is not Modality.UNKNOWN


def test_actor_is_taken_from_the_words_before_the_modal():
    # The object of the duty names the client; the subject is still us.
    s = "The Consultant shall not vary the works without the Foundation's approval."
    assert actor_of(s) is Actor.CONTRACTOR


# ---------------------------------------------------------------------------
# List stems
# ---------------------------------------------------------------------------


def test_a_list_item_inherits_the_modal_of_its_stem():
    d = doc_of(
        ("heading", "3.2 Part B - Supervision"),
        ("body", "Following award, the Consultant shall:"),
        ("body", "8. Maintain cover of not less than USD 1,500,000 per claim."),
    )
    spec = compile_spec(d)
    item = [c for c in spec.commitments if "Maintain cover" in c.text]
    assert item, "the list item compiled to no commitment"
    assert item[0].modality is Modality.OBLIGATION
    assert item[0].modal_inherited is True
    assert item[0].actor is Actor.CONTRACTOR


def test_an_ordinary_paragraph_ends_the_list():
    d = doc_of(
        ("body", "The Consultant shall:"),
        ("body", "1. Do the first thing at each site."),
        ("body", "The Foundation issues the notice to proceed in due course."),
        ("body", "2. Do the second thing at each site."),
    )
    spec = compile_spec(d)
    second = [c for c in spec.commitments if "second thing" in c.text]
    assert second == [] or second[0].modal_inherited is False


# ---------------------------------------------------------------------------
# Tables and coverage
# ---------------------------------------------------------------------------


def test_consecutive_rows_regroup_into_one_table():
    d = doc_of(
        ("table", "# | Milestone | % of Contract Price"),
        ("table", "1 | Advance payment on signature | 15"),
        ("table", "2 | Acceptance of the report | 20"),
        ("body", "Invoices shall quote the solicitation number."),
        ("table", "Item | Detail"),
        ("table", "Date of issue | 5 October 2026"),
    )
    assert [len(t) for t in tables(d)] == [3, 2]


def test_coverage_names_every_block_that_entered_no_check():
    d = doc_of(
        ("body", "The Consultant shall submit a report."),
        ("body", "Mtara is the capital of Chelwa Province."),
    )
    spec = compile_spec(d)
    assert spec.blocks_total == 2
    assert spec.blocks_compiled == 1
    assert [u["block_id"] for u in spec.uncompiled] == ["d.b1"]


def test_coverage_is_serialised_with_the_spec():
    d = doc_of(("body", "The Consultant shall submit a report."))
    payload = compile_spec(d).to_json()
    assert '"coverage"' in payload
    assert '"blocks_uncompiled"' in payload
