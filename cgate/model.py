"""
cgate.model — canonical document model and finding types.

Design rule that governs this whole file: **every assertion the system makes
must be addressable back to bytes in a source file.** A finding without a
resolvable span is not a weaker finding, it is not a finding at all, and the
grounding gate deletes it.

Stable identifiers
------------------
A `SpanRef` is (doc_id, block_id, start, end) where `start`/`end` are character
offsets into `Block.text`. `doc_id` is the SHA-256 of the source file bytes, so
the same PDF always produces the same identifiers, and a changed PDF never
silently reuses them. This is what makes a review reproducible years later,
which the design commits the firm to.

Discontinuous evidence
----------------------
`Requirement.spans` is a list. The NLP literature measures ~28% of evidence for
contract-level questions as discontinuous — the obligation is in one clause and
its operative limb is somewhere else. A single-span data model cannot represent
that, so it silently truncates the evidence. This one does not.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Source document structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpanRef:
    """A character range inside one block of one document."""

    doc_id: str
    block_id: str
    start: int
    end: int

    def key(self) -> str:
        return f"{self.doc_id[:12]}:{self.block_id}:{self.start}-{self.end}"


@dataclass
class Block:
    """A reading-order unit of text with its physical provenance."""

    block_id: str          # e.g. "p10.c0.b3"  page 10, column 0, block 3
    page: int              # 1-based
    column: int            # 0-based, left to right
    order: int             # position in the document's reading order
    text: str
    bbox: tuple[float, float, float, float]
    kind: str = "body"     # body | heading | header | footer | table | list


@dataclass
class Document:
    doc_id: str
    path: str
    filetype: str
    n_pages: int
    blocks: list[Block] = field(default_factory=list)
    # Ingest-time observations that downstream stages are entitled to act on.
    notes: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    _index: dict[str, Block] = field(default_factory=dict, repr=False)

    def build_index(self) -> None:
        self._index = {b.block_id: b for b in self.blocks}

    def block(self, block_id: str) -> Block | None:
        if not self._index:
            self.build_index()
        return self._index.get(block_id)

    def text_of(self, span: SpanRef) -> str | None:
        """Resolve a span to its literal source text, or None if unresolvable."""
        if span.doc_id != self.doc_id:
            return None
        b = self.block(span.block_id)
        if b is None:
            return None
        if not (0 <= span.start < span.end <= len(b.text)):
            return None
        return b.text[span.start : span.end]

    def full_text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)


# ---------------------------------------------------------------------------
# Clause segmentation
# ---------------------------------------------------------------------------


@dataclass
class Clause:
    """A numbered or otherwise bounded provision.

    `number` is the document's own label ("6.2", "3.2.2.14", "(a)"). `path` is
    the resolved hierarchy, so a reference elsewhere to "Article 6.8" can be
    matched against it.
    """

    clause_id: str
    number: str | None
    heading: str | None
    path: list[str]
    text: str
    spans: list[SpanRef]
    page_start: int
    page_end: int
    depth: int = 0
    parent_id: str | None = None
    # cross-references this clause makes to other clauses or to outside documents
    internal_refs: list[str] = field(default_factory=list)
    external_refs: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------


class Modality(str, Enum):
    OBLIGATION = "obligation"        # shall / must / will / undertakes to
    PROHIBITION = "prohibition"      # shall not / must not / may not / is prohibited
    PERMISSION = "permission"        # may / is entitled to
    RIGHT_OF_UN = "right_of_un"      # the purchaser reserves the right / may require
    CONDITION = "condition"          # eligibility precondition, warranty, certification
    UNKNOWN = "unknown"


class Actor(str, Enum):
    CONTRACTOR = "contractor"        # the firm, bidder, supplier, offeror
    SUBCONTRACTOR = "subcontractor"  # incl. agents, intermediaries, consortium members
    PERSONNEL = "personnel"
    UN = "un"                        # the organisation
    BOTH = "both"
    UNKNOWN = "unknown"


class Status(str, Enum):
    """Deterministic adjudication outcome for a checklist item.

    There is deliberately no `cleared` and no `satisfied`. The system reports
    what the evidence shows; the Compliance Officer clears. See
    docs/ARCHITECTURE.md.
    """

    EVIDENCE_PRESENT = "evidence_present"
    FLAG = "flag"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE = "not_applicable"
    REQUIRES_HUMAN = "requires_human"


@dataclass
class Exception_:
    """A carve-out that narrows a requirement.

    `scope_resolved` is the field that matters. The measured failure mode of
    language models on legal negation is not that they miss the word "except" —
    it is that they cannot tell how far the exception reaches, and degrade from
    ~91% to ~40% on local scope and to ~0% on non-local scope. So the system
    records scope resolution as an explicit, falsifiable state. Unresolved scope
    escalates; it never silently narrows or silently ignores the carve-out.
    """

    marker: str
    text: str
    spans: list[SpanRef]
    scope_resolved: bool
    scope_note: str = ""
    target_ref: str | None = None    # clause the exception points at, if non-local


@dataclass
class Requirement:
    req_id: str
    category: str                    # R1..R15, see lexicon.CATEGORIES
    modality: Modality
    actor: Actor
    text: str                        # verbatim, assembled from spans
    spans: list[SpanRef]
    clause_id: str | None
    clause_number: str | None
    page: int
    exceptions: list[Exception_] = field(default_factory=list)
    incorporation: dict | None = None   # {"target":..., "retrieved":bool, "url":...}
    checklist_items: list[int] = field(default_factory=list)
    extractor: str = "rules"            # rules | llm | rules+llm
    triggers: list[str] = field(default_factory=list)
    evidence_quote: str = ""            # what the grounding gate verified
    # The surface forms that caused this match. Without them a reviewer's
    # rejection tells the feedback ledger that something was wrong but not what,
    # and nothing can be learned from it. See cgate.feedback.
    matched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["modality"] = self.modality.value
        d["actor"] = self.actor.value
        return d


@dataclass
class ItemResult:
    """Adjudication of one of the 15 checklist items."""

    item_id: int
    section: str
    text: str
    handling: str
    status: Status
    triggers: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)   # req_ids
    notes: list[str] = field(default_factory=list)
    never_auto_clear: bool = False


@dataclass
class ReviewResult:
    doc_id: str
    doc_path: str
    checklist_version: str
    tool_version: str
    generated_at: str
    requirements: list[Requirement] = field(default_factory=list)
    items: list[ItemResult] = field(default_factory=list)
    ingest_notes: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    # Ways the client's own document fails to agree with itself, and how much of
    # it the compiler read. Both are about the solicitation rather than about the
    # firm, so neither bears on a checklist status; both belong in the file the
    # proposal team works from. See spec.py and lint.py.
    defects: list["Defect"] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "doc_id": self.doc_id,
                "doc_path": self.doc_path,
                "checklist_version": self.checklist_version,
                "tool_version": self.tool_version,
                "generated_at": self.generated_at,
                "ingest_notes": self.ingest_notes,
                "errors": self.errors,
                "stats": self.stats,
                "defects": [d.to_dict() for d in self.defects],
                "coverage": self.coverage,
                "requirements": [r.to_dict() for r in self.requirements],
                "items": [
                    {**asdict(i), "status": i.status.value} for i in self.items
                ],
            },
            indent=2,
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# The compiled specification
# ---------------------------------------------------------------------------
#
# Everything above this line answers "which provisions of this document are
# about a subject I was told to care about?" That question has a lexicon on one
# side of it, and a lexicon is a ceiling: a provision the list does not name is
# not scored low, it is never seen.
#
# The types below answer a different question — "what does this document
# require, and does it agree with itself?" — which has no lexicon in it. A
# quantity is a quantity whatever it measures; a column either totals or it does
# not; a requirement stated once in an annex is stated once in an annex whether
# or not its subject appears on anybody's list. See spec.py and lint.py.


@dataclass
class Measurand:
    """One quantity as the document states it, with what it measures.

    `subject` is the stemmed content words around the literal. Two measurands
    describe the same thing when their subjects overlap; that is the whole
    mechanism, and it is deliberately not a list of known quantities.
    """

    m_id: str
    kind: str                     # money | percent | duration | count | date
    value: float                  # canonical: money in currency units, percent
                                  # 0-100, duration in days, date as ordinal
    unit: str                     # USD | % | day | year | contract | ...
    raw: str                      # the literal exactly as written
    subject: list[str]
    spans: list[SpanRef]
    block_id: str
    section: str
    section_kind: str
    quote: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Commitment:
    """One binding sentence, with where it sits and what it would take to meet it.

    A commitment is not a category match. Any sentence carrying a modal and an
    actor becomes one, whatever it is about, which is what lets the checks below
    find requirements nobody thought to look for.
    """

    c_id: str
    modality: Modality
    actor: Actor
    text: str
    spans: list[SpanRef]
    block_id: str
    section: str
    section_kind: str
    lemmas: list[str]
    disqualifying: bool = False
    roles: list[str] = field(default_factory=list)
    measurands: list[str] = field(default_factory=list)
    modal_inherited: bool = False   # the modal came from a list stem, not this
                                    # sentence — "the Consultant shall:" above
    quote: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["modality"] = self.modality.value
        d["actor"] = self.actor.value
        return d


@dataclass
class Defect:
    """A way in which the document fails to agree with itself.

    Not a compliance finding and not a determination. It is a statement about
    the client's document, quoted back at the clauses that disagree, for a
    proposal team and a Compliance Officer to act on.
    """

    defect_id: str
    check: str                    # value_conflict | table_sum | ...
    severity: str                 # high | medium | low
    title: str
    detail: str
    spans: list[SpanRef]
    quotes: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    resolution: str = ""          # where the document itself settles the
                                  # disagreement, when it does

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SpecResult:
    """The compiled document: what it asks for, and where it contradicts itself."""

    doc_id: str
    doc_path: str
    measurands: list[Measurand] = field(default_factory=list)
    commitments: list[Commitment] = field(default_factory=list)
    defects: list[Defect] = field(default_factory=list)
    # Coverage is a first-class output, not a diagnostic. A tool that cannot
    # say what it did not read cannot be checked by the person relying on it.
    blocks_total: int = 0
    blocks_compiled: int = 0
    uncompiled: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "doc_id": self.doc_id,
                "doc_path": self.doc_path,
                "coverage": {
                    "blocks_total": self.blocks_total,
                    "blocks_compiled": self.blocks_compiled,
                    "blocks_uncompiled": len(self.uncompiled),
                    "ratio": (
                        round(self.blocks_compiled / self.blocks_total, 4)
                        if self.blocks_total
                        else 0.0
                    ),
                },
                "sections": self.sections,
                "measurands": [m.to_dict() for m in self.measurands],
                "commitments": [c.to_dict() for c in self.commitments],
                "defects": [d.to_dict() for d in self.defects],
                "uncompiled": self.uncompiled,
                "notes": self.notes,
            },
            indent=2,
            ensure_ascii=False,
        )
