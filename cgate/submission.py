"""
cgate.submission — reviewing the firm's own outgoing submission.

Every other module in this package reads the *client's* document and asks what
it requires. This one reads **our** document and asks whether we have done what
it requires — the way a rigorous examiner reads a paper: marking what is right
as deliberately as what is wrong, and never confusing "I found no fault here"
with "I verified this is sound".

Three marks, and the rule that separates them
---------------------------------------------
``COMMENDATION``  a check ran against a source, and this passage passed it.
``CORRECTION``    a check ran against a source, and this passage failed it.
``QUERY``         a reviewer needs the author to answer something. Not a fault.

**A commendation is earned, never inferred.** It is emitted only where a named
check actually executed against a named source and that source affirmed the
passage. Silence never produces one. This is the same rule as `evidence_present`
versus `unresolved` in `model.Status`, applied to ink in a margin — and it is
load-bearing, because a green mark that merely means "nothing was checked here"
would be the single most dangerous output this system could produce. A reviewer
trusts the margin more than any report, and PRODUCT.md forbids any treatment
that lets an unchecked item look like a clean one.

Every mark carries `evidence`: the sentence explaining how the mark was reached
and against which source. A mark whose reasoning is hidden is a machine
asserting a conclusion, which is the thing this architecture exists to prevent.

What is marked in the margin, and what is not
---------------------------------------------
An **omission has no span**. A mandatory requirement the submission never
answers cannot be underlined, because there is nothing there to underline.
Inventing an anchor for it would be a fabrication in the one place a reviewer
cannot check cheaply. Omissions therefore appear in the `Rubric` — the grade
sheet — and never as a document mark.

The rubric also records which checks *did not run*, and why. A rubric showing
"0 unverified personnel" because no staff register was supplied would be a lie
told with a true number.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict

from .model import Document, Requirement, SpanRef

# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------

COMMENDATION = "commendation"
CORRECTION = "correction"
QUERY = "query"

MARK_ORDER = (CORRECTION, QUERY, COMMENDATION)

MARK_LABEL = {
    COMMENDATION: "Verified",
    CORRECTION: "Correction",
    QUERY: "Query",
}

# The examiner's shorthand, used in the margin and on the grade sheet.
MARK_GLYPH = {COMMENDATION: "✓", CORRECTION: "✗", QUERY: "?"}


@dataclass
class Mark:
    """One examiner's mark on one passage of the submission."""

    mark_id: str
    kind: str
    rule: str                 # the check that produced it
    heading: str              # what a reader sees first, in the margin
    detail: str               # the mark itself, in a reviewer's words
    evidence: str             # how it was reached, and against what source
    quote: str                # verbatim from the submission
    spans: list[SpanRef] = field(default_factory=list)
    page: int = 0
    block_id: str = ""
    # Where a reader should look, in a form the source can actually support.
    # A .docx has no pages until Word renders it, so `page` is 1 for every
    # block of one and printing "p.1" beside every mark in a nine-page
    # proposal is a confident lie. See `locate()`.
    locator: str = ""
    checklist_items: list[int] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = MARK_LABEL[self.kind]
        d["glyph"] = MARK_GLYPH[self.kind]
        return d


@dataclass
class SkippedCheck:
    """A check that did not run, and the source it needed.

    This is the rubric's honesty mechanism. Without it a grade sheet reports
    zero faults in a dimension nobody examined, which reads as a pass.
    """

    rule: str
    needs: str
    consequence: str


