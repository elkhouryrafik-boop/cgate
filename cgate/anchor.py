"""
cgate.anchor — map a provision's quote onto a range in the *Word-side* text.

Why this exists
---------------
A provision's quote was extracted from the PDF by `ingest.py`. When Word opens
that same PDF it *converts* it: reflowed lines, rejoined hyphenation,
substituted ligatures, altered whitespace. A search for the PDF-extracted
string against Word's text therefore fails for conversion reasons before it
ever fails for repetition reasons (the same phrase recurring in General
Conditions).

This module is the **plan**: pure Python over two strings — no Word, no
document open, nothing that cannot run under pytest. It answers, for one
provision, "what should the add-in search Word for, and which hit is ours?"
The add-in (phase 2) only executes that plan and verifies the text it finds
matches what the plan expected; it never re-decides anything here.

What "Word-side text" means for this module
---------------------------------------------
The add-in sends the document body's own text (`Body.text` in Office.js,
paragraphs terminated by CR/LF). This module treats any run of `\\r`/`\\n` in
that text as a paragraph break — never crossed by a single-point anchor
fragment (§4.1.2) and the signal that splits a quote into a two-point,
head/tail anchor (§4.1.3).

Normalisation, and why it does not use NFKC
--------------------------------------------
`ingest.normalise()` folds ligatures and collapses whitespace before spans are
ever computed, and that is right for *comparing* the quote to the body text.
But every fragment this module emits must be an exact, literal substring of
the *original* body text, because `Range.search` searches Word's real
characters, not a normalised copy. So normalisation here is deliberately
narrow and always reversible to an exact source offset: runs of whitespace
collapse to one space, a small set of typographically-equivalent punctuation
marks are straightened, and everything is compared case-insensitively (Word's
own default search behaviour). No ligature expansion, no NFKC — both can
change the number of characters, and this module never needs to explain a
character it did not can point back to.

Anchor plan outcomes
---------------------
`exact`      the whole quote, modulo whitespace/typography, is present verbatim.
`fragment`   only part of the quote could be aligned reliably; the fragment(s)
             below are a true excerpt of that part, not an invention.
`ambiguous`  the quote is present in substance but no contiguous run of it is
             long enough to serve as a reliable search string.
`failed`     no usable overlap between the quote and the body text at all.

`ambiguous` and `failed` provisions carry no fragment and are never anchored;
the add-in lists them under *Unanchored* rather than dropping them (§4.1.5,
§9.5 — the same "absent is not clean" rule as everywhere else in this system).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Range.search's hard limit (§8). Kept as a constant here, not re-derived by
# the add-in, so the one number that must never be exceeded lives in one place.
MAX_FRAGMENT_CHARS = 255

# A fragment shorter than this is not a reliable anchor: common contract words
# repeat constantly, and a short "unique" hit today is not one tomorrow if the
# document is amended. Reported as `ambiguous` rather than risking a wrong
# clause getting framed.
MIN_FRAGMENT_CHARS = 20

# Below this fraction of the quote's characters aligning to the body text, the
# quote is treated as not present rather than forced into a fragment that
# would misrepresent it.
MIN_COVERAGE = 0.5

_PARA_BREAK = re.compile(r"[\r\n]+")
_WS = re.compile(r"\s")

# 1:1 character substitutions only — anything that could change the number of
# characters (ligatures, zero-width removal) is out of scope here; see the
# module docstring.
_STRAIGHTEN = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-",
}


# ---------------------------------------------------------------------------
# Normalisation with an exact offset map back to the source string
# ---------------------------------------------------------------------------


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Whitespace-collapsed, punctuation-straightened, case-folded copy of
    `text`, plus `out_map` where `out_map[i]` is the offset in `text` that
    produced `norm[i]`. Every normalised character maps to exactly one real
    source character, so a match in the normalised string can always be
    resolved back to real offsets."""
    out_chars: list[str] = []
    out_map: list[int] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _STRAIGHTEN:
            out_chars.append(_STRAIGHTEN[ch])
            out_map.append(i)
            i += 1
            continue
        if ch.isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            out_chars.append(" ")
            out_map.append(i)
            i = j
            continue
        out_chars.append(ch.lower())
        out_map.append(i)
        i += 1
    return "".join(out_chars), out_map


def _split_on_para_breaks(body_text: str, s: int, e: int) -> list[tuple[int, int]]:
    """[s, e) cut at every paragraph break it contains, breaks themselves
    dropped. A span that does not cross a break comes back as itself."""
    spans: list[tuple[int, int]] = []
    pos = s
    for m in _PARA_BREAK.finditer(body_text, s, e):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < e:
        spans.append((pos, e))
    return spans


