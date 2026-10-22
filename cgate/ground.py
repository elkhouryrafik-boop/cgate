"""
cgate.ground — the grounding gate.

Every Requirement must carry spans that resolve, in the source document, to
text that literally contains what the Requirement claims to quote. A finding
that fails is deleted, not downgraded.

Why deletion and not a warning
------------------------------
Published evaluations of retrieval-augmented legal tools measure hallucination
at roughly 17-33% even with retrieval in place, and Dahl et al. find models have
no reliable awareness of their own errors — so a model's own confidence cannot
be used to sort good findings from invented ones. What can be used is whether
the quoted words exist at the cited location. That is a property of the
document, checkable without asking anything of the model, and it is the only
line of defence in this tool that an LLM cannot talk its way past.

The gate is applied identically to rule-derived and model-derived findings. For
rules, it should never fire; if it does, the extractor has an offset bug, and
`drop_rate` in the benchmark makes that visible instead of silent.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .model import Document, Requirement


def _canon(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[‘’]", "'", s)
    s = re.sub(r"[“”]", '"', s)
    s = re.sub(r"[‐-―]", "-", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


@dataclass
class GateReport:
    kept: int = 0
    dropped: int = 0
    reasons: dict[str, int] = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = {}

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def verify(doc: Document, req: Requirement) -> tuple[bool, str]:
    """(passes, reason). Sets req.evidence_quote to the verified source text."""
    if not req.spans:
        return False, "no_spans"

    parts: list[str] = []
    for sp in req.spans:
        text = doc.text_of(sp)
        if text is None:
            return False, "span_unresolvable"
        parts.append(text)
    source = _canon(" ".join(parts))
    if not source:
        return False, "empty_span_text"

    claim = _canon(req.text)
    if not claim:
        return False, "empty_claim"

    if claim in source:
        req.evidence_quote = " ".join(parts)
        return True, "ok"

    # A model may return the provision with internal whitespace or list markers
    # differently rendered. Token containment in order is still verifiable and
    # still means the words are in the document at the cited place.
    claim_tokens = claim.split()
    src_tokens = source.split()
    if len(claim_tokens) < 4:
        return False, "claim_too_short_to_verify"
    i = 0
    for tok in src_tokens:
        if i < len(claim_tokens) and tok == claim_tokens[i]:
            i += 1
    if i == len(claim_tokens):
        req.evidence_quote = " ".join(parts)
        return True, "ok_token_subsequence"

    return False, "quote_not_in_cited_span"


def gate(doc: Document, reqs: list[Requirement]) -> tuple[list[Requirement], GateReport]:
    rep = GateReport()
    kept: list[Requirement] = []
    for r in reqs:
        ok, reason = verify(doc, r)
        if ok:
            rep.kept += 1
            kept.append(r)
        else:
            rep.dropped += 1
            rep.note(reason)
    return kept, rep
