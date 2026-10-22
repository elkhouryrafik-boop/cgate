"""
cgate.reading — one extracted provision, in the words a reviewer thinks in.

Why this module exists
----------------------
`report.py` renders the page the Compliance Officer signs. It is precise and it
is dense, and it is read once, at the end. This module renders the same finding
in the margin of the document, next to the clause it came from, at the moment
the reviewer's eye is on that clause. Same facts, different reading position.

The four lines
--------------
A reviewer in the margin of a contract is asking four questions, in this order:

    What does this clause do to us?
    Why is that a problem?
    Why might it not be?
    How did you decide that — and can I check it?

So every provision renders as `binds`, `against`, `manageable`, `found`. The
fourth is not decoration: a margin note whose reasoning is hidden is a machine
asserting a conclusion, and the whole architecture of this system exists so that
it does not do that.

The word this module will not print
-----------------------------------
Nowhere does it say compliant, non-compliant, cleared, passed or satisfied.
`config/checklist.example.yaml` lists `issue_compliance_clearance` among the actions the system
is structurally prohibited from performing, `model.Status` has no state that
means clean, and `adjudicate._assert_enforceable()` refuses to run if that
guarantee is weakened. A margin note reading "compliant" would defeat all three
from the one place a reader trusts most.

What it says instead is **exposure**: what the clause binds the firm to, and
whether anything on file shows the firm can meet it. That is a statement about
evidence, which the system is permitted to make, rather than a determination,
which it is not.
"""

from __future__ import annotations

from .model import Requirement, Status
from . import GOVERNANCE_REF
from . import settings
from . import lexicon as lx

# Four bands, worst first. `exposure` returns the first rule that fires, so the
# reason a provision is marked is always the sharpest true reason about it, never
# an average of several.
#
# Why UNCLASSIFIED is its own band, and not folded into HIGH
# ---------------------------------------------------------
# On a real general-conditions tender, 40 of 41 provisions that would otherwise
# be HIGH carry `modality_unresolved` — the extractor could not tell an
# obligation from a permission. That is a limit of the parser, not a finding
# about the contract. Filed together, the seven clauses that genuinely
# incorporate an unattached document are buried under forty parse failures, and
# a band that fires on 43% of provisions is a band a reviewer learns to skip.
#
# So they are separated, and both are still surfaced. The reviewer gets a real
# triage order: what the document does that is sharp, then what the machine could
# not read, then what binds us with nothing on file to prove we can meet it.
HIGH = "high"
UNCLASSIFIED = "unclassified"
OPEN = "open"
ROUTINE = "routine"

EXPOSURE_ORDER = (HIGH, UNCLASSIFIED, OPEN, ROUTINE)

EXPOSURE_LABEL = {
    HIGH: "Read the clause itself",
    UNCLASSIFIED: "Not classified",
    OPEN: "Binds us, unproven",
    ROUTINE: "Routine",
}

# Triggers the extractor can raise, by what they mean for a reader.
#
# SUBSTANTIVE  something about the document escalates under the checklist.
# PARSE        the extractor could not classify the provision. A limit of the
#              tool, reported as one.
# INFORMATIONAL  worth showing, escalates nothing on its own.
#
# A trigger in none of these sets escalates to HIGH and says so. A new trigger
# added to the extractor must never lose its escalation by being unrecognised
# here — the same fail-loud posture as adjudicate._assert_enforceable().
SUBSTANTIVE_TRIGGERS = {
    "conditions_incorporated_by_reference_not_retrieved",
    "exception_scope_unresolved",
}
PARSE_TRIGGERS = {"modality_unresolved", "actor_unresolved"}
INFORMATIONAL_TRIGGERS = {"cited_at_clause_level"}
KNOWN_TRIGGERS = SUBSTANTIVE_TRIGGERS | PARSE_TRIGGERS | INFORMATIONAL_TRIGGERS

BINDS = {
    ("obligation", "contractor"): "Obliges the firm to act.",
    ("obligation", "subcontractor"): "Obliges our subcontractors, agents or consortium partners to act.",
    ("obligation", "personnel"): "Obliges our personnel individually.",
    ("obligation", "both"): "Obliges both parties.",
    ("obligation", "un"): "Obliges the client.",
    ("prohibition", "contractor"): "Forbids the firm from acting.",
    ("prohibition", "subcontractor"): "Forbids our subcontractors, agents or consortium partners.",
    ("prohibition", "personnel"): "Forbids our personnel individually.",
    ("prohibition", "both"): "Forbids both parties.",
    ("prohibition", "un"): "Forbids the client.",
    ("permission", "contractor"): "Permits the firm to act. Optional, not required.",
    ("right_of_un", "un"): "Reserves a right to the client, exercisable at its discretion.",
    ("condition", "contractor"): "Sets a condition the firm must satisfy to be eligible.",
    ("condition", "subcontractor"): "Sets a condition our subcontractors must satisfy to be eligible.",
}

