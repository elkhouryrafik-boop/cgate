"""
cgate.gate — assemble the decision-ready file for one pursuit.

This is the final assembly step. Everything else in the package answers part of the
checklist; this module puts the parts in one place, in the order a Compliance
Officer reads them, and hands over a file in which no item is silently absent.

What it does
------------
For a pursuit already on the register, it gathers:

    item 1        client registration, from the entity registry
    item 2        the solicitation's own hash as a source snapshot
    items 3, 8    screening of the firm and of every named party (cgate.screen)
    item 4        recorded relationships with the procuring entity
    item 5        jurisdiction risk against the checklist's own table
    items 6, 7, 9 declarations, cross-pursuit collisions (cgate.registry),
                  pre-bid attendance, and shared-content detection (cgate.conflict)
    item 12       extraction from the solicitation (cgate.pipeline)
    items 10, 11, 15  attestations, if a signed one has been supplied
    items 13, 14  left unresolved with the reason stated, until HR source
                  records exist to check against

then runs the ordinary review with that bundle, so the fifteen items are
adjudicated by exactly the same code whether the evidence came from a file a
person wrote or from the register.

The two rules that shape this module
------------------------------------
**Absent evidence is reported, never inferred.** Where a source does not exist —
no HR extract, no source URL, no attestation — the item stays `unresolved` and
says why. There is no code path that improves an item's status by finding
nothing.

**Assembly is not determination.** This produces a file. `blocking_reasons()`
lists what stands in the way of a submission, and it is the Compliance Officer
who acts on it. Nothing here writes to `decision`.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field

from . import conflict, db, registry, screen, workflow
from .adjudicate import load_checklist
from .db import _now
from .model import ReviewResult, Status, sha256_file
from .pipeline import run_review


@dataclass
class GateFile:
    """Everything gathered for one pursuit, plus the adjudicated review."""

    pursuit_id: str
    pursuit: dict
    review: ReviewResult
    bundle: dict
    clocks: dict
    parties: list = field(default_factory=list)
    screenings: list = field(default_factory=list)
    conflict_scans: list = field(default_factory=list)
    attendance: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def statuses(self) -> dict[int, str]:
        return {i.item_id: i.status.value for i in self.review.items}


# ---------------------------------------------------------------------------
# Per-item evidence
# ---------------------------------------------------------------------------


def _item_1(conn: sqlite3.Connection, pursuit: dict) -> dict:
    client = db.get_entity(conn, pursuit["client_entity"])
    if client is None or not client["registration_no"]:
        return {}
    return {
        "commercial_registry_record": {
            "legal_name": client["legal_name"],
            "registration_no": client["registration_no"],
            "jurisdiction": client["jurisdiction"],
        }
    }


def _item_2_hash(doc_id: str, source_url: str | None) -> dict:
    """The solicitation's hash is a real snapshot. Its provenance is not.

    Hashing the file proves which bytes were reviewed. It says nothing about
    where they came from, so without a recorded source URL the item stays
    incomplete rather than being closed by the half of the evidence that is
    easy to produce.
    """
    entry: dict = {"source_snapshot_hash": doc_id}
    if source_url:
        entry["solicitation_source_url"] = source_url
        entry["retrieval_timestamp"] = _now()
    return entry


def _item_4(conn: sqlite3.Connection, pursuit: dict) -> dict:
    """Recorded relationships between the firm's registry and the procuring entity."""
    client = pursuit["client_entity"]
    rows = list(conn.execute(
        "SELECT r.*, f.legal_name AS from_name, t.legal_name AS to_name "
        "FROM relationship r "
        "JOIN entity f ON f.id = r.from_entity "
        "JOIN entity t ON t.id = r.to_entity "
        "WHERE r.from_entity = ? OR r.to_entity = ?",
        (client, client),
    ))
    if not rows:
        # No recorded relationship is not the same as no relationship. The
        # registry only knows what has been entered into it, and item 4 is
        # `assisted` precisely because a person has to say so.
        return {}
    return {
        "relationship_graph_extract": [dict(r) for r in rows],
        "prior_engagement_history": [],
        "triggers": ["any_relationship_found"],
    }


def _item_5(pursuit: dict, checklist: dict) -> dict:
    """Jurisdiction risk, against the table the checklist itself carries."""
    item = _find_item(checklist, 5)
    high_risk = {c.upper() for c in (item.get("high_risk_default") or [])}
    code = (pursuit.get("jurisdiction") or "").strip().upper()

    if not code:
        return {}
    entry = {"jurisdiction": code, "high_risk_list": sorted(high_risk)}
    if code in high_risk:
        entry["triggers"] = ["high_risk_jurisdiction"]
    else:
        # The checklist carries a high-risk list, not a complete risk table.
        # A country absent from it has not been assessed as low risk; it has
        # not been assessed at all, and item 5 escalates on exactly that.
        entry["triggers"] = ["jurisdiction_not_in_table"]
    return entry


