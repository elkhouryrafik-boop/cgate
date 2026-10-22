"""
cgate.adjudicate — config/checklist.example.yaml plus a document's requirements to fifteen item results.

The line this module holds
--------------------------
`config/checklist.example.yaml` says the system produces evidence and the Compliance Officer
signs. `model.Status` therefore has no `cleared` and no `satisfied`, and this
module never invents one. The strongest thing it can say about an item is
`evidence_present`, which means *the evidence the checklist asked for is on
file and internally consistent* — not that the item passes.

`prohibited_actions` in the checklist is described there as "enforced in code,
not by convention". `_assert_enforceable()` is that enforcement: if the ECC adds
a prohibition to the YAML that this module does not know how to honour, the run
fails loudly instead of silently ignoring it.

Absent evidence is not good news
--------------------------------
Eleven of the fifteen items need a source this tool does not carry — a registry,
the staff register, HR records, a signed declaration. With no evidence bundle
supplied they return `unresolved` with a stated reason. They do not return
`evidence_present`, and they are never quietly skipped: an item that was not
checked and an item that was checked and found clean must not look the same on
the page a Compliance Officer signs.

What extraction can and cannot prove
------------------------------------
Extracted Requirements are the *client's* rules. They prove item 12 — the
client's procurement integrity rules have been reviewed — because reviewing them
is the whole of that item. They do not prove item 3 or item 8, which are facts
about the firm and its subcontractors, not about the solicitation. So requirements
are attached to the other items as context, under `related_requirements`, and
carry no weight in their status. Conflating the two would let a well-drafted
contract clear a checklist item about the firm.
"""

from __future__ import annotations

from typing import Any

from .model import Document, ItemResult, Requirement, Status
from .resolve import dedupe_instruments

# Prohibitions this module knows how to honour. Compared against the YAML at load.
ENFORCED_PROHIBITIONS = {
    "issue_compliance_clearance",
    "issue_conditional_clearance",
    "lift_compliance_hold",
    "close_register_entry",
    "mark_item_satisfied_without_human_confirmation",
    "represent_system_output_as_a_CO_or_ECC_determination",
}

# Item 12 is the only item this tool can adjudicate from the document alone.
DOCUMENT_ADJUDICABLE = {12}


class ChecklistError(RuntimeError):
    """The checklist could not be honoured as written."""


def load_checklist(path: str) -> dict[str, Any]:
    import yaml

    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    _assert_enforceable(data)
    return data


def _assert_enforceable(checklist: dict[str, Any]) -> None:
    declared = set(checklist.get("prohibited_actions") or [])
    unknown = declared - ENFORCED_PROHIBITIONS
    if unknown:
        raise ChecklistError(
            "checklist declares prohibited actions this build cannot enforce: "
            + ", ".join(sorted(unknown))
            + ". Refusing to run rather than appear to honour them."
        )


def iter_items(checklist: dict[str, Any]):
    """(section_id, section_title, item) for all fifteen items."""
    for section in checklist.get("sections", []):
        for item in section.get("items", []):
            yield section["id"], section.get("title", ""), item


# ---------------------------------------------------------------------------
# Evidence bundle
# ---------------------------------------------------------------------------


def load_bundle(path: str | None) -> dict[str, Any]:
    """Externally-gathered evidence, keyed by item id as a string.

    Shape, all keys optional:

        {"3":  {"screening_result_set": [...], "retrieval_timestamp": "..."},
         "10": {"attestation": {"signed": true, "attestor": "...", "date": "..."}},
         "6":  {"signed_declaration": [...], "registry_collision_report": []}}

    Absent file means no external evidence, which is a legitimate state and is
    reported as such.
    """
    if not path:
        return {}
    import json

    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {str(k): v for k, v in raw.items()}


def _attestation_status(entry: dict | None) -> tuple[Status, list[str], list[str]]:
    if not entry or not entry.get("attestation"):
        return Status.UNRESOLVED, ["attestation_missing"], [
            "No attestation on file. The named attestor must declare before submission."
        ]
    att = entry["attestation"]
    if not att.get("signed"):
        return Status.FLAG, ["attestation_unsigned"], [
            f"Attestation present but unsigned (attestor: {att.get('attestor', 'unnamed')})."
        ]
    who = att.get("attestor", "unnamed")
    when = att.get("date", "undated")
    return Status.EVIDENCE_PRESENT, [], [f"Signed by {who} on {when}."]


# ---------------------------------------------------------------------------
# Item 12 — the one this tool adjudicates from the document
# ---------------------------------------------------------------------------