@dataclass
class Rubric:
    """The grade sheet: coverage, not a score.

    There is deliberately no mark out of ten and no percentage. A single figure
    would conceal weakness on one high-consequence dimension behind strength on
    a trivial one, which the design rules out for the same reason it rules
    out an aggregate accuracy figure.
    """

    submission: str
    submission_id: str
    solicitation_id: str | None = None

    mandatory_total: int = 0
    mandatory_addressed: list[dict] = field(default_factory=list)
    mandatory_unaddressed: list[dict] = field(default_factory=list)

    personnel_named: int = 0
    personnel_verified: list[str] = field(default_factory=list)
    personnel_unverified: list[str] = field(default_factory=list)

    claims_queried: int = 0
    shared_passages: int = 0
    undeclared_parties: list[str] = field(default_factory=list)

    checks_run: list[str] = field(default_factory=list)
    checks_skipped: list[SkippedCheck] = field(default_factory=list)

    @property
    def triggers(self) -> list[str]:
        out = []
        if self.mandatory_unaddressed:
            out.append("mandatory_requirement_unaddressed")
        if self.personnel_unverified:
            out.append("person_not_in_register")
        if self.shared_passages:
            out.append("content_similarity_above_threshold")
        if self.undeclared_parties:
            out.append("undeclared_party_in_submission")
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["triggers"] = self.triggers
        return d


@dataclass
class SubmissionReview:
    submission_path: str
    submission_id: str
    marks: list[Mark] = field(default_factory=list)
    rubric: Rubric | None = None
    notes: list[str] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Mark]:
        return [m for m in self.marks if m.kind == kind]

    def counts(self) -> dict[str, int]:
        return {k: len(self.by_kind(k)) for k in MARK_ORDER}

    def to_dict(self) -> dict:
        return {
            "submission_path": self.submission_path,
            "submission_id": self.submission_id,
            "counts": self.counts(),
            "marks": [m.to_dict() for m in self.marks],
            "rubric": self.rubric.to_dict() if self.rubric else None,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Check 1 — did we answer what the solicitation demands?
# ---------------------------------------------------------------------------

# Verbs that make a provision a *deliverable* rather than a general obligation.
# "The Contractor shall indemnify" is an obligation but nothing to answer in a
# proposal; "The Bidder shall submit a methodology" is a thing that is either
# in the document or is not.
DELIVERABLE_VERBS = re.compile(
    r"\b(submit|provide|furnish|include|attach|enclose|complete|supply|"
    r"demonstrate|describe|detail|specify|state|list|indicate|present)\b",
    re.I,
)

# Words too common to identify a requirement by. Overlap on these means nothing.
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "with", "by", "on",
    "at", "as", "is", "are", "be", "been", "shall", "must", "will", "may",
    "any", "all", "each", "such", "this", "that", "these", "those", "from",
    "which", "their", "its", "it", "not", "no", "bidder", "contractor",
    "supplier", "offeror", "tenderer", "proposal", "bid", "tender", "party",
    "parties", "organisation", "organization", "agency",
    "submission", "document", "documents", "section", "clause", "article",
    "annex", "appendix", "form", "including", "shall_not", "other", "otherwise",
    "provide", "submit", "include", "required", "requirement", "requirements",
}

# A requirement must contribute at least this many distinctive terms before
# coverage can be judged at all. Below it, term overlap is noise and the
# honest answer is that this check could not read the requirement.
MIN_TERMS = 3

# Fraction of a requirement's distinctive terms that must appear together in one
# paragraph of the submission before that paragraph counts as addressing it.
COVERAGE_THRESHOLD = 0.6


