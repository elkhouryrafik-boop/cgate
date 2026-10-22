"""
Tests for cgate.conflict — shared-content and shared-origin detection.

This is the module that answers undisclosed content-sharing directly: the firm's submission
and its subcontractor's independent submission "contained sections and
photographs that were identical". Text overlap and image hashing were already
covered by the module's own design; these tests cover the document-property
comparison added alongside them, and the invariant all three share — an
unreadable source is never reported as a source with nothing in it.
"""

from __future__ import annotations

import docx
import pytest

from cgate import conflict


def make_docx(path, *, author=None, last_modified_by=None, title=None,
              paragraphs=("Some ordinary text.",)):
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    cp = d.core_properties
    if author is not None:
        cp.author = author
    if last_modified_by is not None:
        cp.last_modified_by = last_modified_by
    if title is not None:
        cp.title = title
    d.save(str(path))
    return str(path)


# ---------------------------------------------------------------------------
# Reading properties
# ---------------------------------------------------------------------------


def test_reads_docx_properties(tmp_path):
    p = make_docx(tmp_path / "a.docx", author="S. Quill", title="Technical Proposal")
    meta, errors = conflict.read_metadata(p)
    assert errors == []
    assert meta["author"] == "S. Quill"
    assert meta["title"] == "Technical Proposal"


def test_unreadable_file_is_an_error_not_an_empty_dict(tmp_path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"this is not a docx")
    meta, errors = conflict.read_metadata(str(p))
    assert meta == {}
    assert errors and errors[0].error_type == "metadata_unreadable"
    assert errors[0].retryable is True


def test_unsupported_filetype_is_reported(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello", encoding="utf-8")
    meta, errors = conflict.read_metadata(str(p))
    assert meta == {}
    assert errors[0].error_type == "unsupported_filetype"
    assert errors[0].retryable is False


@pytest.mark.parametrize("junk", ["", "  ", "Unknown", "Microsoft Office User",
                                  "Administrator", "windows user"])
def test_placeholder_authors_carry_no_information(junk):
    assert conflict._clean_meta(junk) == ""


# ---------------------------------------------------------------------------
# Comparing properties
# ---------------------------------------------------------------------------


def test_shared_author_is_a_strong_match():
    a = {"author": "S. Quill", "title": "Proposal A"}
    b = {"author": "s. quill", "title": "Proposal B"}
    matches = conflict._metadata_matches(a, b)
    assert len(matches) == 1
    assert matches[0].field == "author"
    assert matches[0].weight == "strong"


def test_shared_producer_alone_is_weak():
    """Two firms using the same version of Word is unremarkable."""
    a = {"producer": "Microsoft® Word 2021", "author": "One Person"}
    b = {"producer": "Microsoft® Word 2021", "author": "Another Person"}
    matches = conflict._metadata_matches(a, b)
    assert [m.field for m in matches] == ["producer"]
    assert matches[0].weight == "weak"


def test_two_empty_documents_do_not_match_on_nothing():
    """The failure mode that would make this whole check useless."""
    assert conflict._metadata_matches({"author": ""}, {"author": ""}) == []
    assert conflict._metadata_matches({}, {}) == []


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_scan_flags_shared_origin(tmp_path):
    shared = ["Methodology. " + "The consultant shall mobilise a survey team. " * 20]
    a = make_docx(tmp_path / "ours.docx", author="S. Quill", paragraphs=shared)
    b = make_docx(tmp_path / "theirs.docx", author="S. Quill",
                  paragraphs=shared + ["An extra closing paragraph of their own."])

    result = conflict.scan(a, b)
    assert "metadata_origin_match" in result.triggers
    assert "content_similarity_above_threshold" in result.triggers
    assert any(m["field"] == "author" for m in result.metadata_matches)
    # No verdict field anywhere. The CO determines; this reports.
    assert not hasattr(result, "verdict")


def test_scan_on_unrelated_documents_is_quiet(tmp_path):
    a = make_docx(tmp_path / "a.docx", author="One Person",
                  paragraphs=["Bridge deck rehabilitation across the northern span."])
    b = make_docx(tmp_path / "b.docx", author="Another Person",
                  paragraphs=["Water supply network modelling for the eastern district."])
    result = conflict.scan(a, b)
    assert "metadata_origin_match" not in result.triggers
    assert "exact_passage_match" not in result.triggers


def test_scan_records_both_documents_properties_for_the_reviewer(tmp_path):
    a = make_docx(tmp_path / "a.docx", author="One Person")
    b = make_docx(tmp_path / "b.docx", author="Another Person")
    result = conflict.scan(a, b)
    assert result.metadata["doc_a"]["author"] == "One Person"
    assert result.metadata["doc_b"]["author"] == "Another Person"


def test_unreadable_source_stops_the_scan(tmp_path):
    a = make_docx(tmp_path / "a.docx", author="One Person")
    bad = tmp_path / "b.docx"
    bad.write_bytes(b"not a document")
    result = conflict.scan(a, str(bad))
    assert "source_unreadable" in result.triggers
    assert result.text_similarity == 0.0
    assert result.triggers.count("content_similarity_above_threshold") == 0
