"""
cgate.extract — clauses to Requirements, by rule.

This is the rules layer of the "roughly 70% needs no language model" claim in
docs/ARCHITECTURE.md. It carries the whole of checklist item 12 on its own. A
language model is not consulted here and is not required for the tool to be
useful; `extractor="llm"` is a later, optional second pass whose output is
subject to exactly the same grounding gate as this one.

What a Requirement is
---------------------
One provision-bearing sentence (or, where a clause has no sentence structure the
splitter can see, one clause) that matches at least one category in
`lexicon.CATEGORIES`, together with:

  * its modality — obligation, prohibition, permission, UN right, condition
  * its actor — who the modality binds
  * every carve-out in it, each with an explicit scope-resolution state
  * character spans into the source blocks, which the grounding gate re-checks

Offsets are the point
---------------------
`Clause.text` is `" ".join(block texts)`. Every offset produced here is mapped
back through that join to (block_id, start, end) in the *normalised* block text
that `ingest` produced, so `Document.text_of()` returns the same characters this
module read. If the join cannot be reconstructed exactly the clause is cited
whole rather than at a guessed offset — a wider citation is recoverable, a wrong
one is not.

Recall over precision is not the trade here
-------------------------------------------
A reviewer who learns the output is noisy stops reading it, and then the control
does not exist. Anchors are specific by construction (see the lexicon
docstring), a two-context-term rule stands in for a missing anchor, and
sentences under `MIN_SENTENCE_CHARS` are dropped rather than guessed at.
"""

from __future__ import annotations

import re

from . import lexicon as lx
from .model import (
    Actor,
    Clause,
    Document,
    Exception_,
    Modality,
    Requirement,
    SpanRef,
)

MIN_SENTENCE_CHARS = 25
MAX_REQ_CHARS = 1400


# ---------------------------------------------------------------------------
# Offset mapping: clause text -> source blocks
# ---------------------------------------------------------------------------


def _segments(clause: Clause) -> list[tuple[int, int, SpanRef]] | None:
    """[(clause_start, clause_end, span)] or None if the join is not reproducible."""
    segs: list[tuple[int, int, SpanRef]] = []
    pos = 0
    for sp in clause.spans:
        length = sp.end - sp.start
        segs.append((pos, pos + length, sp))
        pos += length + 1              # the single space that " ".join inserted
    if not segs:
        return None
    if segs[-1][1] != len(clause.text):
        return None                    # strip() moved things, or the text was edited
    return segs


def spans_for(clause: Clause, start: int, end: int) -> list[SpanRef]:
    """Map a [start, end) range in `clause.text` back to source spans."""
    segs = _segments(clause)
    if segs is None:
        return list(clause.spans)
    out: list[SpanRef] = []
    for s0, s1, sp in segs:
        a, b = max(start, s0), min(end, s1)
        if a < b:
            out.append(
                SpanRef(sp.doc_id, sp.block_id, sp.start + (a - s0), sp.start + (b - s0))
            )
    return out or list(clause.spans)


def _sentences(text: str) -> list[tuple[int, int]]:
    """Sentence ranges. Falls back to the whole text when there is no sentence structure."""
    bounds: list[tuple[int, int]] = []
    start = 0
    for m in lx.SENTENCE_SPLIT.finditer(text):
        bounds.append((start, m.start()))
        start = m.end()
    bounds.append((start, len(text)))
    kept = [(a, b) for a, b in bounds if b - a >= MIN_SENTENCE_CHARS]
    return kept or ([(0, len(text))] if len(text) >= MIN_SENTENCE_CHARS else [])


# ---------------------------------------------------------------------------
# Modality and actor
# ---------------------------------------------------------------------------


def classify_modality(text: str) -> tuple[Modality, int | None]:
    """(modality, offset of the deciding match). Order encodes precedence.

    Prohibition is tested before obligation because "shall not" matches both and
    the negative reading is the operative one. The purchaser's own rights are tested
    before the contractor's obligations for the same reason: "the Purchaser may
    terminate" is a right held by the client, not a permission granted to us,
    and the two route to different checklist items.
    """
    for pattern, mode in (
        (lx.PROHIBITION, Modality.PROHIBITION),
        (lx.RIGHT_OF_UN, Modality.RIGHT_OF_UN),
        (lx.OBLIGATION, Modality.OBLIGATION),
        (lx.CONDITION, Modality.CONDITION),
        (lx.PERMISSION, Modality.PERMISSION),
    ):
        m = pattern.search(text)
        if m:
            return mode, m.start()
    return Modality.UNKNOWN, None


_ACTOR_BY_NAME = {
    "contractor": Actor.CONTRACTOR,
    "subcontractor": Actor.SUBCONTRACTOR,
    "personnel": Actor.PERSONNEL,
    "un": Actor.UN,
}