def _adjudicate_item_12(
    doc: Document, reqs: list[Requirement], item: dict
) -> tuple[Status, list[str], list[str]]:
    triggers: list[str] = []
    notes: list[str] = []
    escalators = set(item.get("escalate_on") or [])

    blocking = [e for e in doc.errors if e.get("blocking")]
    if blocking:
        return (
            Status.UNRESOLVED,
            ["extraction_unresolved"],
            [f"Ingest failed: {e['error_type']} — {e['message']}" for e in blocking],
        )

    if not reqs:
        return (
            Status.UNRESOLVED,
            ["extraction_unresolved"],
            [
                "No integrity, eligibility or conflict-of-interest provision was found. "
                "A UN solicitation that genuinely contains none is rare; treat this as "
                "a failure to read the document, not as a finding about the document."
            ],
        )

    # An instrument pulled in by reference and not attached is the escalation the
    # checklist names by hand: its content is unknown and must not be assumed.
    incorporated: dict[str, str | None] = {}
    for r in reqs:
        if r.incorporation and not r.incorporation.get("retrieved"):
            incorporated[r.incorporation["target"]] = r.incorporation.get("url")
    incorporated = dedupe_instruments(incorporated)
    if incorporated:
        triggers.append("conditions_incorporated_by_reference_not_retrieved")
        for target, url in sorted(incorporated.items()):
            notes.append(
                f"Incorporated by reference, not attached, content unknown: {target}"
                + (f" ({url})" if url else "")
            )

    unresolved_scope = [r for r in reqs if "exception_scope_unresolved" in r.triggers]
    if unresolved_scope:
        triggers.append("extraction_unresolved")
        notes.append(
            f"{len(unresolved_scope)} requirement(s) carry a carve-out whose scope "
            "points outside this document. The obligation cannot be read at full "
            "width until the target provision is supplied."
        )

    if any(n.startswith("word_spacing") or "spacing" in n for n in doc.notes):
        triggers.append("parser_disagreement")
        notes.append(
            "Ingest reported lost word spacing in part of the source. Provisions on "
            "affected pages may be misread; verify against the PDF."
        )

    unknown_modality = sum(1 for r in reqs if "modality_unresolved" in r.triggers)
    if unknown_modality:
        notes.append(
            f"{unknown_modality} provision(s) matched a category but no modal verb; "
            "they are reported for reading, not classified as binding."
        )

    fired = [t for t in triggers if t in escalators]
    status = Status.FLAG if fired else Status.EVIDENCE_PRESENT
    notes.insert(
        0,
        f"{len(reqs)} provision(s) extracted and verbatim-verified across "
        f"{len({r.category for r in reqs})} of 15 categories.",
    )
    return status, triggers, notes


# ---------------------------------------------------------------------------


def _evidence_gaps(item: dict, entry: dict) -> list[str]:
    """Evidence keys the checklist names for this item that the bundle does not carry."""
    wanted = item.get("evidence") or []
    return [k for k in wanted if k not in entry]


def adjudicate(
    checklist: dict[str, Any],
    doc: Document,
    requirements: list[Requirement],
    bundle: dict[str, Any] | None = None,
) -> list[ItemResult]:
    bundle = bundle or {}
    results: list[ItemResult] = []

    for section_id, _title, item in iter_items(checklist):
        iid = int(item["id"])
        entry = bundle.get(str(iid)) or {}
        handling = item.get("handling", "assisted")
        never_auto_clear = bool(item.get("never_auto_clear"))
        related = [r.req_id for r in requirements if iid in r.checklist_items]

        if iid in DOCUMENT_ADJUDICABLE:
            reqs_12 = [r for r in requirements if iid in r.checklist_items]
            status, triggers, notes = _adjudicate_item_12(doc, reqs_12, item)
        elif handling == "attested":
            status, triggers, notes = _attestation_status(entry)
        elif not entry:
            status = Status.UNRESOLVED
            triggers = []
            notes = [
                "Not checked: this item is decided against a source outside the "
                "solicitation (registry, register, HR record or signed declaration) "
                "and no evidence was supplied to this run."
            ]
        else:
            gaps = _evidence_gaps(item, entry)
            if gaps:
                status = Status.FLAG
                triggers = []
                notes = ["Evidence supplied but incomplete. Missing: " + ", ".join(gaps)]
            else:
                status = Status.EVIDENCE_PRESENT
                triggers = []
                notes = ["All evidence named by the checklist for this item is on file."]

            # Structural triggers the caller reported alongside the evidence.
            fired = [t for t in (entry.get("triggers") or []) if t in (item.get("escalate_on") or [])]
            if fired:
                status = Status.FLAG
                triggers = fired

        # Items 6, 7 and 9 cover the highest-consequence integrity risk. Complete evidence
        # advances them to a human, never past one.
        if never_auto_clear and status is Status.EVIDENCE_PRESENT:
            status = Status.REQUIRES_HUMAN
            notes.append(
                "never_auto_clear: this item is determined by the Compliance Officer "
                "on the evidence above. The system does not advance it further."
            )

        if related and iid not in DOCUMENT_ADJUDICABLE:
            notes.append(
                f"{len(related)} provision(s) in the solicitation bear on this item. "
                "They are the client's rules, and are listed as context; they are not "
                "evidence about the firm and carry no weight in the status above."
            )

        results.append(
            ItemResult(
                item_id=iid,
                section=section_id,
                text=item.get("text", ""),
                handling=handling,
                status=status,
                triggers=triggers,
                requirements=related,
                notes=notes,
                never_auto_clear=never_auto_clear,
            )
        )

    results.sort(key=lambda r: r.item_id)
    return results


def summarise(results: list[ItemResult]) -> dict[str, int]:
    out: dict[str, int] = {s.value: 0 for s in Status}
    for r in results:
        out[r.status.value] += 1
    return out
