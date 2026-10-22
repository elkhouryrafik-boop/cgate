"""
cgate.spec — compile a solicitation into a checkable specification.

Why this exists
---------------
`extract.py` asks each clause whether it is about one of fifteen integrity
subjects. That is the right question for checklist item 12 and the wrong one for
the document as a whole, because the answer is bounded by the list: a provision
the lexicon does not name is not scored low, it is never seen at all. The blind
test run measured the cost of that — six planted defects, none caught, none
of them on any integrity subject and three of them not properties of a single
clause in the first place.

So this module reads the document the way a compiler reads source. Every
sentence that carries a modal becomes a `Commitment`; every quantity becomes a
`Measurand` carrying the words around it; every heading becomes a section with a
kind. Nothing here asks what a provision is *about*. `lint.py` then checks the
compiled form for the things that go wrong between clauses rather than inside
one: two values for the same quantity, a column that does not total, a trigger
owned by somebody else, a requirement stated exactly once where nobody reads.

Everything the compiler emits carries `SpanRef`s into the source blocks, so the
grounding rule of the rest of the tool holds unchanged: a finding that cannot be
quoted back at its offsets is deleted, not downgraded.

Coverage
--------
The compiler reports the blocks it did not compile, by id and by text. This is
the property a lexicon-matched pipeline cannot have. A reviewer relying on the
output can read what was skipped and decide whether the skip mattered, which is
the only honest form of "what did it miss?" available to a deterministic tool.
"""

from __future__ import annotations

import re
from datetime import date

from .model import (
    Actor,
    Block,
    Commitment,
    Document,
    Measurand,
    Modality,
    SpanRef,
    SpecResult,
)

# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "by", "for",
    "with", "from", "as", "is", "are", "be", "been", "was", "were", "that",
    "this", "these", "those", "which", "it", "its", "any", "all", "not", "no",
    "such", "shall", "will", "must", "may", "can", "should", "would", "each",
    "per", "than", "then", "there", "their", "have", "has", "had", "but", "if",
    "into", "under", "over", "within", "upon", "other", "another", "more",
    "most", "less", "least", "own", "same", "so", "only", "also", "where",
    "when", "who", "whom", "whose", "what", "how", "including", "include",
    "includes", "shall's", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "hrs", "etc",
}

_SUFFIXES = ("ing", "edly", "ity", "ies", "ed", "ly", "es")


def stem(word: str) -> str:
    """A deliberately small stemmer.

    It exists to make "validity" and "valid" the same token, and to make "days"
    and "day" the same token. It is not linguistics; it is a normalisation whose
    every rule can be read off this list and reproduced by hand years from now,
    which is the property that matters for a tool whose output is filed.
    """
    w = word.lower().strip("'")
    if w.endswith("'s"):
        w = w[:-2]
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            if suf == "ies":
                w += "y"
            break
    else:
        if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    return w


_WORD = re.compile(r"[A-Za-z][A-Za-z\-']*")


def content_stems(text: str, limit: int | None = None) -> list[str]:
    out: list[str] = []
    for m in _WORD.finditer(text):
        w = m.group(0)
        if len(w) < 3 or w.lower() in STOPWORDS:
            continue
        s = stem(w)
        if s and s not in out:
            out.append(s)
            if limit and len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