def _build_fragment(body_text: str, s: int, e: int) -> str | None:
    """A literal, `Range.search`-safe substring of body_text[s:e]: capped at
    MAX_FRAGMENT_CHARS, cut at a word boundary rather than mid-word, and never
    carrying a `^` (Word's find escape, §4.1.2). None if nothing usable
    survives. The fragment stays an exact substring of the source — a `^`
    inside the span truncates the fragment before it, rather than being
    deleted out of the string, since deleting it would make the fragment stop
    matching the very text it was cut from."""
    caret = body_text.find("^", s, e)
    if caret != -1:
        e = caret
    if e - s > MAX_FRAGMENT_CHARS:
        window_end = s + MAX_FRAGMENT_CHARS
        cut = None
        for m in re.finditer(r"\s", body_text[s:window_end]):
            cut = m.start()
        e = s + cut if cut else window_end
    frag = body_text[s:e].strip()
    if len(frag) < MIN_FRAGMENT_CHARS:
        return None
    return frag


def _resolve_occurrence(body_text: str, frag_text: str, approx_start: int) -> "Fragment | None":
    """Which literal hit of `frag_text` in `body_text` is ours, and how many
    are there. Word's own search is what will actually run against these
    offsets, so occurrence counting is case-insensitive exact substring
    matching, exactly like `Range.search` with `matchCase: false`."""
    if not frag_text:
        return None
    lower_body = body_text.lower()
    lower_frag = frag_text.lower()
    starts: list[int] = []
    i = lower_body.find(lower_frag)
    while i != -1:
        starts.append(i)
        i = lower_body.find(lower_frag, i + 1)
    if not starts:
        return None
    best = min(range(len(starts)), key=lambda k: abs(starts[k] - approx_start))
    start = starts[best]
    return Fragment(
        text=frag_text,
        ordinal=best + 1,
        occurrences=len(starts),
        start=start,
        end=start + len(frag_text),
    )


# ---------------------------------------------------------------------------
# Public data
# ---------------------------------------------------------------------------


@dataclass
class Fragment:
    """One literal string to search Word for, and which hit is ours."""

    text: str
    ordinal: int        # 1-based: "take the Nth hit" (§4.1.2)
    occurrences: int     # how many hits `text` has in the body text
    start: int           # resolved character offset in the body text
    end: int

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "ordinal": self.ordinal,
            "occurrences": self.occurrences,
            "start": self.start,
            "end": self.end,
        }


@dataclass
class AnchorPlan:
    """The plan for one provision. `head`/`tail` are None for `ambiguous` and
    `failed`; `tail` is set only for a two-point (multi-paragraph) anchor."""

    req_id: str
    result: str          # exact | fragment | ambiguous | failed
    reason: str
    head: Fragment | None = None
    tail: Fragment | None = None
    multi_paragraph: bool = False

    @property
    def span(self) -> tuple[int, int] | None:
        """The range this anchor would occupy in the body text, or None if it
        has no usable range at all."""
        if self.head is None:
            return None
        if self.tail is not None:
            return (self.head.start, self.tail.end)
        return (self.head.start, self.head.end)

    def to_dict(self) -> dict:
        return {
            "req_id": self.req_id,
            "result": self.result,
            "reason": self.reason,
            "multi_paragraph": self.multi_paragraph,
            "head": self.head.to_dict() if self.head else None,
            "tail": self.tail.to_dict() if self.tail else None,
        }


@dataclass
class CoalescedAnchor:
    """One or more overlapping provisions sharing a single content control
    (§4.1.4 — Word content controls cannot partially overlap)."""

    anchor_id: str
    req_ids: list[str] = field(default_factory=list)
    band: str = "routine"
    start: int = 0
    end: int = 0

    def to_dict(self) -> dict:
        return {
            "anchor_id": self.anchor_id,
            "req_ids": self.req_ids,
            "band": self.band,
            "start": self.start,
            "end": self.end,
        }


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _finish(result: str, req_id: str, body_text: str,
            subspans: list[tuple[int, int]], reason: str) -> AnchorPlan:
    kept = sorted(sp for sp in subspans if sp[1] - sp[0] >= MIN_FRAGMENT_CHARS)
    if not kept:
        return AnchorPlan(
            req_id, "ambiguous",
            "the quote is present in substance but no contiguous run of it is "
            "long enough to build a reliable search fragment",
        )

    if len(kept) == 1:
        s, e = kept[0]
        frag = _build_fragment(body_text, s, e)
        if frag is None:
            return AnchorPlan(req_id, "ambiguous", "no reliable search fragment could be built")
        head = _resolve_occurrence(body_text, frag, s)
        if head is None:
            return AnchorPlan(
                req_id, "ambiguous",
                "the chosen fragment does not appear verbatim in the document text",
            )
        return AnchorPlan(req_id, result, reason, head=head)

    s0, e0 = kept[0]
    s1, e1 = kept[-1]
    head_txt = _build_fragment(body_text, s0, e0)
    tail_txt = _build_fragment(body_text, s1, e1)
    if head_txt is None or tail_txt is None:
        return AnchorPlan(
            req_id, "ambiguous",
            "the quote spans a paragraph break and one side could not be turned "
            "into a reliable search fragment",
        )
    head = _resolve_occurrence(body_text, head_txt, s0)
    tail = _resolve_occurrence(body_text, tail_txt, s1)
    if head is None or tail is None:
        return AnchorPlan(
            req_id, "ambiguous",
            "the two-point fragment does not appear verbatim in the document text",
        )
    return AnchorPlan(
        req_id, result,
        reason + " — the quote spans a paragraph break; anchored at its first and last passage",
        head=head, tail=tail, multi_paragraph=True,
    )


