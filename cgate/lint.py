"""
cgate.lint — checks over the compiled specification.

Each check below answers a question that cannot be asked of one clause at a
time, which is why a clause-by-clause matcher cannot fail them and cannot pass
them either. They are ordered by what they read:

    value_conflict          two numbers, one quantity
    date_arithmetic         which of those numbers the document's own dates pick
    table_sum               a column that does not add up
    foreign_trigger         a payment we cannot cause
    disqualifying_rule      a bid-ending rule filed where nobody looks for one
    personnel_requirement   a duty on a named role, stated away from that role
    single_point_obligation a binding requirement the document says exactly once
    dangling_reference      a pointer to a clause or annex that is not there

No check consults a subject lexicon and none consults a model. Every defect
quotes the source at the offsets it was found at, and a defect whose quotes do
not resolve is dropped by `ground_defects` on the way out — the same rule the
requirement extractor lives under.

Operating point
---------------
Deliberately biased toward over-flagging. A false flag costs a proposal
manager a minute; a bid validity period read as 90 days when the client's own
dates say 120 costs the bid. The report says how many defects each check
raised so the bias stays visible instead of being discovered.
"""

from __future__ import annotations

import re
from datetime import date

from .model import Defect, Document, Measurand, SpanRef, SpecResult
from .spec import (
    SCORING,
    Section,
    content_stems,
    section_at,
    sections,
    stem,
    tables,
)

# Where a requirement is allowed to live without comment. A rule that ends a bid
# belongs in the pages a bidder reads to find out what to submit; one that turns
# up in an evaluation annex or a terms-of-reference bullet is a trap whether or
# not anybody meant it as one.
ANCHOR_KINDS = {"submission", "eligibility", "instructions"}

# Section kinds that do not constitute a place in the document's own scheme.
# Three values mean "this document did not label this", and none of them is a
# place a rule could have been filed into or away from:
#   "front" — no heading at all above the block (spec.py's fallback), or an
#             introduction/background/contact heading;
#   "other" — a heading the tool does not recognise (sections()'s catch-all);
#   None    — a section whose kind was never resolved.
# "other" matters most: a notice's own title ("REQUEST FOR BIDS") is recovered as
# a heading and classified "other", so without it every paragraph in the document
# looks like it was filed under a named section, and a placement check reports
# the whole document as misfiled.
UNPLACED_KINDS = {"", "front", "other", None}

# Two subjects must share this many stems before their numbers are treated as
# measuring the same thing, and at least one shared stem must be rare enough in
# the document to carry identity. "days" is shared by every duration in a
# solicitation; "turnover" is shared by two clauses and means something.
MIN_SUBJECT_OVERLAP = 2
RARE_DF = 12
# The certificate, licence or cover a single-point obligation turns on must
# appear in no more than this many blocks for the obligation to count as stated
# once.
OBJECT_DF = 2

# A stem this rare identifies the quantity on its own, so two shared words are
# enough. Above it, three shared words are required before two numbers are
# treated as answers to the same question.
IDENTIFYING_DF = 4
# How many of the words closest to a number are treated as naming it.
NEAR = 4
# Below this many recovered clause numbers, the cross-reference check cannot
# tell a broken pointer from an unparsed one, and says so instead of guessing.
MIN_NUMBERING = 10

# Nouns that make a sentence a requirement to hold or produce something, as
# distinct from a task to perform. A singleton task in a terms of reference is
# ordinary; a singleton certificate is a disqualification waiting to happen.
OBJECT_NOUNS = re.compile(
    r"\b(?:insurance|indemnit\w*|certificate|certification|licen[cs]e|registration"
    r"|guarantee|bond|accreditation|membership|clearance|permit|warrant\w*|cover"
    r"|coverage|security|fluen\w*|conversant|proficien\w*|qualification"
    r"|authorisation|authorization|attestation|apostille|notaris\w*)\b",
    re.IGNORECASE,
)

CLIENT_AGENT = re.compile(
    r"\bby\s+(?:the\s+)?(?:Foundation|Employer|Purchaser|Client|Organisation"
    r"|Organization|Agency|Ministry|procuring\s+entity"
    r"|contracting\s+authority)\b",
    re.IGNORECASE,
)
DELIVERABLE_CODE = re.compile(r"\bD\d{1,2}\b")
OUR_ACT = re.compile(
    r"\b(?:consultant|contractor|bidder|supplier|we|our)\b", re.IGNORECASE
)