# A section's *kind* is what the document uses it for, read off its own heading.
# The order matters: the first pattern that matches wins, so the more specific
# heading words come first. These are heading conventions, not subject matter —
# every solicitation has a place where the submission requirements live, and the
# checks in lint.py care only about whether a requirement is in that place.
SECTION_KINDS: list[tuple[str, str]] = [
    ("submission", r"mandatory submission|submission requirement|documents? to be submitted"
                   r"|contents? of (?:the )?(?:proposal|bid|tender)|required documents?"
                   r"|proposal (?:contents|package)"),
    ("personnel", r"key personnel|key staff|personnel|staffing|team composition|experts?\b"),
    ("evaluation", r"evaluation|technical criteri|scoring|award and|selection method"),
    ("eligibility", r"eligib|qualification criteri|exclusion|disqualif"),
    ("price", r"financial proposal|price|pricing|payment|currency|remuneration"),
    ("instructions", r"instructions? to bidders|bid validity|bid security|submission"
                     r"|clarification|late, modified|format|data sheet"
                     r"|letter of invitation|invitation to (?:bid|tender)"
                     r"|preparation of (?:proposals|bids)|returnable|bidding forms"
                     r"|opening"),
    ("ethics", r"code of conduct|ethic|conflict of interest|anti-?corruption|fraud"
               r"|safeguard|sanction"),
    ("scope", r"scope of work|terms of reference|part [ab]\b|services|deliverable"
              r"|objective|duration|schedule"),
    ("terms", r"contract terms|conditions|governing law|dispute|termination|liquidated"
              r"|confidentialit|intellectual property|liabilit"),
    ("front", r"introduction|background|contact"),
]

_HEADING_NUM = re.compile(r"^((?:annex|appendix|schedule)\s+[a-z0-9]+|\d{1,2}(?:\.\d{1,3})*)",
                          re.IGNORECASE)


class Section:
    __slots__ = ("number", "heading", "kind", "start_order", "end_order", "depth")

    def __init__(self, number: str | None, heading: str, kind: str, start_order: int,
                 depth: int):
        self.number = number
        self.heading = heading
        self.kind = kind
        self.start_order = start_order
        self.end_order = 10**9
        self.depth = depth

    def label(self) -> str:
        return f"{self.number} {self.heading}".strip() if self.number else self.heading

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "heading": self.heading,
            "kind": self.kind,
            "depth": self.depth,
            "start_order": self.start_order,
            "end_order": self.end_order,
        }


def _kind_of(heading: str) -> str | None:
    h = heading.lower()
    for kind, pattern in SECTION_KINDS:
        if re.search(pattern, h):
            return kind
    return None


def sections(doc: Document) -> list[Section]:
    """Every heading block, with the use the document puts that section to.

    A subsection with an unrecognised heading inherits its parent's kind, so
    "7.2 Payment Schedule" under "7. Contract Terms" is still priced work, and a
    heading like "3.3 Deliverables and Indicative Schedule" does not fall
    through to nothing merely because its own words are unusual.
    """
    out: list[Section] = []
    for b in doc.blocks:
        if b.kind != "heading":
            continue
        text = b.text.strip()
        m = _HEADING_NUM.match(text)
        number = m.group(1) if m else None
        title = text[m.end():].strip(" .-—:") if m else text
        depth = (number.count(".") + 1) if number and "." in number else 1
        kind = _kind_of(title) or _kind_of(text)
        if kind is None and number and "." in number:
            parent = number.rsplit(".", 1)[0]
            for s in reversed(out):
                if s.number == parent:
                    kind = s.kind
                    break
        out.append(Section(number, title, kind or "other", b.order, depth))
    for i, s in enumerate(out):
        if i + 1 < len(out):
            s.end_order = out[i + 1].start_order
    return out


def section_at(secs: list[Section], order: int) -> Section | None:
    """The deepest section containing this block."""
    found = None
    for s in secs:
        if s.start_order <= order < s.end_order:
            found = s
    return found


# ---------------------------------------------------------------------------
# Sentences
# ---------------------------------------------------------------------------

# Split on end punctuation followed by a capital or an opening bracket, but not
# where the full stop belongs to a number ("USD 1,500,000.") or an initial.
_SENT_END = re.compile(r"(?<=[.;])\s+(?=[A-Z(•])")
_ABBREV_TAIL = re.compile(r"(?:\b[A-Z]|\bNo|\bSect|\bArt|\bAnnex)\.$")


