"""
cgate.resolve — is the instrument this contract incorporates actually in front of us?

`segment` records every external instrument a clause pulls in and leaves
`attached: False` on it, to be settled here against the whole bundle of files
under review. Settling it matters in both directions.

A false "not attached" is the more dangerous error. A tender can
incorporate its General Terms and Conditions — and then print them, in
full, on later pages of the same PDF. Reporting them as missing sends a
reviewer looking for a document they are already holding, and a control that
raises alarms about things that are fine is a control people learn to skip.

A false "attached" is quieter and worse. It would tell the Compliance Officer
that the content of an instrument is known when it is not, which is exactly the
assumption checklist item 12 exists to prevent. So the test here is deliberately
narrow: a document is treated as present only when the bundle contains something
that looks like its *title page*, with substantial content following it, and the
location is printed in the review so a human can check it in one turn of a page.
Naming an instrument in a list is not the same as attaching it, and does not
count.
"""

from __future__ import annotations

import os
import re

from .model import Clause, Document

# "ANNEX A GENERAL TERMS AND CONDITIONS" -> "GENERAL TERMS AND CONDITIONS"
TITLE_PREFIX = re.compile(
    r"^(?:annex|appendix|schedule|attachment|section|part)\s+[A-Za-z0-9]+\s*[:.\-—]?\s*",
    re.IGNORECASE,
)

MAX_TITLE_CHARS = 140
MIN_INSTRUMENT_CHARS = 14
MIN_FOLLOWING_BLOCKS = 10

# How far into a document a glued title (see `_glued_title`) may appear. A
# document's title page is at its start; a page header or two may precede it.
# Past this, an all-capitals opening is a mention inside somebody else's
# document, not that document's own title.
MAX_GLUED_TITLE_BLOCK = 3

# Publication metadata that policy title pages run onto the same line as the
# title itself. "EXAMPLE POLICY ON SAFEGUARDING Document number: POLICY/XX/
# 2024/001 v. 6 June 2025 Effective date: ..." is one 200-character line in
# which only the first four words are the title. Cutting the tail lets the
# length test see the title rather than the masthead.
METADATA_TAIL = re.compile(
    r"\s+(?:document\s+number|document\s+no|doc\.?\s*no|version\s+number|issuance\s+date|"
    r"effective\s+date|mandatory\s+review\s+date|approved\s+by|business\s+owner|"
    r"revision|rev\.|v\.\s*\d)\b.*$",
    re.IGNORECASE | re.DOTALL,
)