MODALITY_SHORT = {
    "obligation": "Obligation",
    "prohibition": "Prohibition",
    "permission": "Permission",
    "right_of_un": "Client right",
    "condition": "Eligibility condition",
    "unknown": "Unclassified",
}

ACTOR_SHORT = {
    "contractor": "binds us",
    "subcontractor": "binds our counterparties",
    "personnel": "binds our people",
    "un": "binds the client",
    "both": "binds both",
    "unknown": "actor unresolved",
}


def _binds(req: Requirement) -> str:
    """One sentence: what this clause does, and to whom.

    Both halves can fail independently, and the sentence has to stay readable
    when either does. "Unclassified, binds us." is not a sentence a reviewer can
    act on, so each unresolved half is named as unresolved instead.
    """
    key = (req.modality.value, req.actor.value)
    if key in BINDS:
        return BINDS[key]

    mod_known = req.modality.value != "unknown"
    actor_known = req.actor.value != "unknown"

    if not mod_known and not actor_known:
        return ("Neither what this clause does nor which party it binds could be resolved "
                "from the wording. Read it.")
    if not mod_known:
        return (f"What this clause does — oblige, forbid, permit — could not be resolved "
                f"from the wording. It {ACTOR_SHORT.get(req.actor.value, 'binds an unresolved party')}.")
    if not actor_known:
        return (f"{MODALITY_SHORT[req.modality.value]}. Which party it binds could not be "
                "resolved from the wording — read the clause.")
    return f"{MODALITY_SHORT[req.modality.value]}. It {ACTOR_SHORT[req.actor.value]}."


def _unresolved_carve_out(req: Requirement):
    for ex in req.exceptions:
        if not ex.scope_resolved:
            return ex
    return None


def exposure(req: Requirement, item_status: dict[int, Status] | None = None) -> tuple[str, str]:
    """(level, the reason it is at that level). First true reason wins."""
    item_status = item_status or {}

    ex = _unresolved_carve_out(req)
    if ex is not None:
        return HIGH, (
            f"A carve-out on this clause (“{ex.marker}”) narrows it, and how far it "
            "reaches could not be resolved from the text. The system does not guess at the "
            "reach of an exception: it reports that it could not resolve it and escalates. "
            "This is the one place where reading the clause yourself is not optional."
        )

    if req.incorporation and not req.incorporation.get("retrieved"):
        target = req.incorporation.get("target", "another document")
        return HIGH, (
            f"This clause pulls in {target}, which is not in the bundle that was reviewed. "
            "Its content is unknown to this review and was not assumed. Whatever it says, "
            "the firm is bound by it on signature."
        )

    substantive = [t for t in req.triggers if t in SUBSTANTIVE_TRIGGERS]
    if substantive:
        return HIGH, (
            "Escalated on: " + ", ".join(substantive) + ". These are structural conditions "
            "the checklist names for this class of provision. None of them is a confidence "
            "score — the system does not have one and does not escalate on one."
        )

    unrecognised = [t for t in req.triggers if t not in KNOWN_TRIGGERS]
    if unrecognised:
        return HIGH, (
            "Triggers fired that this reading layer does not classify: "
            + ", ".join(unrecognised)
            + ". Treated as an escalation rather than ignored, because a trigger nobody "
            "has taught this layer to read is not evidence that it is harmless."
        )

    parse = [t for t in req.triggers if t in PARSE_TRIGGERS]
    if parse:
        return UNCLASSIFIED, (
            "The extractor found this provision but could not classify it: "
            + ", ".join(parse)
            + ". That is a limit of the parser, not a finding about the contract — and it "
            "is reported rather than dropped, because a provision the rules cannot read is "
            "precisely the one a person should."
        )

    if req.modality.value == "right_of_un":
        return OPEN, (
            "A right the client may exercise at its own discretion. Nothing here obliges "
            "the firm to do anything, so there is no evidence that closes it — but there is "
            "also nothing the firm can do to stop it being exercised. Worth reading with "
            "the exclusion risk in mind."
        )

    bearing = [i for i in req.checklist_items
               if item_status.get(i) in (Status.UNRESOLVED, Status.REQUIRES_HUMAN)]
    if bearing and req.actor.value in ("contractor", "subcontractor", "personnel"):
        which = ", ".join(str(i) for i in bearing)
        return OPEN, (
            f"This binds us, and checklist item {which} — the item that would show the firm "
            "can meet it — is not resolved. The clause is not the problem; the missing "
            "evidence is."
        )

    return ROUTINE, (
        "Standard for this class of solicitation, no carve-out left unresolved, and nothing "
        "incorporated that is not in hand."
    )