def _find_item(checklist: dict, item_id: int) -> dict:
    for section in checklist.get("sections", []):
        for item in section.get("items", []):
            if int(item["id"]) == item_id:
                return item
    return {}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def assemble(
    conn: sqlite3.Connection,
    pursuit_id: str,
    solicitation_paths: list[str],
    *,
    checklist_path: str = "config/checklist.example.yaml",
    firm_name: str,
    firm_aliases: list[str] | None = None,
    submission_path: str | None = None,
    third_party_material: list[str] | None = None,
    screening_config: screen.ScreeningConfig | None = None,
    attestations: dict | None = None,
    source_url: str | None = None,
    holidays=None,
    as_of: str | None = None,
    store: str = ".cgate",
) -> GateFile:
    """Gather every source and adjudicate the checklist for one pursuit."""
    row = conn.execute("SELECT * FROM pursuit WHERE id = ?", (pursuit_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such pursuit on the register: {pursuit_id!r}")
    pursuit = dict(row)
    checklist = load_checklist(checklist_path)
    notes: list[str] = []

    # --- parties, declarations, collisions (6, 7, 9) ----------------------
    parties = registry.standing(conn, pursuit_id, as_of=as_of)
    registry_ev = registry.evidence(parties)

    # --- screening (3, 8) -------------------------------------------------
    cfg = screening_config or screen.ScreeningConfig.from_env()
    firm_result = screen.screen_entity(
        firm_name, aliases=firm_aliases, kind="firm",
        country=pursuit.get("jurisdiction"), config=cfg)
    party_results = screen.screen_many(
        [{"name": p.legal_name, "aliases": p.aliases, "kind": "firm",
          "country": p.jurisdiction, "entity_id": p.entity_id} for p in parties],
        config=cfg)
    screenings = [firm_result, *party_results]

    # --- shared content (6, 15) -------------------------------------------
    scans = []
    for material in third_party_material or []:
        if not submission_path:
            notes.append(
                f"Third-party material supplied ({os.path.basename(material)}) but no "
                "submission to compare it against; shared-content detection did not run."
            )
            break
        scans.append(conflict.scan(submission_path, material))

    # --- attendance (6, mechanism 3) --------------------------------------
    attendance = db.attendance(conn, pursuit_id)

    # --- clocks ------------------------------------------------------------
    clocks = workflow.clocks(conn, pursuit_id, as_of=as_of, holidays=holidays)

    # --- bundle ------------------------------------------------------------
    bundle: dict[str, dict] = {}

    def put(item_id: int, entry: dict) -> None:
        if entry:
            bundle[str(item_id)] = entry

    put(1, _item_1(conn, pursuit))
    put(3, screen.evidence([firm_result]))
    put(4, _item_4(conn, pursuit))
    put(5, _item_5(pursuit, checklist))
    put(8, screen.evidence(party_results) if party_results else {})

    scan_triggers = sorted({t for s in scans for t in s.triggers})
    item_6 = dict(registry_ev)
    item_6["attendance_record"] = attendance
    item_6["conflictscan_report"] = [
        {"doc_a": os.path.basename(s.doc_a), "doc_b": os.path.basename(s.doc_b),
         "text_similarity": s.text_similarity,
         "exact_matches": len(s.exact_matches),
         "image_matches": len(s.image_matches),
         "metadata_matches": s.metadata_matches,
         "triggers": s.triggers}
        for s in scans
    ]
    item_6["triggers"] = sorted(set(item_6.get("triggers", [])) | set(scan_triggers))
    put(6, item_6)
    put(7, registry_ev)
    put(9, registry_ev)

    for iid in (10, 11, 15):
        put(iid, (attestations or {}).get(str(iid), {}))

    # Items 13 and 14 have no source to check against yet. Supplying nothing is
    # what leaves them `unresolved` with the reason printed, which is correct
    # until HR and credential records exist.
    notes.append(
        "Items 13 and 14 were not evaluated: they are checked against HR and "
        "credential source records, which are not connected to this system."
    )
    if not scans and (third_party_material or []) == []:
        notes.append(
            "No third-party material was supplied, so shared-content detection "
            "(item 6, mechanism 4) did not run. This is the mechanism that "
            "addresses undisclosed content-sharing directly."
        )
    if clocks.breaches:
        notes.append(
            f"{len(clocks.breaches)} clock breach(es) under 3.10. See the clocks "
            "section; a breach is a fact about the process, not about this file."
        )

    # --- adjudicate --------------------------------------------------------
    # Item 2's snapshot hash is the document's own id, which is the SHA-256 of
    # its bytes and needs no parse. Computing it here means the bundle is
    # complete before the review runs, so the solicitation is ingested once.
    put(2, _item_2_hash(sha256_file(solicitation_paths[0]), source_url))

    review, _lexicon, _blocking = run_review(
        solicitation_paths, checklist_path, None, store, bundle_data=bundle)

    return GateFile(
        pursuit_id=pursuit_id,
        pursuit=pursuit,
        review=review,
        bundle=bundle,
        clocks=clocks.to_dict(),
        parties=parties,
        screenings=screenings,
        conflict_scans=scans,
        attendance=attendance,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# What stands in the way
# ---------------------------------------------------------------------------


def blocking_reasons(gf: GateFile) -> list[str]:
    """Facts that stop this submission progressing under 3.10.

    Not a determination and not a recommendation. Each line is something a
    person has to resolve or consciously decide to proceed despite.
    """
    reasons: list[str] = []

    for p in registry.blocking(gf.parties):
        reasons.append(
            f"{p.legal_name} ({p.role}): counterparty declaration "
            f"{p.declaration_state}. 3.10 makes it blocking."
        )
    for s in gf.screenings:
        if not s.ok:
            reasons.append(
                f"{s.subject}: not screened — {s.errors[0].get('error_type')}. "
                "A screening that did not run is not a clean screening."
            )
    for b in gf.clocks.get("breaches", []):
        reasons.append(f"Clock breach ({b['clock']}): {b['message']}")
    for item in gf.review.items:
        if item.status is Status.UNRESOLVED:
            reasons.append(f"Item {item.item_id} unresolved: {item.text}")
    return reasons