def _canon(s: str) -> str:
    s = s.lower()
    # A contract cites "the Agency's Policy on Safeguarding"; the document titles
    # itself "AGENCY POLICY ON SAFEGUARDING". Dropping the possessive before
    # punctuation is squashed keeps the two from differing by a stray "s" —
    # without it neither string contains the other and one document reads as
    # two.
    s = re.sub(r"['’]s\b", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(s.split())


def _strip_metadata_tail(text: str) -> str:
    """Drop a trailing document-number/version/date masthead from a title line."""
    return METADATA_TAIL.sub("", text).strip()


def _glued_title(text: str) -> str | None:
    """The title of a document whose PDF runs it into the first paragraph.

    A Supplier Code of Conduct can have no isolated title block at all: the
    text reads "SUPPLIER CODE OF CONDUCT A. Principles: The
    values enshrined..." as a single body block. The title is there; it simply
    has no block of its own.

    The discrimination that keeps this safe is what follows the capitals. A
    document title is followed by more document — a section marker, a
    capitalised heading, the start of the text proper. A *mention* of a
    document inside somebody else's prose is the subject of a sentence, and is
    therefore followed by a lower-case verb: "SUPPLIER CODE OF CONDUCT is
    available at www.example.invalid". So the leading run of non-lower-case characters
    is a title only when it ends by running into a capitalised word, never when
    it ends cleanly at a space before a lower-case one. Returns None when the
    opening is prose, which is the refusing default.
    """
    m = re.match(r"^[^a-z]+", text)
    if not m:
        return None
    run = m.group(0)
    # Ended at a word boundary: the next word is lower-case, so this is a
    # sentence about the document, not the document.
    if run != text and (run.endswith(" ") or run.endswith("\t")):
        return None
    words = run.split()
    if len(words) < 2:
        return None
    # The final token is the capital that started the next word ("...CONDUCT A. U").
    if run != text:
        words = words[:-1]
    # A lone section marker left at the end ("A.", "1.") is not part of the title.
    while words and re.fullmatch(r"[A-Z0-9]{1,2}[.)]?", words[-1]):
        words.pop()
    return " ".join(words) if len(words) >= 2 else None


def _looks_like_a_title(text: str, kind: str) -> bool:
    if len(_strip_metadata_tail(text)) > MAX_TITLE_CHARS:
        return False
    if kind == "heading":
        return True
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    # A title page line is typically set in capitals in UN solicitations.
    return sum(c.isupper() for c in letters) / len(letters) > 0.7


def _title_index(docs: list[Document]) -> list[tuple[str, Document, object]]:
    out: list[tuple[str, Document, object]] = []
    for d in docs:
        total = len(d.blocks)
        for i, b in enumerate(d.blocks):
            t = b.text.strip()
            candidates: list[str] = []
            if _looks_like_a_title(t, b.kind):
                candidates.append(_strip_metadata_tail(t))
            elif i <= MAX_GLUED_TITLE_BLOCK:
                glued = _glued_title(t)
                if glued:
                    candidates.append(_strip_metadata_tail(glued))
            if not candidates:
                continue
            # A title with nothing after it is a mention, not a document.
            if total - i < MIN_FOLLOWING_BLOCKS:
                continue
            for cand in candidates:
                canon = _canon(TITLE_PREFIX.sub("", cand))
                if len(canon) >= MIN_INSTRUMENT_CHARS:
                    out.append((canon, d, b))
    return out


def dedupe_instruments(names: dict[str, str | None]) -> dict[str, str | None]:
    """Drop any instrument name that another captured name already contains.

    A clause citing "the UN Supplier Code of Conduct" and another citing
    "Supplier Code of Conduct" are pointing at one document. Listing it twice
    makes the review look like it found two unretrieved instruments when it
    found one, and a reviewer counting escalations would be counting noise.
    """
    keys = sorted(names, key=len, reverse=True)
    kept: dict[str, str | None] = {}
    for k in keys:
        low = k.lower()
        if any(low in other.lower() and low != other.lower() for other in kept):
            continue
        kept[k] = names[k]
    return kept


def resolve_incorporation(
    docs: list[Document], clauses_by_doc: list[tuple[Document, list[Clause]]]
) -> list[str]:
    """Mark incorporated instruments present in the bundle. Returns review notes."""
    titles = _title_index(docs)
    located: dict[str, str] = {}
    missing: set[str] = set()

    for _doc, clauses in clauses_by_doc:
        for cl in clauses:
            # Keyed by document as well as block. `block_id` is "p1.c0.b0" —
            # unique inside one document and emphatically not across a bundle,
            # so comparing it alone made an annex whose title page happens to
            # sit at the same page/column/index as the citing clause look like
            # the citing sentence itself, and silently reported an attached
            # instrument as missing.
            own_blocks = {(sp.doc_id, sp.block_id) for sp in cl.spans}
            for ref in cl.external_refs:
                name = _canon(ref["instrument"])
                if len(name) < MIN_INSTRUMENT_CHARS:
                    continue
                if not ref.get("incorporation_marker"):
                    continue

                where = None
                for canon, tdoc, tblock in titles:
                    if (tdoc.doc_id, tblock.block_id) in own_blocks:
                        continue                      # the citing sentence itself
                    if name in canon or canon in name:
                        where = f"{os.path.basename(tdoc.path)} p.{tblock.page}"
                        break

                if where:
                    ref["attached"] = True
                    ref["attached_at"] = where
                    located[ref["instrument"]] = where
                else:
                    missing.add(ref["instrument"])

    located = dedupe_instruments(located)
    missing = dedupe_instruments({m: None for m in missing})

    # Which publisher's General Conditions these are is stated by the tender,
    # not by the phrase "General Conditions". Read it off the front of the
    # primary document, where solicitations put their own letterhead.
    issuer = None
    try:
        from .corpus import issuer_in

        if docs and docs[0].blocks:
            issuer = issuer_in(" ".join(b.text for b in docs[0].blocks[:40]))
    except Exception:
        issuer = None

    notes: list[str] = []
    for instrument, where in sorted(located.items()):
        notes.append(
            f"Incorporated instrument located in the bundle: {instrument} "
            f"({where}). Its provisions were read and are included below."
        )
    for instrument in sorted(missing):
        note = (
            f"Incorporated instrument NOT in the bundle: {instrument}. "
            "Its content is unknown to this review and was not assumed."
        )
        # The reference corpus can often say which document that is. It does
        # not say the document is attached — it is not, and the sentence above
        # stands unchanged. What it removes is the dead end: a reviewer who
        # reads "content unknown" and has no idea where to go next.
        found = _corpus_candidates(instrument, issuer)
        if len(found) == 1:
            c = found[0]
            note += (
                f" A copy is held in the reference corpus as `{c.id}`"
                f" ({c.issuer}, stated edition {c.edition_hint or 'unrecorded'},"
                f" retrieved {c.retrieved_at}, sha256 {c.sha256[:12]});"
                " supply it on the command line to have its provisions read."
            )
        elif found:
            named = ", ".join(f"`{c.id}` ({c.edition_hint or 'edition unrecorded'})"
                              for c in found)
            note += (
                f" The reference corpus holds {len(found)} instrument(s) that "
                f"could be this one — {named}. Which of them the tender means "
                "is not something a title match can settle; supply the right "
                "one on the command line."
            )
        notes.append(note)
    return notes


def _corpus_candidates(instrument: str, issuer: str | None):
    """Corpus entries that might be this instrument. Never more than three.

    Isolated behind a try because a review must not fail on account of a
    missing or malformed corpus. No corpus is a smaller problem than a review
    that will not run.
    """
    try:
        from .corpus import candidates

        return candidates(instrument, issuer_hint=issuer)
    except Exception:
        return []