TOTAL_ROW = re.compile(r"\b(?:total|sum|subtotal|grand\s+total)\b", re.IGNORECASE)
PERCENT_HEADER = re.compile(r"%|per\s?cent|proportion|share\b", re.IGNORECASE)
PAYMENT_TABLE = re.compile(r"milestone|payment|instal?ment|% of contract", re.IGNORECASE)

SELF_ARITHMETIC = re.compile(
    r"\b(?:that\s+is|i\.e\.|namely)\W{0,3}(\d+)\s+"
    r"(day|days|week|weeks|month|months)\s+"
    r"(beyond|after|from|following|before|prior\s+to)\s+([^.);]{3,70})",
    re.IGNORECASE,
)
_UNIT_DAYS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30}

REF = re.compile(
    r"\b(Section|Sections|Clause|Clauses|Article|Articles|Annex|Annexes|Appendix"
    r"|Schedule|paragraph)\s+([A-Z]|\d{1,2}(?:\.\d{1,3})*)\b"
)


# ---------------------------------------------------------------------------


def _df(doc: Document) -> dict[str, int]:
    """Document frequency of every stem, counted in blocks."""
    df: dict[str, int] = {}
    for b in doc.blocks:
        for s in set(content_stems(b.text)):
            df[s] = df.get(s, 0) + 1
    return df


def _fmt(m: Measurand) -> str:
    if m.kind == "date":
        return date.fromordinal(int(m.value)).isoformat()
    if m.kind == "money":
        return f"{m.unit} {m.value:,.0f}"
    if m.kind == "percent":
        return f"{m.value:g}%"
    if m.kind == "duration":
        return f"{m.value:g} days"
    return f"{m.value:g} {m.unit}"


def _family(m: Measurand) -> str:
    """Values are only ever compared inside one family."""
    if m.kind == "money":
        return f"money:{m.unit}"
    if m.kind == "count":
        return f"count:{m.unit}"
    return m.kind


# ---------------------------------------------------------------------------
# 1 & 2 — one quantity, two numbers, and what the document's dates say about it
# ---------------------------------------------------------------------------


def _table_of(doc: Document) -> dict[str, int]:
    """block_id -> index of the table it is a row of."""
    out: dict[str, int] = {}
    for i, group in enumerate(tables(doc)):
        for b in group:
            out[b.block_id] = i
    return out


def _binding_blocks(doc: Document, spec: SpecResult) -> set[str]:
    """Blocks whose numbers commit somebody to something.

    Two figures that disagree matter when at least one of them is load-bearing.
    A quantity inside a sentence with a modal is load-bearing; so is a value in
    a two-column key/value table, which is how every solicitation states its own
    deadlines and validity periods. Two learner counts in a background paragraph
    describing two different schools are not in disagreement at all, and this is
    what keeps them out of the report.
    """
    out = {c.block_id for c in spec.commitments}
    for group in tables(doc):
        if len(_cells(group[0].text)) == 2:
            out.update(b.block_id for b in group)
    return out


# Order of precedence, as solicitations themselves declare it: special
# conditions defeat general ones, and a data sheet defeats the standing
# instructions it amends. Two values that disagree across that boundary are the
# document working as designed — but only if somebody notices which one governs,
# and a bid priced off the general condition is priced wrong either way.
PRECEDENCE: list[tuple[str, int, str]] = [
    (r"special conditions|special terms|particular conditions", 3,
     "Special Conditions"),
    (r"data sheet", 3, "the Data Sheet"),
    (r"general (?:terms and )?conditions|general terms", 1, "General Conditions"),
    (r"instructions? to bidders", 1, "the Instructions to Bidders"),
]


def _precedence_at(secs: list[Section], order: int) -> tuple[int, str]:
    """Rank of the instrument this block sits in, read off the headings above it."""
    for s in sorted((s for s in secs if s.start_order <= order),
                    key=lambda s: -s.start_order):
        for pattern, rank, name in PRECEDENCE:
            if re.search(pattern, s.label(), re.IGNORECASE):
                return rank, name
    return 2, ""