def classify_actor(text: str, modal_at: int | None) -> Actor:
    """The actor nearest-preceding the modal verb is the one it binds."""
    hits: list[tuple[int, str]] = []
    for name, pattern in lx.ACTOR_PATTERNS:
        for m in pattern.finditer(text):
            hits.append((m.start(), name))
    if not hits:
        return Actor.UNKNOWN
    hits.sort()

    if modal_at is not None:
        before = [h for h in hits if h[0] < modal_at]
        if before:
            return _ACTOR_BY_NAME[before[-1][1]]

    names = {n for _, n in hits}
    if names == {"contractor", "un"}:
        return Actor.BOTH
    return _ACTOR_BY_NAME[hits[0][1]]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def find_exceptions(
    clause: Clause, sent_start: int, sent_end: int, by_number: dict[str, Clause]
) -> list[Exception_]:
    """Carve-outs in one sentence, each with its scope-resolution state.

    Scope is recorded, never assumed. A carve-out pointing at another clause is
    resolved only if that clause is actually present in this document; if it is
    not, `scope_resolved` stays False and the requirement carries
    `exception_scope_unresolved` into adjudication. See `model.Exception_` for
    why this is a first-class state and not a warning.
    """
    text = clause.text[sent_start:sent_end]
    out: list[Exception_] = []
    for m in lx.EXCEPTION_MARKERS.finditer(text):
        # The carve-out runs from its marker to the next clause-level break.
        tail = re.search(r"[;.]\s", text[m.end():])
        rel_end = m.end() + (tail.end() if tail else len(text) - m.end())
        body = text[m.start():rel_end].strip()
        if len(body) < 8:
            continue

        target_ref = None
        resolved = True
        note = "local; the carve-out is stated in the same sentence as the provision"

        ref = lx.NONLOCAL_HINT.search(body)
        if ref:
            target_ref = ref.group(1) if ref.lastindex else None
            if target_ref and target_ref in by_number:
                note = f"non-local; resolves to clause {target_ref} in this document"
            else:
                resolved = False
                shown = target_ref or ref.group(0)
                note = (
                    f"non-local; points to {shown}, which is not present in this "
                    "document as a numbered clause"
                )
        out.append(
            Exception_(
                marker=m.group(0),
                text=body[:600],
                spans=spans_for(clause, sent_start + m.start(), sent_start + rel_end),
                scope_resolved=resolved,
                scope_note=note,
                target_ref=target_ref,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _incorporation(clause: Clause) -> dict | None:
    """The first instrument this clause pulls in by reference without attaching it."""
    for ref in clause.external_refs:
        if ref.get("incorporation_marker") and not ref.get("attached"):
            return {
                "target": ref["instrument"],
                "retrieved": False,
                "url": ref.get("url"),
                "marker": ref["incorporation_marker"],
            }
    return None


def _triggers_for(
    exceptions: list[Exception_], incorporation: dict | None, modality: Modality
) -> list[str]:
    t: list[str] = []
    if any(not e.scope_resolved for e in exceptions):
        t.append("exception_scope_unresolved")
    if incorporation:
        t.append("conditions_incorporated_by_reference_not_retrieved")
    if modality is Modality.UNKNOWN:
        t.append("modality_unresolved")
    return t


def extract_clause(
    clause: Clause, by_number: dict[str, Clause], counter: list[int]
) -> list[Requirement]:
    reqs: list[Requirement] = []
    incorporation = _incorporation(clause)
    seen: set[tuple[str, int, int]] = set()
    matched_any = False

    for s0, s1 in _sentences(clause.text):
        raw = clause.text[s0:s1]
        sentence = raw.strip()
        if len(sentence) < MIN_SENTENCE_CHARS:
            continue
        cats = lx.match_categories(sentence)
        if not cats:
            continue
        matched_any = True

        modality, modal_at = classify_modality(sentence)
        actor = classify_actor(sentence, modal_at)
        exceptions = find_exceptions(clause, s0, s1, by_number)

        # Re-derive the exact range so req.text is verbatim source, not a stripped copy.
        a = s0 + (len(raw) - len(raw.lstrip()))
        b = min(a + len(sentence), a + MAX_REQ_CHARS)

        for cid, hits in cats:
            key = (cid, a, b)
            if key in seen:
                continue
            seen.add(key)
            counter[0] += 1
            reqs.append(
                Requirement(
                    req_id=f"req{counter[0]:04d}",
                    category=cid,
                    modality=modality,
                    actor=actor,
                    text=clause.text[a:b],
                    spans=spans_for(clause, a, b),
                    clause_id=clause.clause_id,
                    clause_number=clause.number,
                    page=clause.page_start,
                    exceptions=exceptions,
                    incorporation=incorporation,
                    checklist_items=list(lx.CATEGORIES[cid]["checklist_items"]),
                    extractor="rules",
                    triggers=_triggers_for(exceptions, incorporation, modality),
                    matched=list(hits),
                )
            )

    # A clause whose provision is spread across sentences that individually carry no
    # anchor is still a finding — cited whole, which is verifiable, rather than cited
    # at a sentence boundary we guessed at.
    if not matched_any:
        b = min(len(clause.text), MAX_REQ_CHARS)
        cats = lx.match_categories(clause.text)
        if cats:
            modality, modal_at = classify_modality(clause.text)
            actor = classify_actor(clause.text, modal_at)
            exceptions = find_exceptions(clause, 0, len(clause.text), by_number)
            for cid, hits in cats:
                counter[0] += 1
                reqs.append(
                    Requirement(
                        req_id=f"req{counter[0]:04d}",
                        category=cid,
                        modality=modality,
                        actor=actor,
                        text=clause.text[:b],
                        spans=spans_for(clause, 0, b),
                        clause_id=clause.clause_id,
                        clause_number=clause.number,
                        page=clause.page_start,
                        exceptions=exceptions,
                        incorporation=incorporation,
                        checklist_items=list(lx.CATEGORIES[cid]["checklist_items"]),
                        extractor="rules",
                        triggers=(
                            _triggers_for(exceptions, incorporation, modality)
                            + ["cited_at_clause_level"]
                        ),
                        matched=list(hits),
                    )
                )
    return reqs


def extract(doc: Document, clauses: list[Clause]) -> list[Requirement]:
    """All rule-derived Requirements in a segmented document."""
    by_number = {c.number: c for c in clauses if c.number}
    counter = [0]
    out: list[Requirement] = []
    for cl in clauses:
        out.extend(extract_clause(cl, by_number, counter))
    return out