def _terms(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z][A-Za-z\-']{2,}", text.lower())
    return {w for w in words if w not in _STOP}


def deliverables(requirements: list[Requirement]) -> list[Requirement]:
    """Provisions that ask the bidder to put something in the submission."""
    from .model import Actor, Modality

    out = []
    for r in requirements:
        if r.modality is not Modality.OBLIGATION:
            continue
        if r.actor not in (Actor.CONTRACTOR, Actor.BOTH, Actor.PERSONNEL):
            continue
        if not DELIVERABLE_VERBS.search(r.text):
            continue
        out.append(r)
    return out


def check_coverage(
    doc: Document, reqs: list[Requirement], counter
) -> tuple[list[Mark], list[dict], list[dict]]:
    """For each deliverable requirement, find the paragraph that answers it.

    A commendation here is a real but *narrow* finding, and says so: it means
    the submission has a passage using this requirement's own distinctive
    terms. It does not mean the answer is adequate. Saying more than that would
    be a determination about quality, which no term-overlap test can support.
    """
    marks: list[Mark] = []
    addressed: list[dict] = []
    unaddressed: list[dict] = []

    blocks = [b for b in doc.blocks if len(b.text.split()) >= 12]
    block_terms = [(b, _terms(b.text)) for b in blocks]

    for r in reqs:
        want = _terms(r.text)
        if len(want) < MIN_TERMS:
            unaddressed.append({
                "req_id": r.req_id, "clause": r.clause_number, "page": r.page,
                "text": r.text[:300],
                "why": "too few distinctive terms to test coverage; read it by hand",
                "testable": False,
            })
            continue

        best_block, best_hit = None, 0.0
        for b, have in block_terms:
            if not have:
                continue
            hit = len(want & have) / len(want)
            if hit > best_hit:
                best_block, best_hit = b, hit

        entry = {
            "req_id": r.req_id, "clause": r.clause_number, "page": r.page,
            "text": r.text[:300], "coverage": round(best_hit, 2), "testable": True,
        }
        if best_block is None or best_hit < COVERAGE_THRESHOLD:
            unaddressed.append({**entry, "why": "no paragraph of the submission uses "
                                                "this requirement's distinctive terms"})
            continue

        entry["block_id"] = best_block.block_id
        entry["page_in_submission"] = best_block.page
        addressed.append(entry)
        clause = f"clause {r.clause_number}" if r.clause_number else "the requirement"
        marks.append(Mark(
            mark_id=f"m{next(counter)}",
            kind=COMMENDATION,
            rule="requirement_coverage",
            heading=f"Answers {clause}",
            detail=(
                f"This passage responds to {clause} of the solicitation "
                f"(page {r.page}), which requires: “{r.text[:180].strip()}”."
            ),
            evidence=(
                f"{int(best_hit * 100)}% of that requirement's distinctive terms "
                f"appear in this paragraph. That establishes the requirement is "
                f"addressed here — not that the answer is sufficient. Sufficiency "
                f"is yours to judge."
            ),
            quote=best_block.text[:400],
            spans=[SpanRef(doc.doc_id, best_block.block_id, 0,
                           min(len(best_block.text), 400))],
            page=best_block.page,
            block_id=best_block.block_id,
            locator=locate(doc, best_block),
            checklist_items=[12],
        ))

    return marks, addressed, unaddressed


# ---------------------------------------------------------------------------
# Check 2 — is every named person on the staff register?
# ---------------------------------------------------------------------------

# Conservative. Two to four capitalised words, allowing common name particles
# (al-, van, de, ...). Deliberately misses more than it invents: a missed
# name is visible on the rubric as a low count, an invented one puts a
# correction against a person who does not exist.
NAME_RE = re.compile(
    r"\b((?:(?:El|Al|Abu|Abou|Bou|Ibn|Bin|De|Van|Von|Da|Di|Le|La)[\s\-])?"
    r"[A-Z][a-z]{1,15}(?:[\-'][A-Z][a-z]{1,15})?"
    r"(?:\s+(?:(?:El|Al|Abu|Abou|Bou|Ibn|Bin|de|van|von|da|di|le|la)[\s\-])?"
    r"[A-Z][a-z]{1,15}(?:[\-'][A-Z][a-z]{1,15})?){1,3})\b"
)

# Sections where a capitalised name is likely to be a person rather than a place.
PERSONNEL_HINT = re.compile(
    r"\b(cv|curriculum\s+vitae|key\s+personnel|team\s+(?:member|leader|composition)|"
    r"staff|expert|engineer|architect|manager|specialist|coordinator|surveyor|"
    r"proposed\s+team|personnel|qualifications?)\b",
    re.I,
)

# Role titles are two capitalised words, exactly the shape of a name, so
# "Riley Stone, Senior Engineer" reads as two people unless roles are excluded.
# A candidate containing any of these is a job, not a person.
ROLE_WORDS = re.compile(
    r"\b(engineer|architect|manager|leader|specialist|coordinator|surveyor|"
    r"director|officer|consultant|expert|analyst|planner|supervisor|inspector|"
    r"technician|draughtsman|draftsman|quantity|senior|junior|principal|lead|"
    r"chief|deputy|assistant|associate|head|team|key|personnel|staff|"
    r"curriculum|vitae|resident|site|design|project|contract|civil|structural|"
    r"electrical|mechanical|environmental|geotechnical)\b",
    re.I,
)

# Capitalised strings that are not people.
_NOT_A_PERSON = re.compile(
    r"\b(ltd|limited|sal|sarl|llc|inc|corp|company|group|united|nations|"
    r"example\s+city|project|annex|appendix|section|table|figure|january|"
    r"february|march|april|may|june|july|august|september|october|november|"
    r"december|monday|tuesday|wednesday|thursday|friday|ministry|university|"
    r"republic|government|department|terms|reference|scope|work)\b",
    re.I,
)


def _fold_person(name: str) -> str:
    s = re.sub(r"[^\w\s]", " ", name.lower())
    return re.sub(r"\s+", " ", s).strip()


def named_persons(doc: Document) -> list[tuple[str, object]]:
    """(name, block) for every person-shaped string in a personnel context."""
    found: list[tuple[str, object]] = []
    seen: set[str] = set()
    for b in doc.blocks:
        if not PERSONNEL_HINT.search(b.text):
            continue
        for m in NAME_RE.finditer(b.text):
            name = m.group(1).strip()
            if len(name.split()) < 2:
                continue
            if _NOT_A_PERSON.search(name) or ROLE_WORDS.search(name):
                continue
            key = _fold_person(name)
            if key in seen:
                continue
            seen.add(key)
            found.append((name, b))
    return found


def check_personnel(
    doc: Document, staff_register: list[dict] | None, counter
) -> tuple[list[Mark], list[str], list[str], SkippedCheck | None]:
    """Every named person against the staff register.

    With no register this check does not run, and says so. It does not fall
    back to reporting every name as unverified — that would be a fault list
    generated by the absence of a source rather than by anything in the paper.
    """
    people = named_persons(doc)
    if staff_register is None:
        return [], [], [], SkippedCheck(
            rule="personnel_register",
            needs="a staff register: JSON list of {name, status, role}",
            consequence=(
                f"{len(people)} name(s) were detected in personnel sections and "
                "none could be checked. Checklist items 13 and 14 remain "
                "unevaluated."
            ),
        )

    active = {
        _fold_person(p["name"]): p
        for p in staff_register
        if str(p.get("status", "active")).lower() in ("active", "current",
                                                      "approved_associate")
    }
    known = {_fold_person(p["name"]): p for p in staff_register}

    marks: list[Mark] = []
    verified: list[str] = []
    unverified: list[str] = []

    for name, block in people:
        key = _fold_person(name)
        record = active.get(key)
        if record:
            verified.append(name)
            role = record.get("role")
            marks.append(Mark(
                mark_id=f"m{next(counter)}",
                kind=COMMENDATION,
                rule="personnel_register",
                heading=f"{name} verified",
                detail=(
                    f"{name} is on the staff register as "
                    f"{role or 'a current member of staff'}"
                    f"{', status ' + str(record.get('status')) if record.get('status') else ''}."
                ),
                evidence=("Matched against the staff register supplied to this run, "
                          "on a case- and punctuation-insensitive comparison of the "
                          "full name."),
                quote=name,
                spans=[_span_for(doc, block, name)],
                page=block.page,
                block_id=block.block_id,
                locator=locate(doc, block),
                checklist_items=[13, 14],
            ))
            continue

        unverified.append(name)
        stale = known.get(key)
        if stale:
            detail = (f"{name} appears on the staff register with status "
                      f"“{stale.get('status')}”, which is not current staff or an "
                      "approved associate.")
            trig = ["status_inactive"]
        else:
            detail = (f"{name} does not appear on the staff register at all. Naming "
                      "a person who is not current staff or an approved associate is "
                      "a misrepresentation in a submission, independently of anything "
                      "else in this file.")
            trig = ["person_not_in_register"]
        marks.append(Mark(
            mark_id=f"m{next(counter)}",
            kind=CORRECTION,
            rule="personnel_register",
            heading=f"{name} not verified",
            detail=detail,
            evidence=("Compared against the staff register supplied to this run. "
                      "If the register is out of date, correct the register — do not "
                      "leave the name unsupported in the submission."),
            quote=name,
            spans=[_span_for(doc, block, name)],
            page=block.page,
            block_id=block.block_id,
            locator=locate(doc, block),
            checklist_items=[13, 14],
            triggers=trig,
        ))

    return marks, verified, unverified, None


def locate(doc: Document, block) -> str:
    """A human-readable position, honest about what the format knows.

    PDFs carry real page numbers. Word documents do not until Word paginates
    them, and `ingest` reports `n_pages=0` for a .docx rather than inventing
    any — so marks on one are located by paragraph, which is a number the
    source genuinely has.
    """
    if block is None:
        return ""
    if doc.n_pages > 0 and block.page:
        return f"p.{block.page}"
    return f"\u00b6{block.order + 1}"


def _span_for(doc: Document, block, needle: str) -> SpanRef:
    idx = block.text.find(needle)
    if idx < 0:
        idx = 0
    return SpanRef(doc.doc_id, block.block_id, idx, idx + len(needle))


# ---------------------------------------------------------------------------
# Check 3 — claims a reviewer will ask us to support
# ---------------------------------------------------------------------------

# Each pattern is a thing a rigorous reader underlines and writes "source?"
# beside. None of them is a fault on its own, which is why they are all
# QUERY and never CORRECTION.
CLAIM_PATTERNS: list[tuple[str, str, str]] = [
    ("superlative",
     r"\b(the\s+)?(leading|foremost|premier|best|largest|biggest|top|number\s+one|"
     r"unrivalled|unmatched|unparalleled|world[\s\-]class|award[\s\-]winning|"
     r"most\s+experienced|only\s+firm)\b",
     "A superlative is the first thing an evaluator tests. Either cite what "
     "supports it or drop it."),
    ("absolute",
     r"\b(always|never|all\s+of\s+our|every\s+single|without\s+exception|"
     r"100\s*%|zero\s+(defects?|incidents?|delays?)|fully\s+compliant|"
     r"guarantee[ds]?|ensures?\s+complete)\b",
     "An absolute claim is disproved by one counter-example. State the actual "
     "record instead."),
    ("unsourced_quantity",
     r"\b(?:over|more\s+than|in\s+excess\s+of|upwards\s+of)\s+[\d,]+\s+"
     r"(projects?|contracts?|staff|employees?|engineers?|years?|km|kilometres?|"
     r"million|billion)\b",
     "A quantity with no source is a claim, not a fact. Name the record it "
     "comes from."),
    ("certification",
     r"\b(iso\s*\d{4,5}|certified|accredited|licen[cs]ed)\b(?![^.]{0,80}"
     r"(certificate|no\.|number|ref|annex|appendix|attached))",
     "A certification claimed without its certificate reference is unverifiable "
     "on the page it appears."),
]

_CLAIM_RES = [(name, re.compile(rx, re.I), advice) for name, rx, advice in CLAIM_PATTERNS]


def check_claims(doc: Document, counter) -> list[Mark]:
    marks: list[Mark] = []
    for b in doc.blocks:
        for name, rx, advice in _CLAIM_RES:
            for m in rx.finditer(b.text):
                phrase = m.group(0).strip()
                marks.append(Mark(
                    mark_id=f"m{next(counter)}",
                    kind=QUERY,
                    rule=f"claim_{name}",
                    heading=f"Support this: “{phrase}”",
                    detail=advice,
                    evidence=(
                        "Matched a claim pattern in the submission's own text. "
                        "This is not a finding that the claim is untrue — it is a "
                        "finding that the document does not show why it is true."
                    ),
                    quote=_sentence_around(b.text, m.start(), m.end()),
                    spans=[SpanRef(doc.doc_id, b.block_id, m.start(), m.end())],
                    page=b.page,
                    block_id=b.block_id,
                    locator=locate(doc, b),
                    checklist_items=[15],
                ))
    return marks


def _sentence_around(text: str, start: int, end: int, width: int = 300) -> str:
    lo = text.rfind(".", 0, start) + 1
    hi = text.find(".", end)
    hi = len(text) if hi < 0 else hi + 1
    out = text[lo:hi].strip()
    return out[:width] if out else text[max(0, start - 60):end + 60].strip()


# ---------------------------------------------------------------------------
# Check 4 — content shared with a third party
# ---------------------------------------------------------------------------


def check_shared_content(
    doc: Document, submission_path: str, third_party_material: list[str], counter
) -> tuple[list[Mark], int, SkippedCheck | None]:
    """Shared-content check, run before submission rather than after award."""
    if not third_party_material:
        return [], 0, SkippedCheck(
            rule="shared_content",
            needs="material received from or shared with a named third party",
            consequence=("The mechanism that addresses independent bidding by a "
                         "subcontractor directly did not run. Checklist item 6 mechanism 4 is "
                         "unevaluated."),
        )

    from . import conflict

    marks: list[Mark] = []
    total = 0
    for material in third_party_material:
        result = conflict.scan(submission_path, material)
        other = os.path.basename(material)
        if result.errors:
            continue
        for match in result.exact_matches:
            total += 1
            block = _block_containing(doc, match["text"])
            marks.append(Mark(
                mark_id=f"m{next(counter)}",
                kind=CORRECTION,
                rule="shared_content",
                heading=f"{match['words']} words identical to {other}",
                detail=(
                    "This passage appears word for word in material a named third "
                    "party supplied to us while the bid was being prepared. Its origin "
                    "must be visible to the Compliance Officer before sending: rewrite "
                    "it, or record where it came from and why it is here."
                ),
                evidence=(
                    f"Exact match on {match['words']} normalised words against "
                    f"{other}. Reproduced verbatim above."
                ),
                quote=match["text"][:400],
                spans=[SpanRef(doc.doc_id, block.block_id, 0,
                               min(len(block.text), 400))] if block else [],
                page=block.page if block else 0,
                block_id=block.block_id if block else "",
                locator=locate(doc, block),
                checklist_items=[6, 7, 9, 15],
                triggers=["exact_passage_match"],
            ))
        for meta in result.metadata_matches:
            if meta["weight"] != "strong":
                continue
            total += 1
            marks.append(Mark(
                mark_id=f"m{next(counter)}",
                kind=CORRECTION,
                rule="shared_origin",
                heading=f"Shared document {meta['field']} with {other}",
                detail=(
                    f"This submission and {other} carry the same "
                    f"`{meta['field']}`: “{meta['value']}”. A section built from "
                    "material a third party supplied keeps that material's author or "
                    "template; its origin needs declaring."
                ),
                evidence=("Read from both documents' own properties. Neither value "
                          "was inferred from the text."),
                quote="",
                spans=[],
                page=0,
                checklist_items=[6, 15],
                triggers=["metadata_origin_match"],
            ))
    return marks, total, None


def _block_containing(doc: Document, needle: str):
    probe = " ".join(needle.split()[:8]).lower()
    for b in doc.blocks:
        if probe and probe in " ".join(b.text.split()).lower():
            return b
    return None


# ---------------------------------------------------------------------------
# Check 5 — parties named in the submission but not declared
# ---------------------------------------------------------------------------


# A company suffix comes in two strengths, and conflating them is what makes
# naive firm detection useless in an engineering proposal.
#
# STRONG suffixes are legal forms. Nothing but a company carries one, so a
# single capitalised word in front is enough: "Granite Ltd" is a firm.
#
# WEAK suffixes are trade descriptors. They end plenty of real firm names
# ("Northern Survey Contractors") but they also end job titles ("Resident
# Engineer") and ordinary prose ("our consultants"). Two capitalised words in
# front are required before one counts, and any candidate containing a role
# word is rejected outright.
STRONG_SUFFIX = (
    r"(?:S\.?A\.?L|S\.?A\.?R\.?L|L\.?L\.?C|Ltd|Limited|Inc|Incorporated|"
    r"PLC|GmbH|W\.?L\.?L|F\.?Z\.?C|F\.?Z\.?E|JSC|SpA|SRL|Pte|Co\.|Company|"
    r"Corporation|Establishment)"
)
WEAK_SUFFIX = (
    r"(?:Consultants?|Contractors?|Engineers?|Architects?|Partners|Associates|"
    r"Group|Holdings?|Services|Solutions|Laboratories|Surveyors)"
)

# A name token, allowing the connectors that appear inside firm names.
_TOKEN = r"[A-Z][\w'\-\.]*"
_CONNECTOR = r"(?:\s+(?:&|and|of|for|el|al|de|van|von)\b)?"

FIRM_STRONG_RE = re.compile(
    rf"\b((?:{_TOKEN}{_CONNECTOR}\s+){{1,5}}{STRONG_SUFFIX})\b"
)
FIRM_WEAK_RE = re.compile(
    rf"\b((?:{_TOKEN}{_CONNECTOR}\s+){{2,5}}{WEAK_SUFFIX})\b"
)


def _candidate_firms(text: str) -> list[tuple[str, int, int]]:
    """(name, start, end) for firm-shaped strings, longest match preferred."""
    hits: list[tuple[str, int, int]] = []
    for rx in (FIRM_STRONG_RE, FIRM_WEAK_RE):
        for m in rx.finditer(text):
            hits.append((m.group(1).strip(), m.start(1), m.end(1)))
    # Prefer the longest span when two patterns overlap, so a firm is never
    # reported under a truncated version of its own name.
    hits.sort(key=lambda h: (h[1], -(h[2] - h[1])))
    kept: list[tuple[str, int, int]] = []
    for name, a, b in hits:
        if any(a >= ka and b <= kb for _, ka, kb in kept):
            continue
        kept.append((name, a, b))
    return kept


def _is_own_firm(candidate_key: str, firm_keys: list[str]) -> bool:
    """True when a candidate is the firm itself under a partial name.

    A proposal names its own author repeatedly and often partially — the
    letterhead in full, a running header clipped. Every one of those must be
    silent, or the check drowns its real findings in the firm's own name.
    """
    for key in firm_keys:
        if not key or not candidate_key:
            continue
        if candidate_key in key or key in candidate_key:
            return True
    return False


def check_undeclared_parties(
    doc: Document,
    declared: list[str] | None,
    counter,
    firm_names: list[str] | None = None,
) -> tuple[list[Mark], list[str], SkippedCheck | None]:
    """Third parties the submission names that the gate has never heard of.

    Produces `undeclared_party_in_submission`, which `config/checklist.example.yaml` names as
    an escalation trigger for item 11 and which nothing else in this system
    generates.
    """
    if declared is None:
        return [], [], SkippedCheck(
            rule="undeclared_parties",
            needs="the pursuit's declared parties, from the register",
            consequence=("Firms named in the submission were not checked against "
                         "the parties declared to the Compliance Officer. "
                         "Checklist item 11 is unevaluated."),
        )

    from .db import _normalise_name

    known = {_normalise_name(n) for n in declared}
    firm_keys = [_normalise_name(n) for n in (firm_names or [])]

    marks: list[Mark] = []
    undeclared: list[str] = []
    seen: set[str] = set()

    for b in doc.blocks:
        for name, start, end in _candidate_firms(b.text):
            if ROLE_WORDS.search(name):
                continue
            key = _normalise_name(name)
            if not key or key in seen:
                continue
            if _is_own_firm(key, firm_keys):
                continue
            # A declared party named more fully or more briefly in the prose is
            # still that party.
            if any(key == k or key in k or k in key for k in known if k):
                continue
            seen.add(key)
            undeclared.append(name)
            marks.append(Mark(
                mark_id=f"m{next(counter)}",
                kind=CORRECTION,
                rule="undeclared_party",
                heading=f"{name} is not declared",
                detail=(
                    f"The submission names {name}, which is not recorded as a "
                    "subcontractor, consortium partner, agent or intermediary on "
                    "this pursuit. Every party involved must be declared to the "
                    "Compliance Officer before submission."
                ),
                evidence=("Compared against the parties named on this pursuit in the "
                          "register. If this is not a counterparty — a client "
                          "reference or a past project — say so and it can be "
                          "recorded as such."),
                quote=_sentence_around(b.text, start, end),
                spans=[SpanRef(doc.doc_id, b.block_id, start, end)],
                page=b.page,
                block_id=b.block_id,
                locator=locate(doc, b),
                checklist_items=[11],
                triggers=["undeclared_party_in_submission"],
            ))
    return marks, undeclared, None


# ---------------------------------------------------------------------------
# The review
# ---------------------------------------------------------------------------


def review_submission(
    submission_path: str,
    *,
    solicitation_requirements: list[Requirement] | None = None,
    solicitation_id: str | None = None,
    staff_register: list[dict] | None = None,
    third_party_material: list[str] | None = None,
    declared_parties: list[str] | None = None,
    firm_names: list[str] | None = None,
) -> SubmissionReview:
    """Mark up the firm's own submission.

    Every argument that is `None` disables a check and records why on the
    rubric. Passing nothing produces a review that marks claims only, and says
    plainly that it did nothing else — which is a true statement about a thin
    run, not a clean bill of health.
    """
    from .ingest import ingest
    from itertools import count

    counter = count(1)
    doc = ingest(submission_path)
    review = SubmissionReview(
        submission_path=os.path.abspath(submission_path),
        submission_id=doc.doc_id,
    )
    rubric = Rubric(
        submission=os.path.basename(submission_path),
        submission_id=doc.doc_id,
        solicitation_id=solicitation_id,
    )
    blocking = [e for e in doc.errors if e.get("blocking")]
    if blocking:
        review.notes.append(
            f"The submission could not be read: {blocking[0].get('message')}. "
            "No mark below should be read as a statement about its contents."
        )
        review.rubric = rubric
        return review

    marks: list[Mark] = []

    # 1 — coverage
    if solicitation_requirements is not None:
        wanted = deliverables(solicitation_requirements)
        cov_marks, addressed, unaddressed = check_coverage(doc, wanted, counter)
        marks += cov_marks
        rubric.mandatory_total = len(wanted)
        rubric.mandatory_addressed = addressed
        rubric.mandatory_unaddressed = unaddressed
        rubric.checks_run.append("requirement_coverage")
    else:
        rubric.checks_skipped.append(SkippedCheck(
            rule="requirement_coverage",
            needs="the solicitation, reviewed, so its deliverables are known",
            consequence=("The submission was not checked against anything the client "
                         "asked for."),
        ))

    # 2 — personnel
    p_marks, verified, unverified, skipped = check_personnel(doc, staff_register, counter)
    marks += p_marks
    rubric.personnel_named = len(named_persons(doc))
    rubric.personnel_verified = verified
    rubric.personnel_unverified = unverified
    (rubric.checks_skipped.append(skipped) if skipped
     else rubric.checks_run.append("personnel_register"))

    # 3 — claims. Always runs; it needs no source but the document itself.
    c_marks = check_claims(doc, counter)
    marks += c_marks
    rubric.claims_queried = len(c_marks)
    rubric.checks_run.append("claims")

    # 4 — shared content
    s_marks, shared_n, skipped = check_shared_content(
        doc, submission_path, third_party_material or [], counter)
    marks += s_marks
    rubric.shared_passages = shared_n
    (rubric.checks_skipped.append(skipped) if skipped
     else rubric.checks_run.append("shared_content"))

    # 5 — undeclared parties
    u_marks, undeclared, skipped = check_undeclared_parties(
        doc, declared_parties, counter, firm_names=firm_names)
    marks += u_marks
    rubric.undeclared_parties = undeclared
    (rubric.checks_skipped.append(skipped) if skipped
     else rubric.checks_run.append("undeclared_parties"))

    # Corrections first, then queries, then what is right. A reviewer with ten
    # minutes must meet the faults before the credit.
    order = {k: i for i, k in enumerate(MARK_ORDER)}
    marks.sort(key=lambda m: (order[m.kind], m.page, m.block_id))
    review.marks = marks
    review.rubric = rubric

    if rubric.checks_skipped:
        review.notes.append(
            f"{len(rubric.checks_skipped)} of 5 checks did not run. A mark that was "
            "never made is not a passage that was found sound."
        )
    return review