def check_value_conflicts(
    doc: Document, spec: SpecResult, df: dict[str, int],
    exclude: set[str] | None = None,
    secs: list[Section] | None = None,
) -> list[Defect]:
    exclude = exclude or set()
    secs = secs if secs is not None else sections(doc)
    same_table = _table_of(doc)
    binding = _binding_blocks(doc, spec)

    by_family: dict[str, list[Measurand]] = {}
    for m in spec.measurands:
        if m.kind == "date" or m.m_id in exclude:
            continue
        by_family.setdefault(_family(m), []).append(m)

    defects: list[Defect] = []
    n = 0
    for family, ms in by_family.items():
        # Union-find over "these two describe the same quantity".
        parent = list(range(len(ms)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                a, b = ms[i], ms[j]
                if a.block_id == b.block_id:
                    continue
                # Rows of one table are a list of different things by
                # construction — seven posts with seven experience minima are
                # not seven answers to one question.
                ta, tb = same_table.get(a.block_id), same_table.get(b.block_id)
                if ta is not None and ta == tb:
                    continue
                shared = set(a.subject) & set(b.subject)
                if len(shared) < MIN_SUBJECT_OVERLAP:
                    continue
                if not any(df.get(s, 99) <= RARE_DF for s in shared):
                    continue
                # Two shared words can be scaffolding ("contract", "part").
                # Three is a description; two plus a word this document barely
                # uses is an identity.
                if len(shared) < 3 and not any(df.get(s, 99) <= IDENTIFYING_DF
                                               for s in shared):
                    continue
                # And the shared word has to be one of the words touching each
                # number. Two amounts twelve tokens apart from the same
                # "contract" are not the same amount.
                if not (shared & set(a.subject[:NEAR]) & set(b.subject[:NEAR])):
                    continue
                parent[find(i)] = find(j)

        groups: dict[int, list[Measurand]] = {}
        for i, m in enumerate(ms):
            groups.setdefault(find(i), []).append(m)

        for members in groups.values():
            values = {round(m.value, 4) for m in members}
            if len(members) < 2 or len(values) < 2:
                continue
            if not any(m.block_id in binding for m in members):
                continue
            # A weighting stated as 70/30 in the data sheet and again as 70/30
            # in the scoring clause is the document agreeing with itself. It
            # only disagrees if some block involved is missing one of the
            # values the others carry.
            in_block: dict[str, set] = {}
            for m in ms:
                in_block.setdefault(m.block_id, set()).add(round(m.value, 4))
            if all(values <= in_block[m.block_id] for m in members):
                continue
            n += 1
            shared = set(members[0].subject)
            for m in members[1:]:
                shared &= set(m.subject)
            subject = ", ".join(sorted(shared)) or family

            ranks = {
                m.m_id: _precedence_at(secs, doc.block(m.block_id).order)
                for m in members
            }
            lines = [
                f"{_fmt(m)} — {m.section} ({m.block_id})"
                + (f" [{ranks[m.m_id][1]}]" if ranks[m.m_id][1] else "")
                for m in members
            ]
            top = max(r for r, _ in ranks.values())
            resolution = ""
            severity = "high"
            if top > min(r for r, _ in ranks.values()):
                winners = sorted({_fmt(m) for m in members
                                  if ranks[m.m_id][0] == top})
                names = sorted({ranks[m.m_id][1] for m in members
                                if ranks[m.m_id][0] == top and ranks[m.m_id][1]})
                if len(winners) == 1:
                    resolution = winners[0]
                    severity = "medium"
                    lines.append(
                        f"{' and '.join(names)} prevail over the general "
                        f"instrument, so {winners[0]} governs — but the "
                        "disagreement is worth a clarification either way."
                    )
            defects.append(
                Defect(
                    defect_id=f"X{n:03d}",
                    check="value_conflict",
                    severity=severity,
                    title=f"{len(values)} different values for one quantity ({subject})",
                    detail=(
                        "These clauses state different numbers for what the "
                        "surrounding words describe as the same quantity. "
                        + (
                            "The document's own order of precedence settles "
                            "which one governs."
                            if resolution
                            else "One of them governs; the document does not "
                                 "say which."
                        )
                        + "\n  "
                        + "\n  ".join(lines)
                    ),
                    spans=[s for m in members for s in m.spans],
                    quotes=[m.raw for m in members],
                    sections=sorted({m.section for m in members}),
                    resolution=resolution,
                )
            )
    return defects


def check_date_arithmetic(doc: Document, spec: SpecResult) -> list[Defect]:
    """Where the document does arithmetic on itself, do the arithmetic.

    A solicitation that says a date is "28 days beyond the bid validity period"
    has stated a constraint the rest of it must satisfy. Solving it does not
    merely detect the disagreement — it says which of the competing values the
    client's own dates are consistent with, which is the difference between a
    query to the client and a decision the bid team can take today.
    """
    dates = [m for m in spec.measurands if m.kind == "date"]
    durations = [m for m in spec.measurands if m.kind == "duration"]
    if not dates or not durations:
        return []

    # A solicitation states several deadlines. The one the periods run from is
    # the submission deadline, so prefer the date that says so and fall back to
    # any deadline only if none does.
    def _base_score(m: Measurand) -> int:
        sub = set(m.subject)
        if {"submission", "deadlin"} <= sub:
            return 2
        return 1 if "deadlin" in sub else 0

    base = max(dates, key=_base_score)
    if _base_score(base) == 0:
        return []

    defects: list[Defect] = []
    n = 0
    for b in doc.blocks:
        for sm in SELF_ARITHMETIC.finditer(b.text):
            offset = int(sm.group(1)) * _UNIT_DAYS[sm.group(2).lower()]
            referent = set(content_stems(sm.group(4)))
            anchor = next((d for d in dates if d.block_id == b.block_id), None)
            if anchor is None:
                continue
            # The referent is named in words — "the bid validity period" — and a
            # candidate has to be about that, not merely share one word with it.
            # One shared stem pulls in every "period" in the document.
            candidates = [
                m for m in durations
                if len(referent & set(m.subject[:NEAR])) >= 2
                and m.block_id != b.block_id
            ]
            if len(candidates) < 2:
                continue
            consistent = [
                m for m in candidates
                if abs((base.value + m.value + offset) - anchor.value) < 0.5
            ]
            if len(consistent) != 1:
                continue
            good = consistent[0]
            bad = [m for m in candidates if m is not good]
            n += 1
            defects.append(
                Defect(
                    defect_id=f"A{n:03d}",
                    check="date_arithmetic",
                    severity="high",
                    title=(
                        f"The document's own dates resolve the conflict to "
                        f"{_fmt(good)}"
                    ),
                    detail=(
                        f"{_fmt(base)} ({base.section}) plus {_fmt(good)} plus the "
                        f"stated {sm.group(1)} {sm.group(2)} gives "
                        f"{_fmt(anchor)}, which is the date written in "
                        f"{anchor.section}. The competing value"
                        + ("s " if len(bad) > 1 else " ")
                        + ", ".join(f"{_fmt(m)} ({m.section})" for m in bad)
                        + " cannot be reconciled with it. Treat the arithmetic as "
                        "governing and put the discrepancy to the client in "
                        "writing before relying on it."
                    ),
                    spans=base.spans + good.spans + anchor.spans
                    + [SpanRef(doc.doc_id, b.block_id, sm.start(), sm.end())],
                    quotes=[base.raw, good.raw, anchor.raw, sm.group(0)],
                    sections=sorted({base.section, good.section, anchor.section}),
                    resolution=_fmt(good),
                )
            )
    return defects


# ---------------------------------------------------------------------------
# 3 & 4 — tables
# ---------------------------------------------------------------------------


def _cells(text: str) -> list[str]:
    return [c.strip() for c in text.split(" | ")]


def _as_number(cell: str) -> float | None:
    c = cell.replace(",", "").replace("%", "").strip()
    return float(c) if re.fullmatch(r"\d+(?:\.\d+)?", c) else None


def check_table_sums(doc: Document, secs: list[Section]) -> list[Defect]:
    defects: list[Defect] = []
    n = 0
    for group in tables(doc):
        header = _cells(group[0].text)
        rows = group[1:]
        if not rows:
            continue
        sec = section_at(secs, group[0].order)
        label = sec.label() if sec else "(front matter)"
        for col, name in enumerate(header):
            if not PERCENT_HEADER.search(name):
                continue
            values: list[tuple[str, float]] = []
            has_total = False
            for r in rows:
                cs = _cells(r.text)
                if len(cs) != len(header):
                    continue
                if any(TOTAL_ROW.search(c) for c in cs):
                    has_total = True
                    continue
                v = _as_number(cs[col]) if col < len(cs) else None
                if v is not None:
                    values.append((r.block_id, v))
            if len(values) < 3:
                continue
            total = sum(v for _, v in values)
            if abs(total - 100.0) < 0.01:
                continue
            n += 1
            defects.append(
                Defect(
                    defect_id=f"T{n:03d}",
                    check="table_sum",
                    severity="high",
                    title=(
                        f"'{name}' column totals {total:g}, not 100"
                        + ("" if has_total else " — and the table carries no total row")
                    ),
                    detail=(
                        f"{len(values)} rows under '{name}' in {label} sum to "
                        f"{total:g}: "
                        + " + ".join(f"{v:g}" for _, v in values)
                        + f". The remaining {100 - total:g} is unaccounted for. "
                        "Where the column is a payment schedule, that is the share "
                        "of the contract price the document does not say how to earn."
                    ),
                    spans=[
                        SpanRef(doc.doc_id, bid, 0, len(doc.block(bid).text))
                        for bid, _ in values
                    ],
                    quotes=[doc.block(bid).text for bid, _ in values],
                    sections=[label],
                )
            )
    return defects


def check_foreign_triggers(doc: Document, secs: list[Section]) -> list[Defect]:
    """Payment milestones nobody on our side can cause.

    A milestone conditioned on an act of the client that our own performance
    does not produce is not a schedule, it is an option the client holds. It is
    priced work with no date, and it belongs in the bid decision.
    """
    defects: list[Defect] = []
    n = 0
    for group in tables(doc):
        header = _cells(group[0].text)
        if not any(PAYMENT_TABLE.search(h) for h in header):
            continue
        sec = section_at(secs, group[0].order)
        label = sec.label() if sec else "(front matter)"
        for r in group[1:]:
            cs = _cells(r.text)
            body = " ".join(cs)
            if TOTAL_ROW.search(body):
                continue
            m = CLIENT_AGENT.search(body)
            if not m:
                continue
            if DELIVERABLE_CODE.search(body) or OUR_ACT.search(body):
                continue
            n += 1
            defects.append(
                Defect(
                    defect_id=f"F{n:03d}",
                    check="foreign_trigger",
                    severity="medium",
                    title="Payment milestone triggered by an act of the client alone",
                    detail=(
                        f"In {label}, this milestone falls due on \"{m.group(0)}\" "
                        "and names no deliverable of ours. Nothing we do brings it "
                        "about, so the payment has no date we can plan against and "
                        "no remedy if it never arrives. Price it, or ask for a "
                        "long-stop date."
                    ),
                    spans=[SpanRef(doc.doc_id, r.block_id, 0, len(r.text))],
                    quotes=[r.text],
                    sections=[label],
                )
            )
    return defects


# ---------------------------------------------------------------------------
# 5, 6, 7 — requirements that are not where a bidder would look for them
# ---------------------------------------------------------------------------


def _declined(check: str, defect_id: str, reason: str) -> Defect:
    """A check that could not run, reported as such rather than left silent.

    Same contract as `R000` for cross-references: "this was not checked" is
    information a reader needs, and saying nothing is indistinguishable from
    saying everything was fine. Carries no quotes and no spans, and callers must
    never count it as a finding.
    """
    return Defect(
        defect_id=defect_id,
        check=check,
        severity="low",
        title=f"{check} did not run",
        detail=reason + " This is not a finding that the document is sound.",
        spans=[],
        quotes=[],
        sections=[],
    )


def _wrapper_blocks(doc: Document, secs: list[Section]) -> set[str]:
    """Block ids that sit inside the document's own title block.

    A notice headed "REQUEST FOR EXPRESSIONS OF INTEREST (CONSULTING SERVICES —
    FIRMS SELECTION)" is one unnumbered heading over the entire text, and
    `spec.py` classifies it `scope` because the word "services" appears in the
    title. That heading is the document, not a place inside it: a rule cannot be
    filed away from the submission requirements when the only heading above it is
    the name of the procurement. The tell is structural — unnumbered, and
    running to the end of the document.
    """
    if not secs:
        return set()
    last = max(secs, key=lambda s: s.end_order)
    if last.number:
        return set()
    return {b.block_id for b in doc.blocks
            if last.start_order <= b.order < last.end_order}


def check_disqualifying_rules(
    doc: Document, spec: SpecResult, secs: list[Section]
) -> list[Defect]:
    """Rules that end a bid, filed where a bidder does not go looking for rules.

    Grouped by block rather than by sentence: a paragraph that voids a proposal
    twice is one place to fix, and quoting only its first sentence would hide
    the half that matters.

    Placement is only judgeable where the document labels its own sections. On a
    document that declares none — a procurement notice, a cover letter — every
    rule sits in unlabelled text, so "filed away from the submission
    requirements" is vacuously true of all of them and means nothing. In that
    case the check declines instead of converting a missing structure into a
    high-severity finding on every paragraph.
    """
    wrapper = _wrapper_blocks(doc, secs)
    placed = [c for c in spec.commitments
              if c.disqualifying and not SCORING.search(c.text)
              and c.block_id not in wrapper]
    labelled = [c for c in placed if c.section_kind not in UNPLACED_KINDS]
    if placed and not labelled:
        return [_declined(
            "disqualifying_rule_off_anchor", "Q001",
            f"{len(placed)} bid-ending rule(s) were found, but none of them sits "
            "in a section this document labels, so there is no declared place to "
            "judge their placement against.",
        )]

    by_block: dict[str, list] = {}
    for c in labelled:
        if c.section_kind in ANCHOR_KINDS:
            continue
        by_block.setdefault(c.block_id, []).append(c)

    defects: list[Defect] = []
    for n, (block_id, group) in enumerate(by_block.items(), start=1):
        first = group[0]
        defects.append(
            Defect(
                defect_id=f"D{n:03d}",
                check="disqualifying_rule_off_anchor",
                severity="high",
                title=f"Bid-ending rule stated in {first.section}, not with the "
                      "submission requirements",
                detail=(
                    f"{len(group)} sentence(s) here make a proposal non-responsive, "
                    f"but they are filed under a '{first.section_kind}' section. A "
                    "bidder reading the mandatory submission requirements and the "
                    "instructions to bidders would not meet them. Add each to the "
                    "compliance matrix by hand — they will not be there otherwise."
                ),
                spans=[s for c in group for s in c.spans],
                quotes=[c.text for c in group],
                sections=[first.section],
            )
        )
    return defects


def _roles(doc: Document, secs: list[Section]) -> dict[str, str]:
    """Named posts, harvested from the document's own personnel section.

    The list comes from the solicitation being read, not from anything shipped
    with the tool, so it works on a document that invents its own job titles.
    """
    out: dict[str, str] = {}
    for group in tables(doc):
        sec = section_at(secs, group[0].order)
        if sec is None or sec.kind != "personnel":
            continue
        for r in group[1:]:
            cs = _cells(r.text)
            if len(cs) < 2:
                continue
            for alt in re.split(r"\s*/\s*|\s+or\s+", re.sub(r"\([^)]*\)", "", cs[0])):
                name = alt.strip(" .,-").lower()
                if 2 <= len(name.split()) <= 6 and len(name) > 6:
                    out[name] = r.text
    return out


def check_personnel_requirements(
    doc: Document, spec: SpecResult, secs: list[Section]
) -> list[Defect]:
    roles = _roles(doc, secs)
    if not roles:
        return []
    defects: list[Defect] = []
    n = 0
    for c in spec.commitments:
        if c.section_kind == "personnel":
            continue
        if c.modality.value not in ("obligation", "prohibition", "condition"):
            continue
        low = c.text.lower()
        hit = next((r for r in roles if r in low), None)
        if hit is None:
            continue
        role_stems = set(content_stems(hit))
        row_stems = set(content_stems(roles[hit]))
        distinguishing = [s for s in c.lemmas if s not in role_stems]
        if not distinguishing:
            continue
        if any(s in row_stems for s in distinguishing):
            continue
        n += 1
        defects.append(
            Defect(
                defect_id=f"P{n:03d}",
                check="personnel_requirement_off_anchor",
                severity="medium",
                title=f"Qualification imposed on the {hit.title()}, stated outside "
                      "the key personnel section",
                detail=(
                    f"This binds the {hit.title()}, but it sits in {c.section} and "
                    "nothing in that post's row of the key personnel table repeats "
                    "it. A team assembled from the personnel table alone does not "
                    "satisfy it, and the omission is not visible at evaluation until "
                    "it costs points or responsiveness."
                ),
                spans=list(c.spans),
                quotes=[c.text, roles[hit]],
                sections=[c.section],
            )
        )
    return defects


def check_single_point_obligations(
    doc: Document, spec: SpecResult, df: dict[str, int], secs: list[Section] | None = None
) -> list[Defect]:
    """Requirements the document states once, away from its requirements.

    The test is deliberately structural: a binding duty on us, carrying
    something we must hold or produce, filed outside every section a bidder
    reads for duties, and built on words that appear in no other block. A
    proposal team working from the submission requirements will not find it.

    The redundancy premise only holds where the document labels its sections.
    This check exists to catch a requirement mentioned once when it should be
    repeated; a procurement notice is *supposed* to state each requirement once,
    and on an unlabelled document the tool cannot tell the two apart. It
    therefore declines on those documents rather than reporting every obligation
    in them as a single-point obligation.
    """
    wrapper = _wrapper_blocks(doc, secs or [])
    candidates = [
        c for c in spec.commitments
        if c.modality.value in ("obligation", "prohibition")
        and c.actor.value in ("contractor", "subcontractor", "personnel")
        and OBJECT_NOUNS.search(c.text)
        and c.block_id not in wrapper
    ]
    if candidates and not any(c.section_kind not in UNPLACED_KINDS for c in candidates):
        return [_declined(
            "single_point_obligation", "Q002",
            f"{len(candidates)} binding obligation(s) on us were found, but the "
            "document labels none of its sections, so it is not possible to tell "
            "a requirement stated once by mistake from one stated once by design.",
        )]

    defects: list[Defect] = []
    n = 0
    for c in candidates:
        if c.section_kind in UNPLACED_KINDS:
            continue
        if c.section_kind in ANCHOR_KINDS:
            continue
        obj = OBJECT_NOUNS.search(c.text)
        if not obj:
            continue
        # The thing to be held or produced must itself be near-unique. Every
        # long solicitation contains obligations with some unique word in them;
        # only a handful name a certificate the document mentions once.
        if df.get(stem(obj.group(0)), 99) > OBJECT_DF:
            continue
        unique = [s for s in c.lemmas if df.get(s, 0) <= 1]
        if not unique:
            continue
        n += 1
        defects.append(
            Defect(
                defect_id=f"S{n:03d}",
                check="single_point_obligation",
                severity="high",
                title=f"Binding requirement stated once, in {c.section}",
                detail=(
                    "This is a duty on us carrying something we must hold or "
                    f"produce. It appears in a '{c.section_kind}' section and "
                    "nowhere else in the document — "
                    + ", ".join(f"'{s}'" for s in unique[:4])
                    + " occur in no other block. It is absent from the mandatory "
                    "submission requirements, so a compliance matrix built from "
                    "those requirements will not carry it."
                ),
                spans=list(c.spans),
                quotes=[c.text],
                sections=[c.section],
            )
        )
    return defects


# ---------------------------------------------------------------------------
# 8 — pointers that go nowhere
# ---------------------------------------------------------------------------


def check_dangling_references(doc: Document, secs: list[Section]) -> list[Defect]:
    """Pointers to provisions that are not in the bundle.

    Two different failures wear the same clothes. A solicitation that cites its
    own Section 4.2 when there is no 4.2 has a numbering error. One that cites
    Article 6.8 when it carries no Article 6 at all is almost always pointing
    into an instrument incorporated by reference and not supplied — which is a
    finding about the bundle, not about the numbering, and is worth exactly one
    line rather than twenty-five.

    Both readings need a numbering index to stand on. When ingest recovers too
    little numbering — scanned PDFs routinely do — the check reports that it did
    not run rather than reporting every reference in the document as broken.
    """
    from .segment import segment

    known: set[str] = {s.number.lower() for s in secs if s.number}
    for c in segment(doc):
        if c.number:
            known.add(c.number.lower())
    for s in list(known):
        if re.match(r"(?:annex|appendix|schedule)\s", s):
            known.add(s.split()[-1])

    tops = {k.split(".")[0] for k in known}
    if len(known) < MIN_NUMBERING:
        return [
            Defect(
                defect_id="R000",
                check="dangling_reference",
                severity="low",
                title="Cross-reference check did not run",
                detail=(
                    f"Only {len(known)} numbered provisions were recovered from "
                    "this document, which is too few to tell a broken reference "
                    "from an unrecovered one. The check is reported as not run. "
                    "It is not a finding that the references are sound."
                ),
                spans=[],
                quotes=[],
                sections=[],
            )
        ]

    internal: list[tuple[str, str, SpanRef]] = []
    external: list[tuple[str, str, SpanRef]] = []
    seen: set[str] = set()
    for b in doc.blocks:
        for m in REF.finditer(b.text):
            kind, num = m.group(1).lower(), m.group(2)
            target = num.lower()
            if target in known or any(k.startswith(target + ".") for k in known):
                continue
            if kind.startswith(("annex", "appendix", "schedule")) and target in known:
                continue
            key = f"{kind}:{target}"
            if key in seen:
                continue
            seen.add(key)
            span = SpanRef(doc.doc_id, b.block_id, m.start(), m.end())
            top = target.split(".")[0]
            (internal if top in tops else external).append((m.group(0), b.block_id, span))

    defects: list[Defect] = []
    for n, (text, block_id, span) in enumerate(internal, start=1):
        sec = section_at(secs, doc.block(block_id).order)
        defects.append(
            Defect(
                defect_id=f"R{n:03d}",
                check="dangling_reference",
                severity="medium",
                title=f"Reference to {text}, which this document does not contain",
                detail=(
                    "The provision it points at is missing under that number, "
                    "though its parent section is present — so this reads as a "
                    "numbering error rather than a pointer into another "
                    "instrument. Put it to the client before the clarification "
                    "deadline."
                ),
                spans=[span],
                quotes=[text],
                sections=[sec.label() if sec else "(front matter)"],
            )
        )
    if external:
        defects.append(
            Defect(
                defect_id="R900",
                check="external_reference",
                severity="medium",
                title=(
                    f"{len(external)} reference(s) point outside this document"
                ),
                detail=(
                    "These cite provisions whose whole numbering series is absent "
                    "from the bundle, which means the instrument they belong to "
                    "was incorporated by reference and not supplied. Their content "
                    "is unknown to this review and has not been assumed: "
                    + ", ".join(sorted({t for t, _, _ in external})[:20])
                    + ("..." if len(external) > 20 else "")
                ),
                spans=[sp for _, _, sp in external],
                quotes=sorted({t for t, _, _ in external}),
                sections=[],
            )
        )
    return defects


# ---------------------------------------------------------------------------


def ground_defects(doc: Document, defects: list[Defect]) -> tuple[list[Defect], int]:
    """Every quoted string must still be in the document at its offsets."""
    kept: list[Defect] = []
    dropped = 0
    for d in defects:
        ok = True
        for sp in d.spans:
            text = doc.text_of(sp)
            if text is None or not text.strip():
                ok = False
                break
        if ok:
            kept.append(d)
        else:
            dropped += 1
    return kept, dropped


def _arithmetic_offsets(doc: Document, spec: SpecResult) -> set[str]:
    """Durations that are an offset in the document's own arithmetic.

    "28 days beyond the bid validity period" is not a candidate value for the
    validity period; it is the sum's other term. Left in, it joins the conflict
    it exists to resolve.
    """
    out: set[str] = set()
    for b in doc.blocks:
        for m in SELF_ARITHMETIC.finditer(b.text):
            for meas in spec.measurands:
                if meas.block_id != b.block_id or meas.kind != "duration":
                    continue
                sp = meas.spans[0]
                if sp.start >= m.start() and sp.end <= m.end():
                    out.add(meas.m_id)
    return out


def lint(doc: Document, spec: SpecResult) -> SpecResult:
    """Run every check and attach the defects to the compiled spec."""
    secs = sections(doc)
    df = _df(doc)

    found: list[Defect] = []
    found += check_value_conflicts(
        doc, spec, df, _arithmetic_offsets(doc, spec), secs
    )
    found += check_date_arithmetic(doc, spec)
    found += check_table_sums(doc, secs)
    found += check_foreign_triggers(doc, secs)
    found += check_disqualifying_rules(doc, spec, secs)
    found += check_personnel_requirements(doc, spec, secs)
    found += check_single_point_obligations(doc, spec, df, secs)
    found += check_dangling_references(doc, secs)

    kept, dropped = ground_defects(doc, found)
    spec.defects = kept
    if dropped:
        spec.notes.append(
            f"{dropped} defect(s) dropped at grounding: quoted text did not "
            "resolve at the cited offsets."
        )
    counts: dict[str, int] = {}
    for d in kept:
        counts[d.check] = counts.get(d.check, 0) + 1
    spec.notes.append(
        "checks raised: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        if counts
        else "checks raised: none"
    )
    return spec
