"""
cgate.feedback — how the tool gets better with use, without getting less predictable.

The constraint that shapes this module
--------------------------------------
The design commits the firm to reproducing any review years after
it was signed. A tool that quietly rewrites its own rules between runs cannot
honour that: the same solicitation would produce two different reviews and the
firm could not say which one the Compliance Officer actually signed. Continuous
silent self-tuning is therefore not available here, and its absence is a design
decision rather than a missing feature.

What is available is a supervised loop with a version stamp:

    1. LEDGER      Every CO decision on a finding is appended to a JSONL ledger:
                   confirmed, rejected (with a reason), or missed (a provision
                   the rules did not find, quoted by the reviewer).

    2. CALIBRATION Precision per category is computed from the ledger and printed
                   in every subsequent report. This is pure measurement — it
                   changes no behaviour, needs no approval, and is the fastest
                   real improvement available, because a reviewer who knows R9
                   runs at 60% and R1 at 98% reads the two differently.

    3. PROPOSALS   Recurring misses become candidate anchors; recurring rejections
                   of one surface form become candidate demotions or suppressions.
                   Proposals are generated automatically and applied by nobody.

    4. PROMOTION   A human approves a proposal. That writes `overrides.yaml` and
                   bumps `lexicon_version`. Every later review records the version
                   it ran under, so any run is reproducible by checking out that
                   version of the overrides.

Nothing here reads a model confidence score, at any stage. The literature is
consistent that models cannot assess their own errors, so the training signal is exclusively a named human's decision
about a verbatim quote.

Why this actually improves the tool
-----------------------------------
The rules layer fails in two ways that feedback fixes directly. It misses
provisions phrased in a way the anchors do not cover — a real-world tender can say "corrupt, fraudulent, coercive, collusive or obstructive conduct",
which no R5 anchor matched, and that is a real miss recoverable from one reviewer
correction. And it over-fires on generic surface forms, which a reviewer's
rejections identify precisely because `Requirement.matched` records which form
fired. Both are learnable from a handful of decisions per document. Neither
needs a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import lexicon as lx

LEDGER_NAME = "feedback.jsonl"
OVERRIDES_NAME = "overrides.yaml"

# A surface form must be rejected this many times, and be this dominant, before a
# demotion is proposed. Set high enough that one reviewer having a bad afternoon
# does not reshape the lexicon.
MIN_REJECTIONS = 3
MIN_REJECTION_RATE = 0.6


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    """One reviewer decision about one finding, or one reported miss.

    `kind='classify'` is the third decision (PLAN §6.6): a reviewer reading an
    UNCLASSIFIED provision names its category, which is the reading the
    machine could not produce on its own. It is recorded exactly like any
    other decision — a named human, a verbatim quote, nothing auto-applied —
    and is available to `proposals()` on the same footing as a confirm.
    """

    kind: str                     # confirm | reject | miss | classify
    category: str
    ts: str = ""
    reviewer: str = ""
    doc_id: str = ""
    req_id: str = ""
    matched: list[str] = field(default_factory=list)
    quote: str = ""
    reason: str = ""
    lexicon_version: str = ""
    # 'pane' (a button press) or 'comment-reply' (§6.5 — the reviewer answered
    # a CGATE comment in the margin, in their own words). Never anything the
    # tool decided on its own; both sources are a named human's action.
    source: str = "pane"

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["ts"] = self.ts or time.strftime("%Y-%m-%dT%H:%M:%S")
        # One line, and it is what makes delta review (PLAN §11 "Horizon") a
        # diff instead of a migration: every decision can be checked against
        # the exact provision text it was made about, even after the
        # extractor or the document itself has since changed.
        d["quote_hash"] = hashlib.sha256(self.quote.strip().encode("utf-8")).hexdigest() if self.quote else ""
        return d


# ---------------------------------------------------------------------------
# Reply grammar (PLAN §6.5) — the margin as a decision surface
# ---------------------------------------------------------------------------
#
# A Word comment reply is free text; parsing it lives here, not in the add-in,
# for the same reason the anchor plan lives in cgate.anchor: this is the hard
# logic, and it has to be testable without Word. The add-in only forwards a
# reply's raw text and the comment it replied to; this module decides what
# the reviewer meant, or that it could not tell — the add-in never guesses.

_REPLY_CONFIRM = re.compile(r"^\s*confirm\s*$", re.IGNORECASE)
_REPLY_REJECT = re.compile(r"^\s*reject\s*:\s*(.+)$", re.IGNORECASE)
_REPLY_CLASSIFY = re.compile(r"^\s*classify\s*:\s*(r\d{1,2})\s*$", re.IGNORECASE)

# taskpane.js's commentBody() always opens a CGATE comment with
# "CGATE <hash12> · <req_id> · <LABEL> · <where>"; the hash prefix is
# optional here so a comment inserted before §5.4's provenance stamp existed
# still parses.
_COMMENT_REQ = re.compile(r"^CGATE\s+(?:[0-9a-f]{6,}\s*·\s*)?(req\S+)\s*·", re.IGNORECASE)


def parse_reply(text: str) -> dict[str, str] | None:
    """The first line of a reply to a CGATE comment, read as a decision.
    `None` means it did not parse — the caller must surface that as an unread
    reply (§9.7), never drop it silently."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0]

    if _REPLY_CONFIRM.match(first_line):
        return {"kind": "confirm"}
    m = _REPLY_REJECT.match(first_line)
    if m:
        return {"kind": "reject", "reason": m.group(1).strip()}
    m = _REPLY_CLASSIFY.match(first_line)
    if m:
        return {"kind": "classify", "category": m.group(1).upper()}
    return None


