"""
cgate — compliance gate. Pre-submission review support for a bidding firm.

The system gathers evidence. A named human decides. Every assertion it makes is
addressable back to bytes in a source file, and any assertion that is not is
deleted rather than downgraded (`cgate.ground`).

Pipeline
--------
    ingest    source file      -> reading-ordered Document with stable span ids
    segment   Document         -> Clauses, using the document's own numbering
    extract   Clauses          -> Requirements, by rule, with modality and carve-outs
    ground    Requirements     -> only those whose quotes are verifiably in the source
    adjudicate Requirements    -> fifteen ItemResults against config/checklist.example.yaml
    report    ReviewResult     -> the page the Compliance Officer signs

`feedback` closes the loop: reviewer decisions accumulate in a ledger, calibrate
the confidence printed in every report, and — only with a named approval — bump
the lexicon version.

Bumping TOOL_VERSION
--------------------
Any change to extraction, adjudication or the grounding gate must bump this.
Reviews record it, and the design commits the firm to reproducing
a review years later; without an accurate version stamp that promise is empty.
"""

TOOL_VERSION = "0.1.0"

TOOL_NAME = "cgate"

# Procedure and governance references are configurable (cgate.settings,
# config/procedure.example.yaml); this default is used in report footers.
GOVERNANCE_REF = "the cgate review-process notes"

__all__ = ["TOOL_VERSION", "TOOL_NAME", "GOVERNANCE_REF"]
