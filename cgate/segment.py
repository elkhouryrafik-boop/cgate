"""
cgate.segment — reading-ordered blocks to bounded provisions (Clauses).

PDF text extraction yields lines, not provisions. A clause such as general-conditions
clause 6.2 spans a dozen line-blocks and often crosses a page boundary. Every later
stage reasons about obligations, and an obligation that has been cut in half by
a line break is a different obligation, so the join has to happen here.

Boundaries are taken from the document's own numbering wherever it exists,
because that is also the addressing scheme the document uses to refer to itself
("as defined in Article 6.8"). Getting the numbering right is what lets the
exception-scope pass follow a non-local carve-out to the clause it modifies.
"""

from __future__ import annotations

import re

from .model import Block, Clause, Document, SpanRef

# ---------------------------------------------------------------------------
# Boundary patterns
# ---------------------------------------------------------------------------

# "1.", "1.1", "3.2.2.14", "6.2"
NUM_DOTTED = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,4})\.?\s+(?=\S)")
# "(a)", "(iv)", "a)", "i."
NUM_ALPHA = re.compile(r"^\(?([a-z]|[ivxl]{1,5})[\)\.]\s+(?=\S)", re.IGNORECASE)
# Article / Clause / Section headings
NUM_WORD = re.compile(
    r"^(?:ARTICLE|Article|CLAUSE|Clause|SECTION|Section|ANNEX|Annex|SCHEDULE|Schedule)\s+"
    r"([A-Z0-9][A-Z0-9.\-]*)\b",
)
# A defined term opening a definition: "Affiliates" means ...
DEFINITION = re.compile(r'^"([^"]{2,80})"\s+(?:means|has the meaning|is defined)')

# Cross-references the document makes to itself.
INTERNAL_REF = re.compile(
    r"\b(?:Article|Articles|Clause|Clauses|Section|Sections|paragraph|paragraphs|"
    r"sub-?clause|Annex|Annexes|Schedule|Appendix)\s+"
    r"([A-Z]?\d{1,2}(?:\.\d{1,3}){0,4}[a-z]?|\([a-z]\)|[A-Z])\b"
)

# Instruments pulled into the contract from outside it.
EXTERNAL_INSTRUMENT = re.compile(
    r"\b("
    r"UN(?:'s)?\s+(?:Policy|Policies|Code|Guidelines?|Regulations?|Rules?|Standards?|Framework|Manual|Charter)"
    r"[^.,;:]{0,120}"
    r"|Supplier Code of Conduct"
    r"|General (?:Terms and )?Conditions(?: of Contract)?"
    r"|Financial Regulations and Rules"
    r"|Security Council (?:Committee|resolutions?)[^.,;:]{0,80}"
    r"|Consolidated [A-Z][\w ]{0,60}Sanctions List"
    r"|Convention on the (?:Privileges|Rights)[^.,;:]{0,80}"
    r")",
)

INCORPORATION_MARKER = re.compile(
    r"\b(incorporated (?:herein )?by reference"
    r"|form(?:s)? an integral part of"
    r"|shall (?:be deemed to )?form part of"
    r"|are publicly available"
    r"|available (?:on|at|from) [^.;]{0,80}"
    r"|as (?:amended|updated) from time to time"
    r"|referred to in the Contract"
    r"|attached hereto"
    r"|set (?:out|forth) in (?:Annex|Schedule|Appendix)[^.;]{0,40}"
    r")",
    re.IGNORECASE,
)

URL = re.compile(r"\b(?:https?://|www\.)[^\s,;)]+", re.IGNORECASE)


# ---------------------------------------------------------------------------


def _boundary(block: Block) -> tuple[bool, str | None, int]:
    """(starts_a_clause, number, depth) for a block."""
    t = block.text
    if block.kind in ("header", "footer"):
        return False, None, 0
    if block.kind == "heading":
        m = NUM_DOTTED.match(t) or NUM_WORD.match(t)
        num = m.group(1) if m else None
        return True, num, (num.count(".") + 1 if num and "." in num else 1)
    m = NUM_DOTTED.match(t)
    if m:
        num = m.group(1)
        return True, num, num.count(".") + 1
    m = NUM_WORD.match(t)
    if m:
        return True, m.group(1), 1
    m = DEFINITION.match(t)
    if m:
        return True, None, 3
    m = NUM_ALPHA.match(t)
    if m and len(t) > 12:
        return True, f"({m.group(1)})", 4
    return False, None, 0