def req_id_from_comment(content: str) -> str | None:
    """Which provision a CGATE comment concerns, read from its own first line."""
    first_line = (content or "").splitlines()[0] if content else ""
    m = _COMMENT_REQ.match(first_line)
    return m.group(1) if m else None


def ledger_path(store: str) -> str:
    return os.path.join(store, LEDGER_NAME)


def record(store: str, decision: Decision) -> None:
    """Append one decision. Append-only: the ledger is evidence, not a cache."""
    os.makedirs(store, exist_ok=True)
    with open(ledger_path(store), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(decision.to_dict(), ensure_ascii=False) + "\n")


def load_ledger(store: str) -> list[dict[str, Any]]:
    path = ledger_path(store)
    if not os.path.exists(path):
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue           # a corrupt line loses one decision, not the ledger
    return out


# ---------------------------------------------------------------------------
# Calibration — measurement only, no behaviour change
# ---------------------------------------------------------------------------


@dataclass
class Calibration:
    category: str
    confirmed: int = 0
    rejected: int = 0
    missed: int = 0

    @property
    def decided(self) -> int:
        return self.confirmed + self.rejected

    @property
    def precision(self) -> float | None:
        return self.confirmed / self.decided if self.decided else None

    @property
    def recall(self) -> float | None:
        """Recall against *known* misses only. It is an upper bound, and is
        labelled as one wherever it is printed — a miss nobody reported is a miss
        nobody can count."""
        denom = self.confirmed + self.missed
        return self.confirmed / denom if denom else None


def calibration(ledger: Iterable[dict[str, Any]]) -> dict[str, Calibration]:
    out: dict[str, Calibration] = {}
    for row in ledger:
        cat = row.get("category") or "?"
        c = out.setdefault(cat, Calibration(cat))
        kind = row.get("kind")
        if kind in ("confirm", "classify"):
            # A classification is a human affirming the category a provision
            # belongs to — the same kind of evidence a confirm is, just
            # arrived at because the extractor could not resolve modality or
            # actor on its own (PLAN §6.6).
            c.confirmed += 1
        elif kind == "reject":
            c.rejected += 1
        elif kind == "miss":
            c.missed += 1
    return out


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