def plan_one(req_id: str, quote: str, body_text: str) -> AnchorPlan:
    """The anchor plan for one provision. See the module docstring for the
    four outcomes."""
    quote = (quote or "").strip()
    if not quote:
        return AnchorPlan(req_id, "failed", "no quote text was extracted for this provision")
    if not (body_text or "").strip():
        return AnchorPlan(req_id, "failed", "the document's body text was empty")

    norm_body, body_map = _normalize_with_map(body_text)
    norm_quote, _ = _normalize_with_map(quote)
    if not norm_quote:
        return AnchorPlan(req_id, "failed", "the quote was empty after normalisation")

    idx = norm_body.find(norm_quote)
    if idx != -1:
        orig_start = body_map[idx]
        orig_end = body_map[idx + len(norm_quote) - 1] + 1
        subspans = _split_on_para_breaks(body_text, orig_start, orig_end)
        return _finish(
            "exact", req_id, body_text, subspans,
            "the quote was found intact, modulo whitespace and typography",
        )

    import difflib

    sm = difflib.SequenceMatcher(None, norm_body, norm_quote, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
    if not blocks:
        return AnchorPlan(
            req_id, "failed",
            "no overlap found between the extracted quote and the document's text",
        )

    coverage = sum(b.size for b in blocks) / len(norm_quote)
    if coverage < MIN_COVERAGE:
        return AnchorPlan(
            req_id, "failed",
            f"alignment covered only {coverage:.0%} of the quote — too little to anchor",
        )

    subspans = []
    for b in blocks:
        s = body_map[b.a]
        e = body_map[b.a + b.size - 1] + 1
        subspans.extend(_split_on_para_breaks(body_text, s, e))

    return _finish(
        "fragment", req_id, body_text, subspans,
        f"the quote drifted from the document's text (PDF-to-Word conversion); "
        f"aligned at {coverage:.0%} coverage",
    )


def plan_anchors(provisions: list[dict], body_text: str) -> list[AnchorPlan]:
    """`provisions`: [{"req_id": ..., "quote": ...}, ...] — as read from
    `reading.read()`'s output. One plan per provision, same order."""
    return [plan_one(p["req_id"], p.get("quote", ""), body_text) for p in provisions]


def coalesce(plans: list[AnchorPlan], bands: dict[str, str]) -> list[CoalescedAnchor]:
    """Merge overlapping anchor ranges into one, wearing the worst member's
    band (§4.1.4). `bands`: req_id -> exposure band (see `reading.EXPOSURE_ORDER`
    for the worst-first ordering used to pick which member's band wins)."""
    from .reading import EXPOSURE_ORDER

    resolved = []
    for p in plans:
        if p.result not in ("exact", "fragment"):
            continue
        span = p.span
        if span is None:
            continue
        band = bands.get(p.req_id, EXPOSURE_ORDER[-1])
        resolved.append((p.req_id, span[0], span[1], band))
    resolved.sort(key=lambda r: r[1])

    groups: list[dict] = []
    for req_id, start, end, band in resolved:
        if groups and start < groups[-1]["end"]:
            g = groups[-1]
            g["req_ids"].append(req_id)
            g["start"] = min(g["start"], start)
            g["end"] = max(g["end"], end)
            if EXPOSURE_ORDER.index(band) < EXPOSURE_ORDER.index(g["band"]):
                g["band"] = band
        else:
            groups.append({"req_ids": [req_id], "start": start, "end": end, "band": band})

    return [
        CoalescedAnchor(anchor_id=f"a{i}", req_ids=g["req_ids"], band=g["band"],
                         start=g["start"], end=g["end"])
        for i, g in enumerate(groups, 1)
    ]


def plan(provisions: list[dict], body_text: str) -> dict:
    """The full anchor plan for a document: per-provision results plus the
    coalesced content-control ranges. This is what `POST /api/anchor-plan`
    returns and what `cgate bench` measures."""
    plans = plan_anchors(provisions, body_text)
    bands = {p["req_id"]: p.get("band", "routine") for p in provisions}
    anchors = coalesce(plans, bands)

    counts: dict[str, int] = {}
    for p in plans:
        counts[p.result] = counts.get(p.result, 0) + 1
    total = len(plans)
    anchored = counts.get("exact", 0) + counts.get("fragment", 0)

    return {
        "results": {p.req_id: p.to_dict() for p in plans},
        "anchors": [a.to_dict() for a in anchors],
        "stats": {
            "total": total,
            "anchored": anchored,
            "anchor_rate": (anchored / total) if total else 0.0,
            "exact": counts.get("exact", 0),
            "fragment": counts.get("fragment", 0),
            "ambiguous": counts.get("ambiguous", 0),
            "failed": counts.get("failed", 0),
        },
    }