def _is_noise(block: Block) -> bool:
    t = block.text.strip()
    if block.kind in ("header", "footer"):
        return True
    if re.fullmatch(r"[_\-\.\s\|]{4,}", t):
        return True
    if re.fullmatch(r"(?:Page\s*)?\d+\s*(?:of\s*\d+)?", t, re.IGNORECASE):
        return True
    return False


def segment(doc: Document) -> list[Clause]:
    """Group blocks into numbered provisions, preserving span provenance."""
    clauses: list[Clause] = []
    cur: dict | None = None
    stack: list[tuple[int, str]] = []          # (depth, number) for path building
    heading_carry: str | None = None

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        text = " ".join(cur["parts"]).strip()
        if len(text) < 20:
            cur = None
            return
        cid = f"c{len(clauses):04d}"
        cl = Clause(
            clause_id=cid,
            number=cur["number"],
            heading=cur["heading"],
            path=list(cur["path"]),
            text=text,
            spans=cur["spans"],
            page_start=cur["page_start"],
            page_end=cur["page_end"],
            depth=cur["depth"],
        )
        cl.internal_refs = sorted({m.group(0) for m in INTERNAL_REF.finditer(text)})
        ext: list[dict] = []
        for m in EXTERNAL_INSTRUMENT.finditer(text):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;")
            window = text[max(0, m.start() - 160) : m.end() + 220]
            inc = INCORPORATION_MARKER.search(window)
            ext.append(
                {
                    "instrument": name[:160],
                    "incorporation_marker": inc.group(0)[:80] if inc else None,
                    "url": (URL.search(window).group(0) if URL.search(window) else None),
                    "attached": False,      # resolved later against the bundle
                }
            )
        # de-duplicate by instrument name
        seen: set[str] = set()
        cl.external_refs = [
            e for e in ext if not (e["instrument"].lower() in seen
                                   or seen.add(e["instrument"].lower()))
        ]
        clauses.append(cl)
        cur = None

    for b in doc.blocks:
        if _is_noise(b):
            continue
        starts, number, depth = _boundary(b)

        # A short heading with no body attaches to the next clause.
        if b.kind == "heading" and len(b.text) < 90 and not NUM_DOTTED.match(b.text):
            flush()
            heading_carry = b.text
            continue

        if starts:
            flush()
            if number:
                while stack and stack[-1][0] >= depth:
                    stack.pop()
                stack.append((depth, number))
            path = [n for _, n in stack]
            cur = {
                "number": number,
                "heading": heading_carry,
                "path": path,
                "depth": depth,
                "parts": [b.text],
                "spans": [SpanRef(doc.doc_id, b.block_id, 0, len(b.text))],
                "page_start": b.page,
                "page_end": b.page,
            }
            heading_carry = None
        elif cur is not None:
            cur["parts"].append(b.text)
            cur["spans"].append(SpanRef(doc.doc_id, b.block_id, 0, len(b.text)))
            cur["page_end"] = b.page
        else:
            cur = {
                "number": None,
                "heading": heading_carry,
                "path": [n for _, n in stack],
                "depth": len(stack),
                "parts": [b.text],
                "spans": [SpanRef(doc.doc_id, b.block_id, 0, len(b.text))],
                "page_start": b.page,
                "page_end": b.page,
            }
            heading_carry = None

    flush()

    # Parent links, used by the exception-scope pass to follow non-local carve-outs.
    by_number: dict[str, str] = {}
    for cl in clauses:
        if cl.number:
            by_number.setdefault(cl.number, cl.clause_id)
    for cl in clauses:
        if cl.number and "." in cl.number:
            parent_num = cl.number.rsplit(".", 1)[0]
            cl.parent_id = by_number.get(parent_num)
    return clauses


def index_by_number(clauses: list[Clause]) -> dict[str, Clause]:
    out: dict[str, Clause] = {}
    for c in clauses:
        if c.number:
            out.setdefault(c.number, c)
    return out