def sentences(text: str) -> list[tuple[int, int, str]]:
    """(start, end, text) for each sentence, offsets into `text`."""
    out: list[tuple[int, int, str]] = []
    pos = 0
    for m in _SENT_END.finditer(text):
        head = text[pos : m.start()]
        if _ABBREV_TAIL.search(head.rstrip()):
            continue
        if head.strip():
            out.append((pos, m.start(), head))
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        out.append((pos, len(text), tail))
    return out


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------

CURRENCIES = {
    "usd": "USD", "us$": "USD", "$": "USD", "united states dollars": "USD",
    "eur": "EUR", "€": "EUR", "gbp": "GBP", "£": "GBP", "chf": "CHF",
}

_DURATION_UNITS = {
    "day": 1.0, "days": 1.0,
    "working day": 1.0, "working days": 1.0,
    "business day": 1.0, "business days": 1.0,
    "calendar day": 1.0, "calendar days": 1.0,
    "week": 7.0, "weeks": 7.0,
    "month": 30.0, "months": 30.0,
    "year": 365.0, "years": 365.0,
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_MONEY = re.compile(
    r"(?:(USD|US\$|EUR|GBP|CHF|\$|€|£)\s?(\d[\d,]*(?:\.\d+)?)"
    r"|(\d[\d,]*(?:\.\d+)?)\s?(USD|EUR|GBP|CHF|United States Dollars))",
    re.IGNORECASE,
)
# Tenders write quantities twice — "ninety (90) days", "twenty percent (20%)",
# "two (2) contracts". The digits live inside the brackets, so a pattern that
# insists on whitespace either side of them reads none of these.
_PERCENT = re.compile(r"\(?(\d+(?:\.\d+)?)\)?\s?(?:%|per\s?cent\b)", re.IGNORECASE)
_DURATION = re.compile(
    r"\(?(\d+(?:\.\d+)?)\)?\s+((?:working|business|calendar)\s+)?"
    r"(day|days|week|weeks|month|months|year|years)\b",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+(\d{4})\b",
    re.IGNORECASE,
)
_COUNT = re.compile(r"\(?\b(\d+)\)?\s+([a-z][a-z\-]{3,})\b")

# Clock times, not durations. Tenders write deadlines as "no later than
# 30 October 2026 at 1600 hours (local time)" and office hours as "0800 hours to
# 1630 hours". Read as a quantity, "1600 hours" is a count of 1600 hours, and two
# deadlines at different times of day then look like two different answers to one
# question — `value_conflict` reported a 4pm deadline and a 10am deadline in
# separate notices as a contradiction. The tell is structural: a four-digit time
# has a leading zero or is introduced by "at", and is qualified as local time far
# more often than a genuine duration is.
_CLOCK_NOUNS = {"hour", "hours", "hrs", "h"}
# "at 1600 hours", "to 1630 hours", "from 0800 hours" — the prepositions that
# introduce a time of day. "1600 hours of expert input" has none of them.
_CLOCK_LEAD = re.compile(r"\b(?:at|to|from|between|until|till|by)\s*$")


def _is_clock_time(text: str, m: re.Match, noun: str) -> bool:
    if noun not in _CLOCK_NOUNS:
        return False
    digits = m.group(1)
    after = text[m.end():m.end() + 20].lower()
    before = text[max(0, m.start() - 12):m.start()].lower()
    if "local time" in after or re.match(r"\s*\(?\s*local\b", after):
        return True
    if _CLOCK_LEAD.search(before):
        return True
    # A four-digit clock always starts with 0, 1 or 2, and the value has to be a
    # possible time. "1630" in "0800 hours to 1630 hours" is caught by _CLOCK_LEAD
    # via "to"; this branch catches a lone leading-zero time at a sentence start.
    return len(digits) == 4 and digits.startswith("0")

# Numbers that address the document rather than measure anything.
# The deliverable-code case ("D1", "D8") is the reason for the second branch,
# and it has to stay glued to the digit: written as a bare `d` with optional
# whitespace it also matched the "d " of "and 30%", and silently dropped every
# number in the document that happened to follow a word ending in d.
_REF_LEAD = re.compile(
    r"(?:\b(?:sections?|articles?|clauses?|annexe?s?|appendix|schedules?"
    r"|paragraphs?|items?|figure|table|part|no\.|number)\s*$"
    r"|\bD$)",
    re.IGNORECASE,
)
_LIST_MARKER = re.compile(r"^\s*\(?\d{1,2}[.)]\s")
# The "8" of "Article 6.8" is the tail of one number, not a number of its own.
_DOTTED_TAIL = re.compile(r"\d[.\-/]$")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _literals(text: str) -> list[tuple[int, int, str, float, str, str]]:
    """(start, end, kind, value, unit, raw) for every measured quantity."""
    found: list[tuple[int, int, str, float, str, str]] = []
    taken: list[tuple[int, int]] = []

    def free(s: int, e: int) -> bool:
        return not any(s < te and ts < e for ts, te in taken)

    def take(s: int, e: int, kind: str, value: float, unit: str) -> None:
        if not free(s, e):
            return
        if _REF_LEAD.search(text[max(0, s - 14) : s]):
            return
        if _DOTTED_TAIL.search(text[:s]):
            return
        taken.append((s, e))
        found.append((s, e, kind, value, unit, text[s:e]))

    for m in _DATE.finditer(text):
        try:
            d = date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            continue
        take(m.start(), m.end(), "date", float(d.toordinal()), "date")

    for m in _MONEY.finditer(text):
        cur = (m.group(1) or m.group(4) or "").lower().strip()
        raw_n = m.group(2) or m.group(3)
        take(m.start(), m.end(), "money", _num(raw_n), CURRENCIES.get(cur, cur.upper()))

    for m in _PERCENT.finditer(text):
        take(m.start(), m.end(), "percent", _num(m.group(1)), "%")

    for m in _DURATION.finditer(text):
        qual = (m.group(2) or "").strip().lower()
        noun = m.group(3).lower()
        unit = f"{qual} {noun}".strip()
        days = _DURATION_UNITS[noun] * _num(m.group(1))
        take(m.start(), m.end(), "duration", days, unit.rstrip("s") or noun)

    for m in _COUNT.finditer(text):
        noun = m.group(2).lower()
        if noun in _DURATION_UNITS or noun in ("percent", "per"):
            continue
        v = _num(m.group(1))
        if 1900 <= v <= 2100 and len(m.group(1)) == 4:
            continue
        if _is_clock_time(text, m, noun):
            continue
        take(m.start(), m.end(), "count", v, stem(noun))

    found.sort(key=lambda t: t[0])
    return found


# Unit vocabulary: words that name a currency or a magnitude rather than the
# thing being counted. Used to keep unit words out of a measurand's subject, so
# two figures are never treated as "the same quantity" merely because both are
# quoted in dollars. Time units (day, week, month, year) are deliberately absent:
# a 90-day and a 120-day validity period really are two answers to one question,
# and the word "days" is the identity there, not noise.
_UNIT_WORDS = {
    # currencies, spelled out
    "dollar", "dollars", "euro", "euros", "pound", "pounds", "sterling",
    "metical", "meticais", "lari", "birr", "rupee", "rupees", "naira",
    "shilling", "shillings", "rand", "cedi", "dirham", "dirhams", "riyal",
    "riyals", "dinar", "dinars", "taka", "afghani", "franc", "francs",
    "zloty", "koruna", "forint", "ruble", "rouble", "hryvnia", "yuan",
    "renminbi", "yen", "won", "peso", "pesos", "real", "reais", "bolivar",
    "guarani", "colon", "quetzal", "lempira", "cordoba", "balboa", "gourde",
    # currency codes
    "usd", "eur", "gbp", "mzn", "gel", "etb", "inr", "ngn", "kes", "tzs",
    "ugx", "zar", "ghs", "aed", "sar", "kwd", "egp", "bdt", "afn", "xof",
    "xaf", "pln", "czk", "huf", "rub", "uah", "cny", "rmb", "jpy", "krw",
    "mxn", "brl", "cad", "aud", "nzd", "chf", "sek", "nok", "dkk", "idr",
    "myr", "php", "thb", "vnd", "pkr", "lkr", "npr", "irr", "iqd", "jod",
    "lbp", "mad", "tnd", "dzd", "lyd", "sdg", "yer", "omr", "bhd", "qar",
    # magnitudes and unit words
    "hundred", "thousand", "million", "billion", "trillion", "lakh", "crore",
    "unit", "united", "states",
}
_UNIT_NOISE = {stem(w) for w in _UNIT_WORDS}


def subject_of(text: str, start: int, end: int, left: int = 12, right: int = 8) -> list[str]:
    """The stemmed words a literal is surrounded by — what it measures.

    Deliberately naive and deliberately symmetric. "a minimum average annual
    turnover of USD 750,000" and "average annual turnover of not less than USD
    900,000" share `average annual turnover` whichever side of the number the
    phrase sits on, and that shared subject is the whole basis on which lint.py
    decides two numbers are talking about the same thing.

    Unit vocabulary is stripped. The words of a currency or a magnitude describe
    the *unit* of a figure, not the quantity it counts: "USD 100" and
    "MZN 4,000,000" share `usd`, `dollar`, `thousand` and `unit` while measuring
    completely different things. Leaving them in made `value_conflict` link
    unrelated figures on their currency vocabulary alone and report a conflict
    whose subject read `dollar, hundr, meticai, mzn, thousand` — a subject made
    entirely of units is evidence the two figures are *not* the same quantity.
    """
    before = " ".join(_WORD.findall(text[:start])[-left:])
    after_src = re.split(r"[;:|]", text[end:])[0]
    after = " ".join(_WORD.findall(after_src)[:right])
    # Ordered by proximity, nearest word first. The words touching a number
    # identify it; the ones twelve tokens away are context. lint.py relies on
    # that ordering to tell "average annual turnover of USD 750,000" from a
    # different amount that merely shares the word "contract" further upstream.
    near = list(reversed(content_stems(before)))
    for s in content_stems(after):
        if s not in near:
            near.append(s)
    return [s for s in near if s not in _UNIT_NOISE]


# ---------------------------------------------------------------------------
# Modality and actor
# ---------------------------------------------------------------------------

_PROHIBITION = re.compile(
    r"\b(?:shall|must|will|may|can)\s+not\b|\bis\s+prohibited\b|\bare\s+prohibited\b"
    r"|\bnot\s+(?:be\s+)?(?:eligible|permitted|allowed)\b|\bno\s+bidder\s+(?:shall|may)\b",
    re.IGNORECASE,
)
_OBLIGATION = re.compile(
    r"\b(?:shall|must|is\s+required\s+to|are\s+required\s+to|is\s+to\s+be|are\s+to\s+be"
    r"|undertakes?\s+to|has\s+to|have\s+to)\b|\bwill\s+be\s+(?:rejected|required)\b"
    r"|\brequired\s+to\s+(?:submit|provide|demonstrate|maintain)\b",
    re.IGNORECASE,
)
_PERMISSION = re.compile(r"\b(?:may|is\s+entitled\s+to|at\s+its\s+(?:sole\s+)?discretion)\b",
                         re.IGNORECASE)
_CONDITION = re.compile(
    r"\bto\s+be\s+eligible\b|\bin\s+order\s+to\s+qualify\b|\bto\s+qualify\b"
    r"|\beligibility\s+(?:requires|criteria)\b|\bmust\s+demonstrate\b",
    re.IGNORECASE,
)

_US = re.compile(
    r"\b(?:bidder|bidders|consultant|contractor|offeror|proposer|tenderer|supplier"
    r"|firm|firms|applicant|joint\s+venture|lead\s+member|proposals?|bids?)\b",
    re.IGNORECASE,
)
_SUB = re.compile(r"\b(?:sub-?contractor|sub-?consultant|agent|intermediar|consortium"
                  r"|associate)\w*\b", re.IGNORECASE)
_PERSONNEL = re.compile(r"\b(?:personnel|staff|team\s+member|key\s+person|expert|"
                        r"specialist|engineer|surveyor|leader|clerk)\w*\b", re.IGNORECASE)
_CLIENT = re.compile(
    r"\b(?:foundation|employer|purchaser|client|organisation|organization|agency"
    r"|ministry|procuring\s+entity|contracting\s+authority)\b",
    re.IGNORECASE,
)


def modality_of(sentence: str) -> Modality:
    if _PROHIBITION.search(sentence):
        return Modality.PROHIBITION
    if _OBLIGATION.search(sentence):
        return Modality.OBLIGATION
    if _CONDITION.search(sentence):
        return Modality.CONDITION
    if _PERMISSION.search(sentence):
        return Modality.PERMISSION
    # A sentence that voids a proposal binds it whether or not it troubles to
    # say "shall". "Proposals quoted in another currency will be treated as
    # non-responsive" carries no modal verb and ends the bid; read as unmodalised
    # prose it disappears, which is how the blind-test currency rule was missed.
    if DISQUALIFYING.search(sentence):
        return Modality.CONDITION
    return Modality.UNKNOWN


def actor_of(sentence: str) -> Actor:
    """Who the sentence binds, decided on the words before the modal.

    Text after the modal is the obligation's object and routinely names the
    other party ("...with the Foundation's prior written approval"), so reading
    the whole sentence would attribute half the bidder's duties to the client.
    """
    m = re.search(r"\b(?:shall|must|may|will|is\s+required|are\s+required)\b",
                  sentence, re.IGNORECASE)
    head = sentence[: m.start()] if m else sentence
    if _SUB.search(head):
        return Actor.SUBCONTRACTOR
    if _US.search(head):
        return Actor.CONTRACTOR
    if _PERSONNEL.search(head):
        return Actor.PERSONNEL
    if _CLIENT.search(head):
        return Actor.UN
    if _US.search(sentence):
        return Actor.CONTRACTOR
    if _CLIENT.search(sentence):
        return Actor.UN
    return Actor.UNKNOWN


DISQUALIFYING = re.compile(
    r"\bwill\s+be\s+rejected\b|\bshall\s+be\s+rejected\b|\bnon-?responsive\b"
    r"|\bresult\s+in\s+rejection\b|\bbe\s+disqualified\b|\bgrounds?\s+for\s+"
    r"(?:rejection|disqualification)\b|\bnot\s+be\s+considered\b"
    r"|\bwill\s+not\s+be\s+(?:accepted|evaluated)\b|\bare\s+not\s+eligible\b",
    re.IGNORECASE,
)

# A sentence that ends a bid, and a sentence about how many points it scores,
# are different animals. Minimum-score thresholds live in evaluation sections by
# nature; a currency rule that voids a bid does not.
SCORING = re.compile(r"\b(?:score|scores|scoring|points?|marks?|weighting|ranked?|"
                     r"ranking|threshold)\b", re.IGNORECASE)

# The stem of a list: "the Consultant shall:" governs the items beneath it.
_LIST_STEM = re.compile(r":\s*$")


# ---------------------------------------------------------------------------
# The compiler
# ---------------------------------------------------------------------------


def _is_list_item(text: str) -> bool:
    return bool(_LIST_MARKER.match(text) or re.match(r"^\s*[•\-–]\s", text)
                or re.match(r"^\s*\([a-z]{1,3}\)\s", text))


def compile_spec(doc: Document) -> SpecResult:
    """Compile a document into measurands, commitments, sections and coverage."""
    secs = sections(doc)
    res = SpecResult(
        doc_id=doc.doc_id,
        doc_path=doc.path,
        blocks_total=len(doc.blocks),
        sections=[s.to_dict() for s in secs],
    )

    compiled: set[str] = set()
    carry: tuple[Modality, Actor, str] | None = None
    n_m = n_c = 0

    for b in doc.blocks:
        sec = section_at(secs, b.order)
        sec_label = sec.label() if sec else "(front matter)"
        sec_kind = sec.kind if sec else "front"

        if b.kind == "heading":
            carry = None
            compiled.add(b.block_id)
            continue

        # --- measurands ------------------------------------------------------
        for s, e, kind, value, unit, raw in _literals(b.text):
            if _LIST_MARKER.match(b.text) and s <= 1:
                continue
            n_m += 1
            res.measurands.append(
                Measurand(
                    m_id=f"m{n_m:04d}",
                    kind=kind,
                    value=value,
                    unit=unit,
                    raw=raw,
                    subject=subject_of(b.text, s, e),
                    spans=[SpanRef(doc.doc_id, b.block_id, s, e)],
                    block_id=b.block_id,
                    section=sec_label,
                    section_kind=sec_kind,
                    quote=raw,
                )
            )
            compiled.add(b.block_id)

        # --- commitments -----------------------------------------------------
        stem_here = _LIST_STEM.search(b.text) and modality_of(b.text) is not Modality.UNKNOWN
        for s, e, sent in sentences(b.text):
            mod = modality_of(sent)
            act = actor_of(sent)
            inherited = False
            if mod is Modality.UNKNOWN and carry and _is_list_item(b.text):
                mod, act, _ = carry
                inherited = True
            if mod is Modality.UNKNOWN:
                continue
            n_c += 1
            ms = [
                m.m_id
                for m in res.measurands
                if m.block_id == b.block_id
                and m.spans[0].start >= s
                and m.spans[0].end <= e
            ]
            dq = bool(DISQUALIFYING.search(sent))
            res.commitments.append(
                Commitment(
                    c_id=f"k{n_c:04d}",
                    modality=mod,
                    actor=act,
                    text=sent.strip(),
                    spans=[SpanRef(doc.doc_id, b.block_id, s, e)],
                    block_id=b.block_id,
                    section=sec_label,
                    section_kind=sec_kind,
                    lemmas=content_stems(sent),
                    disqualifying=dq,
                    measurands=ms,
                    modal_inherited=inherited,
                    quote=sent.strip(),
                )
            )
            compiled.add(b.block_id)

        if stem_here:
            carry = (modality_of(b.text), actor_of(b.text), b.block_id)
            compiled.add(b.block_id)
        elif b.kind != "table" and not _is_list_item(b.text):
            # An ordinary paragraph ends the list; table rows do not, because a
            # table under "the Consultant shall:" is still that list.
            carry = None

    res.blocks_compiled = len(compiled)
    res.uncompiled = [
        {"block_id": b.block_id, "kind": b.kind, "order": b.order,
         "section": (section_at(secs, b.order).label() if section_at(secs, b.order)
                     else "(front matter)"),
         "text": b.text[:240]}
        for b in doc.blocks
        if b.block_id not in compiled
    ]
    return res


def tables(doc: Document) -> list[list[Block]]:
    """Consecutive table blocks, regrouped into the tables they came from.

    Ingest emits one block per row and keeps no table identity, so the grouping
    is recovered from reading order and a stable column count. A row that splits
    the run — a heading, a paragraph — ends the table.
    """
    out: list[list[Block]] = []
    run: list[Block] = []
    for b in doc.blocks:
        if b.kind == "table":
            if run and len(b.text.split(" | ")) != len(run[-1].text.split(" | ")):
                if len(run) > 1:
                    out.append(run)
                run = [b]
            else:
                run.append(b)
        else:
            if len(run) > 1:
                out.append(run)
            run = []
    if len(run) > 1:
        out.append(run)
    return out