def manageable(req: Requirement, item_status: dict[int, Status] | None = None) -> str:
    """The other side. Empty string where there honestly is not one."""
    item_status = item_status or {}
    parts: list[str] = []

    # Deliberately not "the quote is verbatim": that is true of every provision
    # in every review, and a reason that never varies is decoration, not a reason.
    resolved = [ex for ex in req.exceptions if ex.scope_resolved]
    if resolved:
        parts.append(
            f"{len(resolved)} carve-out(s) on this clause were resolved, so the reach of "
            "the provision is known rather than assumed."
        )
    if req.incorporation and req.incorporation.get("retrieved"):
        parts.append(
            f"It incorporates {req.incorporation.get('target', 'another document')}, and "
            "that document is in the bundle — so nothing here is being taken on trust."
        )
    settled = [i for i in req.checklist_items if item_status.get(i) is Status.EVIDENCE_PRESENT]
    if settled:
        parts.append(
            "Evidence is on file for checklist item "
            + ", ".join(str(i) for i in settled)
            + ", which is what this clause bears on."
        )
    if req.modality.value == "permission":
        parts.append("It is a permission, not a requirement. Declining to use it costs nothing.")

    return " ".join(parts)


def found(req: Requirement) -> str:
    """How the machine got here. The reviewer is entitled to disagree with this."""
    parts: list[str] = []

    if req.matched:
        forms = ", ".join(f"“{m}”" for m in req.matched[:6])
        parts.append(f"Matched on {forms}.")
    parts.append(
        f"Classified as {MODALITY_SHORT.get(req.modality.value, req.modality.value).lower()}, "
        f"{ACTOR_SHORT.get(req.actor.value, 'actor unresolved')}."
    )
    spec = lx.CATEGORIES.get(req.category, {})
    name = str(spec.get("name", req.category)).replace("_", " ")
    parts.append(f"Filed under {req.category} ({name}).")
    if req.extractor and req.extractor != "rules":
        parts.append(f"Extractor: {req.extractor}.")
    else:
        parts.append("Found by rule, not by a language model.")
    return " ".join(parts)


def read(req: Requirement, item_status: dict[int, Status] | None = None) -> dict:
    """One provision as the four lines a reviewer reads in the margin."""
    level, why = exposure(req, item_status)
    spec = lx.CATEGORIES.get(req.category, {})
    where = f"p.{req.page}"
    if req.clause_number:
        where += f", clause {req.clause_number}"

    return {
        "req_id": req.req_id,
        "category": req.category,
        "category_name": str(spec.get("name", req.category)).replace("_", " "),
        "checklist_items": req.checklist_items,
        "modality": req.modality.value,
        "actor": req.actor.value,
        "page": req.page,
        "clause_number": req.clause_number,
        "where": where,
        "quote": req.evidence_quote or req.text,
        "exposure": level,
        "exposure_label": EXPOSURE_LABEL[level],
        "binds": _binds(req),
        "against": why,
        "manageable": manageable(req, item_status),
        "found": found(req),
        "triggers": req.triggers,
        "exceptions": [
            {
                "marker": ex.marker,
                "text": ex.text,
                "scope_resolved": ex.scope_resolved,
                "scope_note": ex.scope_note,
                "target_ref": ex.target_ref,
            }
            for ex in req.exceptions
        ],
        "incorporation": req.incorporation,
        "matched": req.matched,
    }


# ---------------------------------------------------------------------------
# The margin note, as plain text, for insertion into the document itself.
# ---------------------------------------------------------------------------

_DISCLAIMER = (
    "Not a determination of the Compliance Officer. The system gathers evidence; "
    f"a named human decides. Governed by {GOVERNANCE_REF}."
)


def comment_text(req: Requirement, item_status: dict[int, Status] | None = None) -> str:
    """A Word comment body. Reads top to bottom in the order the question is asked."""
    r = read(req, item_status)
    L: list[str] = []
    a = L.append

    a(f"CGATE {r['req_id']} · {r['exposure_label'].upper()} · {r['where']}")
    a("")
    a(f"BINDS: {r['binds']}")
    a("")
    a(f"READS AGAINST US: {r['against']}")
    if r["manageable"]:
        a("")
        a(f"MANAGEABLE BECAUSE: {r['manageable']}")
    a("")
    a(f"HOW THIS WAS FOUND: {r['found']}")
    if r["checklist_items"]:
        a("")
        a("BEARS ON CHECKLIST ITEM(S): "
          + ", ".join(str(i) for i in r["checklist_items"])
          + f" of {settings.ref('checklist')}.")
    a("")
    a(_DISCLAIMER)
    return "\n".join(L)
