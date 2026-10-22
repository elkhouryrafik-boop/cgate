"""
cgate.datarequest — the review's unresolved items, as a document somebody can act on.

A review that ends with fourteen items unresolved has told the truth and helped
nobody. The gap is only useful if the next person knows what would close it, who
holds it, what form it has to arrive in, and what happens if it does not arrive
at all. This module turns the unresolved set into that request.

Three rules govern the output
-----------------------------
It asks only for what is actually outstanding. A request that re-asks for
evidence already on file trains the recipient to skim it.

It names the holder. "Provide the screening result set" is a task nobody owns.
"Business Development, from the supplier-registry and development-bank exclusion lists, with the
retrieval timestamp" is a task with an owner and a source.

It states what the evidence cannot buy. Items 6, 7 and 9 are never auto-cleared
however complete the evidence is — the declaration is enforced, the collisions
are detected, and the Compliance Officer still determines. Saying so up front is
the difference between a request and a promise the system is not allowed to keep.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .adjudicate import iter_items
from . import TOOL_NAME
from . import settings
from .model import ReviewResult, Status

# What each evidence key is, who holds it, and the form it has to arrive in.
# Keys are the `evidence:` names in config/checklist.example.yaml. Anything not listed here is
# still requested, using the key itself — an unmapped key is a gap in this table,
# and must not become a silently dropped request.
EVIDENCE: dict[str, tuple[str, str, str]] = {
    "commercial_registry_record": (
        "The client's entry in an official registry, or its constituting instrument "
        "where the client is a UN entity",
        "Business Development",
        "PDF or a screenshot showing the source URL and the date of retrieval",
    ),
    "client_official_domain": (
        "The client's official internet domain, used to confirm that the solicitation "
        "and all correspondence on it came from the client and not from a look-alike",
        "Business Development",
        "The domain as a string, e.g. `agency.example`",
    ),
    "solicitation_source_url": (
        "The address the solicitation was downloaded from",
        "Business Development",
        "Full URL. A forwarded email is not an official channel; if that is how it "
        "arrived, say so — that is item 2's escalation, not a failure to answer",
    ),
    "retrieval_timestamp": (
        "When the source above was read. A screening result with no timestamp cannot be "
        "shown to have been current at submission, which is the only thing it was for",
        "",   # rides along with whoever gathered the source; not a task of its own
        "ISO 8601 UTC, e.g. `2026-08-20T09:16:00Z`",
    ),
    "source_snapshot_hash": (
        "SHA-256 of the file as downloaded, so that the copy reviewed and the copy "
        "bid on can be shown to be the same bytes",
        "Business Development",
        "`sha256:` followed by 64 hex characters",
    ),
    "screening_result_set": (
        "The subject named above, screened against every debarment and ineligibility "
        "source the procedure names, with the result of each source recorded separately",
        "Compliance function",
        "One record per source: source name, result, entity searched, and any detail",
    ),
    "relationship_graph_extract": (
        "Known relationships between the firm personnel and the procuring entity or its "
        "evaluation team — prior employment, family, shared directorships",
        "Human Resources with Business Development",
        "One record per relationship, or an explicit empty list where none is known",
    ),
    "prior_engagement_history": (
        "Previous engagements with this client, including any where the firm advised "
        "on the design of what is now being procured",
        "Business Development",
        "Contract reference, dates, and role",
    ),
    "jurisdiction": (
        "Where the work is performed",
        "Business Development",
        "ISO 3166-1 alpha-2 country code",
    ),
    "risk_rating": (
        f"The rating that jurisdiction carries under {settings.ref('jurisdiction_table')} §4.1",
        "Compliance function",
        "`standard` or `high`, with the reason where high",
    ),
    "signed_declaration": (
        "A signed declaration from each proposed subcontractor and consortium partner "
        "that it is not bidding independently on this procurement and is not "
        "subcontracting to a competing bidder",
        "Business Development, countersigned by the counterparty",
        "One signed record per counterparty: name, signed true/false, date. Unsigned "
        "is a distinct state from missing, and both are reported",
    ),
    "registry_collision_report": (
        "The firm's own live-pursuit register, checked for the same counterparty "
        "appearing on more than one live pursuit",
        "Business Development",
        "One record per collision, or an explicit empty list",
    ),
    "attendance_record": (
        "Site visit and clarification-round attendance, where the client published it",
        "Business Development",
        "Attendee list with dates, or a note that the client published none",
    ),
    "conflictscan_report": (
        "Text and image overlap between this submission and other material, from "
        "`conflictscan.py`",
        "Whoever assembles the submission",
        "The tool's own JSON output",
    ),
    "verified_records": (
        "Each CV, credential and project reference in the submission, checked against "
        "its source record in the HR file",
        "Human Resources",
        "One record per person: name, CV version, source, and whether it is current",
    ),
    "staff_register_check": (
        "Each person named in the submission, checked against the staff register",
        "Human Resources",
        "One record per person: name, status, and whether staff or approved associate",
    ),
    "attestation": (
        "A signed attestation by the named attestor",
        "The attestor named in the checklist for that item",
        "Signed true/false, attestor name, and date. An unsigned attestation is "
        "reported as unsigned, not as absent, and neither is reported as clean",
    ),
}

TRIGGER_GLOSS: dict[str, str] = {
    "source_unreachable": "the source could not be reached at all",
    "no_record_found": "the source was reached and holds no record",
    "name_mismatch": "the name on the record is not the name on the solicitation",
    "unofficial_channel": "the solicitation did not come from the client's own channel",
    "no_snapshot": "no hashed copy of the file as downloaded was kept",
    "any_match": "the entity matched a debarment or ineligibility list",
    "partial_name_match": "a near-match that a person has to look at",
    "any_relationship_found": "a relationship exists between our people and theirs",
    "incomplete_personnel_data": "the personnel data needed to answer was not complete",
    "high_risk_jurisdiction": "the jurisdiction is rated high risk",
    "jurisdiction_not_in_table": "the jurisdiction is not in the risk table at all",
    "declaration_missing": "no declaration was obtained from that counterparty",
    "declaration_unsigned": "a declaration exists but nobody signed it",
    "registry_collision": "the counterparty appears on another live pursuit",
    "attendance_overlap": "the counterparty attended the same pre-bid event as a competitor",
    "content_similarity_above_threshold": "submission text overlaps other material",
    "image_match_found": "a figure or photograph appears in other material",
    "attestation_missing": "the attestation was not made",
    "attestation_unsigned": "the attestation was made but not signed",
    "undeclared_party_in_submission": "a party appears in the submission that was never declared",
    "no_source_record": "there is no source record to check the claim against",
    "value_mismatch": "the submission and the source record disagree",
    "record_stale": "the source record is too old to rely on",
    "source_unavailable": "the HR or register source could not be read",
    "person_not_in_register": "a named person is not in the staff register",
    "status_inactive": "a named person is in the register but not active",
}

# Six of the fifteen items carry `rule:` or `mechanisms:` instead of an
# `evidence:` list. They still need evidence — the checklist just names the
# procedure rather than the artifact. These map the procedure to what has to
# arrive for it to have been performed. Without them those six items appear in
# the request asking for nothing, which is worse than not listing them.
RULE_EVIDENCE: dict[str, list[str]] = {
    "screen_entity_against_all_sources": ["screening_result_set", "retrieval_timestamp"],
    "jurisdiction_risk_table": ["jurisdiction", "risk_rating"],
    "verify_against_hr_source_records": ["verified_records"],
    "verify_against_staff_register": ["staff_register_check"],
    # item 12 is decided from the solicitation itself and is never requested
    "extract_integrity_requirements": [],
}

MECHANISM_EVIDENCE: dict[str, list[str]] = {
    "counterparty_declaration": ["signed_declaration"],
    "cross_pursuit_registry_check": ["registry_collision_report"],
    "prebid_attendance_crosscheck": ["attendance_record"],
    "shared_content_detection": ["conflictscan_report"],
}

# The subject of a screening rule changes who is being screened, and therefore
# what has to come back.
SUBJECT_GLOSS: dict[str, str] = {
    "firm": "the firm itself",
    "each_named_third_party": "every subcontractor, agent and consortium partner named "
                              "in the submission, screened separately",
}


def needed_keys(spec: dict[str, Any]) -> list[str]:
    """Every evidence key an item needs, from whichever way the checklist states it."""
    keys: list[str] = list(spec.get("evidence") or [])
    for k in RULE_EVIDENCE.get(str(spec.get("rule", "")), []):
        if k not in keys:
            keys.append(k)
    for mech in spec.get("mechanisms") or []:
        for k in MECHANISM_EVIDENCE.get(mech, []):
            if k not in keys:
                keys.append(k)
    if spec.get("handling") == "attested" and "attestation" not in keys:
        keys.append("attestation")
    return keys


# Evidence keys whose natural shape is a list of records, not a scalar.
_LIST_KEYS = {
    "screening_result_set", "signed_declaration", "registry_collision_report",
    "attendance_record", "verified_records", "staff_register_check",
    "relationship_graph_extract",
}


def _skeleton(spec: dict[str, Any]) -> dict[str, Any]:
    """A JSON stub for one item, pre-keyed so the recipient fills, not designs."""
    out: dict[str, Any] = {}
    for key in needed_keys(spec):
        if key == "attestation":
            continue
        if key in _LIST_KEYS:
            out[key] = []
        elif key == "conflictscan_report":
            out[key] = {}
        else:
            out[key] = ""
    if spec.get("handling") == "attested":
        out["attestation"] = {"signed": False, "attestor": "", "date": ""}
    if not out:
        out = {"_": "see the narrative above; no fixed evidence key is defined for this item"}
    out["triggers"] = []
    return out


ATTESTOR_TITLE = {"bd_manager": "The BD Manager"}


def _holders(spec: dict[str, Any]) -> list[str]:
    holders: list[str] = []
    for key in needed_keys(spec):
        if key == "attestation":
            who = ATTESTOR_TITLE.get(
                str(spec.get("attestor", "")),
                str(spec.get("attestor", "")).replace("_", " ").title(),
            )
        else:
            who = EVIDENCE.get(key, ("", "", ""))[1]
        # A holder that is a fragment of another ("Business Development" inside
        # "Business Development, countersigned by the counterparty") is the same
        # team twice; the longer form carries the instruction, so it wins.
        if not who:
            continue
        if any(who in h for h in holders):
            continue
        holders = [h for h in holders if h not in who]
        holders.append(who)
    return holders


def render(
    result: ReviewResult,
    checklist: dict[str, Any],
    requested_by: str = "",
    due: str = "",
) -> str:
    specs = {int(item["id"]): (sec, title, item) for sec, title, item in iter_items(checklist)}

    outstanding = [i for i in result.items if i.status is Status.UNRESOLVED]
    determination = [i for i in result.items if i.status is Status.REQUIRES_HUMAN]
    on_file = [i for i in result.items if i.status is Status.EVIDENCE_PRESENT]

    L: list[str] = []
    a = L.append

    a("# Evidence Request — Pre-Submission Compliance Review")
    a("")
    a(f"**Solicitation:** {os.path.basename(result.doc_path)}  ")
    a(f"**Document ID (SHA-256):** `{result.doc_id}`  ")
    a(f"**Checklist:** {result.checklist_version} ({settings.ref('checklist')}), bound to "
      f"{settings.ref('procedure')}  ")
    a(f"**Review generated:** {result.generated_at} by cgate {result.tool_version}  ")
    if requested_by:
        a(f"**Requested by:** {requested_by}  ")
    if due:
        a(f"**Required by:** {due}  ")
    a("")
    a("---")
    a("")

    # ---- why this exists --------------------------------------------------
    a("## Why you are being asked")
    a("")
    a("The pre-submission compliance review of this solicitation has been run. One of")
    a("the fifteen checklist items — item 12, the client's own procurement integrity")
    a("rules — can be decided by reading the solicitation, and it has been. The rest are")
    a("facts about this firm, its subcontractors and its people. They are held in")
    a("registries, the staff register, HR files and signed declarations, and no amount of")
    a("reading the contract will establish them.")
    a("")
    a(f"**{len(outstanding)} of the fifteen items are unresolved for want of that "
      f"evidence.** This document lists exactly what would resolve each one.")
    a("")
    a("Two things worth stating plainly before the list:")
    a("")
    a("1. **An unanswered item is reported as an unanswered item.** It is not recorded")
    a("   as clean and it is not quietly dropped. It appears in the review the")
    a("   Compliance Officer signs, in its own section, with the reason it is open. If")
    a("   the evidence does not exist, the correct response is to say so — not to leave")
    a("   the request unanswered.")
    a("2. **Partial answers are worth sending.** Each evidence key is adjudicated on its")
    a("   own. Three of five keys returned moves the item further than waiting until all")
    a("   five are ready.")
    a("")

    # ---- the ask ----------------------------------------------------------
    a("## 1. What is outstanding")
    a("")
    if not outstanding:
        a("Nothing. Every item that evidence could resolve has been resolved.")
        a("")
    else:
        a("| # | Item | Held by |")
        a("|---:|---|---|")
        for it in outstanding:
            _, _, spec = specs.get(it.item_id, ("", "", {}))
            a(f"| {it.item_id} | {it.text} | {', '.join(_holders(spec)) or '—'} |")
        a("")

    # ---- per item ---------------------------------------------------------
    if outstanding:
        a("## 2. Each item, in detail")
        a("")
        for it in outstanding:
            sec, title, spec = specs.get(it.item_id, ("", "", {}))
            a(f"### Item {it.item_id} — {it.text}")
            a("")
            a(f"*Section {sec}, {title}. Handling: {it.handling}.*")
            a("")
            if it.notes:
                a("What the review said:")
                a("")
                for n in it.notes:
                    a(f"> {n}")
                a("")
            keys = needed_keys(spec)
            subject = SUBJECT_GLOSS.get(str(spec.get("subject", "")))
            if subject:
                a(f"**Screened subject:** {subject}.")
                a("")
            if keys:
                a("**What is needed**")
                a("")
                for key in keys:
                    what, who, form = EVIDENCE.get(
                        key, (key.replace("_", " "), "—", "as recorded"))
                    a(f"- `{key}` — {what}.")
                    if who:
                        a(f"    - Held by: {who}")
                    a(f"    - Form: {form}")
                a("")
            trig = spec.get("escalate_on") or []
            if trig:
                a("**Report these if you see them.** They are the conditions the checklist")
                a("names for this item. Reporting one is not a failure to answer — it is the")
                a("answer, and it routes the item to the Compliance Officer rather than")
                a("closing it.")
                a("")
                for t in trig:
                    a(f"- `{t}` — {TRIGGER_GLOSS.get(t, 'see config/checklist.example.yaml')}")
                a("")
            if spec.get("note"):
                a("> " + " ".join(str(spec["note"]).split()))
                a("")
            if spec.get("never_auto_clear"):
                a("> **This item is never cleared automatically.** Complete evidence moves it")
                a("> from *not resolved* to *for the Compliance Officer's determination*. It")
                a("> does not move it to clean, and nothing you send can. Send it anyway — the")
                a("> determination is only as good as what it is made on.")
                a("")

    # ---- what evidence cannot buy ----------------------------------------
    never = [i for i in result.items
             if specs.get(i.item_id, ("", "", {}))[2].get("never_auto_clear")]
    if never:
        a("## 3. What complete evidence will and will not do")
        a("")
        a("Items " + ", ".join(str(i.item_id) for i in never) + " close the highest-consequence integrity gap")
        a("(a subcontractor bidding independently on the same procurement). The checklist marks them as")
        a("never automatically cleared, and that is enforced in the code rather than by")
        a("convention. However complete the declarations and collision reports are, these")
        a("items arrive at the Compliance Officer for determination. The evidence changes")
        a("what the determination is made on. It does not remove the determination.")
        a("")

    # ---- already answered -------------------------------------------------
    if on_file or determination:
        a("## 4. Not being asked for")
        a("")
        if on_file:
            a("Evidence is already on file for these, and re-sending it is not needed:")
            a("")
            for it in on_file:
                a(f"- Item {it.item_id} — {it.text}")
            a("")
        if determination:
            a("These are with the Compliance Officer for determination. They are not")
            a("waiting on you:")
            a("")
            for it in determination:
                a(f"- Item {it.item_id} — {it.text}")
            a("")

    # ---- how to return ----------------------------------------------------
    a("## 5. How to return it")
    a("")
    a("Fill the skeleton below and return it as a single JSON file. Every key is")
    a("optional: send what exists, omit what does not, and use `triggers` to report any")
    a("of the conditions listed against that item. A key left blank is reported as")
    a("outstanding, which is the correct outcome and not a criticism.")
    a("")
    a("The completed file is read back into the review with:")
    a("")
    a("```")
    a('cgate review "<solicitation>" --bundle evidence.json --out out')
    a("```")
    a("")
    a("The review is then regenerated against the same document hash recorded at the top")
    a("of this request, so the two can be shown to concern the same document.")
    a("")

    # ---- skeleton ---------------------------------------------------------
    skel: dict[str, Any] = {}
    for it in outstanding:
        _, _, spec = specs.get(it.item_id, ("", "", {}))
        skel[str(it.item_id)] = _skeleton(spec)
    a("### Skeleton")
    a("")
    a("```json")
    a(json.dumps(skel, indent=2, ensure_ascii=False) if skel else "{}")
    a("```")
    a("")
    a("---")
    a("")
    a(f"Produced by {TOOL_NAME}, under its review-process notes. The system")
    a("gathers evidence; a named human decides. This request is not a determination of")
    a("the Compliance Officer or of the Ethics and Compliance Committee.")
    a("")
    return "\n".join(L)