_STOP = {
    "the", "and", "that", "this", "with", "from", "shall", "will", "must", "any",
    "for", "not", "its", "his", "her", "their", "such", "which", "have", "has",
    "been", "are", "was", "were", "under", "into", "upon", "them", "they", "all",
    "other", "including", "including,", "may", "contractor",
    "or", "but", "nor", "per", "via", "each", "who", "whom", "whose",
}


def _candidate_phrases(quote: str, width: int = 3) -> list[str]:
    """Distinctive n-grams from a reviewer-quoted miss, longest first.

    A proposed anchor is only ever a *candidate*. It is shown to a human next to
    the quote it came from, and it does not enter the lexicon until approved.
    """
    words = re.findall(r"[A-Za-z][A-Za-z\-']+", quote.lower())
    grams: list[str] = []
    for n in range(width, 1, -1):
        for i in range(len(words) - n + 1):
            g = words[i : i + n]
            if all(w in _STOP for w in g):
                continue
            if len(" ".join(g)) < 8:
                continue
            grams.append(" ".join(g))
    seen: set[str] = set()
    return [g for g in grams if not (g in seen or seen.add(g))][:8]


@dataclass
class Proposal:
    kind: str                  # add_anchor | demote_anchor | suppress_anchor
    category: str
    pattern: str
    evidence: str
    support: int
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def proposals(ledger: list[dict[str, Any]]) -> list[Proposal]:
    """Everything the ledger supports changing. Applied by `approve()`, never here."""
    out: list[Proposal] = []

    # --- misses become candidate anchors -----------------------------------
    miss_grams: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in ledger:
        if row.get("kind") != "miss" or not row.get("quote"):
            continue
        for g in _candidate_phrases(row["quote"]):
            miss_grams[(row.get("category") or "?", g)].append(row["quote"])

    # The n-grams drawn from one reported miss are siblings, not nestings, so
    # containment does not collapse them. Cluster by the misses a phrase
    # explains and offer exactly one anchor per cluster: a reviewer asked to
    # choose between five near-identical regexes will choose none of them.
    clusters: dict[tuple[str, frozenset], list[str]] = defaultdict(list)
    for (cat, gram), quotes in miss_grams.items():
        if len(quotes) < 2:
            continue                # one document's phrasing is not a pattern
        if cat not in lx.CATEGORIES:
            continue
        clusters[(cat, frozenset(quotes))].append(gram)

    def _informativeness(gram: str) -> tuple[int, int]:
        words = gram.split()
        return sum(1 for w in words if w not in _STOP), len(gram)

    for (cat, _quotes), grams in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        gram = max(grams, key=_informativeness)
        quotes = miss_grams[(cat, gram)]
        pattern = r"\b" + re.escape(gram).replace(r"\ ", r"\s+") + r"\b"
        if any(pattern == p for p in lx.CATEGORIES[cat]["anchors"]):
            continue
        out.append(
            Proposal(
                kind="add_anchor",
                category=cat,
                pattern=pattern,
                evidence=quotes[0][:220],
                support=len(quotes),
                rationale=(
                    f"{len(quotes)} reviewer-reported misses in {cat} share this "
                    f"phrasing and no current anchor covers it. Chosen from "
                    f"{len(grams)} candidate phrase(s) drawn from the same misses."
                ),
            )
        )

    # --- repeated rejections of one surface form ---------------------------
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for row in ledger:
        if row.get("kind") not in ("confirm", "reject"):
            continue
        for surface in row.get("matched") or []:
            key = (row.get("category") or "?", surface.strip().lower()[:80])
            tally[key][0 if row["kind"] == "confirm" else 1] += 1

    for (cat, surface), (ok, bad) in sorted(tally.items(), key=lambda kv: -kv[1][1]):
        total = ok + bad
        if bad < MIN_REJECTIONS or total == 0:
            continue
        rate = bad / total
        if rate < MIN_REJECTION_RATE:
            continue
        kind = "suppress_anchor" if ok == 0 else "demote_anchor"
        out.append(
            Proposal(
                kind=kind,
                category=cat,
                pattern=surface,
                evidence=surface,
                support=bad,
                rationale=(
                    f"matched {total} time(s) in {cat}; the reviewer rejected "
                    f"{bad} ({rate:.0%}). "
                    + (
                        "Never confirmed — proposed for suppression."
                        if ok == 0
                        else "Proposed for demotion to a context term, so it needs "
                        "a second independent signal before it fires."
                    )
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Overrides — the only thing that changes behaviour, and only once approved
# ---------------------------------------------------------------------------


def overrides_path(store: str) -> str:
    return os.path.join(store, OVERRIDES_NAME)


def load_overrides(store: str) -> dict[str, Any]:
    path = overrides_path(store)
    if not os.path.exists(path):
        return {"version": "0", "added_anchors": {}, "suppressed": {}, "demoted": {}}
    import yaml

    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data.setdefault("version", "0")
    for k in ("added_anchors", "suppressed", "demoted"):
        data.setdefault(k, {})
    return data


def approve(store: str, chosen: list[Proposal], approver: str) -> str:
    """Apply proposals and bump the lexicon version. Returns the new version.

    This is the human-in-the-loop step the whole module exists to protect. It is
    the only function here that changes what a future review will say.
    """
    data = load_overrides(store)
    try:
        new_version = str(int(data.get("version", "0")) + 1)
    except ValueError:
        new_version = "1"

    for p in chosen:
        if p.kind == "add_anchor":
            data["added_anchors"].setdefault(p.category, []).append(p.pattern)
        elif p.kind == "suppress_anchor":
            data["suppressed"].setdefault(p.category, []).append(p.pattern)
        elif p.kind == "demote_anchor":
            data["demoted"].setdefault(p.category, []).append(p.pattern)

    data["version"] = new_version
    data.setdefault("history", []).append(
        {
            "version": new_version,
            "approved_by": approver,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "changes": [p.to_dict() for p in chosen],
        }
    )

    import yaml

    os.makedirs(store, exist_ok=True)
    with open(overrides_path(store), "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
    return new_version


def _anchor_fires_on(anchor: str, surfaces: list[str]) -> bool:
    """Does this anchor pattern actually match one of the rejected surface forms?

    A reviewer rejects a finding, and what the ledger can record is the text that
    fired — "confidential information" — not the regex behind it. Comparing the
    two as strings silently never matches, so an approved suppression would look
    applied and do nothing. Running the pattern against the text is the only
    comparison that means anything.
    """
    for surface in surfaces:
        try:
            if re.search(anchor, surface, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def apply_overrides(store: str) -> str:
    """Fold approved overrides into the live lexicon. Returns the version applied.

    Called once per run, before extraction, so that a report can state the exact
    lexicon version that produced it.
    """
    data = load_overrides(store)

    for cat, patterns in (data.get("added_anchors") or {}).items():
        if cat in lx.CATEGORIES:
            for p in patterns:
                if p not in lx.CATEGORIES[cat]["anchors"]:
                    lx.CATEGORIES[cat]["anchors"].append(p)

    for cat, surfaces in (data.get("suppressed") or {}).items():
        if cat not in lx.CATEGORIES:
            continue
        lx.CATEGORIES[cat]["anchors"] = [
            a for a in lx.CATEGORIES[cat]["anchors"]
            if not _anchor_fires_on(a, surfaces)
        ]

    for cat, surfaces in (data.get("demoted") or {}).items():
        if cat not in lx.CATEGORIES:
            continue
        keep, moved = [], []
        for a in lx.CATEGORIES[cat]["anchors"]:
            (moved if _anchor_fires_on(a, surfaces) else keep).append(a)
        lx.CATEGORIES[cat]["anchors"] = keep
        lx.CATEGORIES[cat]["context"].extend(moved)

    return str(data.get("version", "0"))
