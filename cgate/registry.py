"""
cgate.registry — what the firm knows about the third parties on a pursuit.

Serves checklist items 6, 7 and 9 — the items that cover the highest-consequence
integrity risk: a subcontractor bidding independently on the same procurement.

The two mechanisms implemented here
-----------------------------------
The design names four mechanisms for item 6. This module holds the two
that need no information from outside the firm:

1. **Mandatory counterparty declaration.** Every named subcontractor,
   consortium partner and agent signs a declaration, per submission, stating
   whether it is bidding independently or supporting another bidder on the same
   procurement. `declaration_status()` reports which are missing or unsigned,
   and `blocking()` is what a caller uses to stop a submission progressing.

2. **Cross-pursuit registry check.** Every party named across every live
   pursuit is cross-referenced. The same entity in two live pursuits is not
   misconduct and this module never says it is — it is a fact the Compliance
   Officer is entitled to see before signing, and nothing else in the firm's
   process surfaces it.

Mechanism 3 (pre-bid engagement capture) is intake data; mechanism 4
(shared-content detection) is `cgate.conflict`.

What this module refuses to do
------------------------------
It does not attempt to prove that no subcontractor is bidding independently.
The full bidder list for a live procurement is usually not knowable before
award, and a system that inferred a clean answer from its own ignorance would
be worse than no system. It enforces the declaration, reports the collisions it
can see, and routes the determination to a person with the evidence attached.

`config/checklist.example.yaml` marks items 6, 7 and 9 `never_auto_clear`, and
`adjudicate.py` forces them to `requires_human` even on complete evidence. That
is the design working, not a gap in it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, asdict

from . import db

# Roles that must carry a signed declaration before a submission goes out.
DECLARABLE_ROLES = ("subcontractor", "consortium_partner", "agent", "intermediary")


@dataclass
class Collision:
    """One entity, named on this pursuit and on another live pursuit."""

    entity_id: str
    legal_name: str
    role_here: str
    other_pursuit_id: str
    other_pursuit_title: str
    other_client: str
    role_there: str
    other_deadline: str
    same_client: bool
    same_tender_reference: bool

    @property
    def severity(self) -> str:
        """Ordering for the reviewer's attention. Never a verdict.

        Same tender reference is the most serious shape of the risk.
        Same client is the next most material. Everything else is context.
        """
        if self.same_tender_reference:
            return "same_procurement"
        if self.same_client:
            return "same_client"
        return "concurrent"


@dataclass
class PartyStanding:
    """Everything the registry knows about one named party on one pursuit."""

    entity_id: str
    legal_name: str
    role: str
    aliases: list[str] = field(default_factory=list)
    jurisdiction: str | None = None
    declaration_ref: str | None = None
    declaration_signed_at: str | None = None
    collisions: list[dict] = field(default_factory=list)

    @property
    def declaration_state(self) -> str:
        if not self.declaration_ref:
            return "missing"
        if not self.declaration_signed_at:
            return "unsigned"
        return "signed"

    @property
    def declarable(self) -> bool:
        return self.role in DECLARABLE_ROLES


# ---------------------------------------------------------------------------
# Cross-pursuit collision detection
# ---------------------------------------------------------------------------


def collisions_for(
    conn: sqlite3.Connection,
    pursuit_id: str,
    entity: str,
    role_here: str,
    *,
    as_of: str | None = None,
) -> list[Collision]:
    """Other live pursuits on which this entity is also named."""
    this = conn.execute(
        "SELECT client_entity, tender_reference FROM pursuit WHERE id = ?",
        (pursuit_id,),
    ).fetchone()
    if this is None:
        return []

    live_ids = {p["id"] for p in db.live_pursuits(conn, as_of)} - {pursuit_id}
    if not live_ids:
        return []

    out: list[Collision] = []
    rows = conn.execute(
        "SELECT pp.pursuit_id, pp.role, p.title, p.tender_reference, "
        "       p.submission_deadline, p.client_entity, "
        "       e.legal_name AS party_name, c.legal_name AS client_name "
        "FROM pursuit_party pp "
        "JOIN pursuit p ON p.id = pp.pursuit_id "
        "JOIN entity e  ON e.id = pp.entity_id "
        "JOIN entity c  ON c.id = p.client_entity "
        "WHERE pp.entity_id = ? AND pp.pursuit_id != ?",
        (entity, pursuit_id),
    )
    for r in rows:
        if r["pursuit_id"] not in live_ids:
            continue
        out.append(Collision(
            entity_id=entity,
            legal_name=r["party_name"],
            role_here=role_here,
            other_pursuit_id=r["pursuit_id"],
            other_pursuit_title=r["title"],
            other_client=r["client_name"],
            role_there=r["role"],
            other_deadline=r["submission_deadline"],
            same_client=r["client_entity"] == this["client_entity"],
            same_tender_reference=bool(
                this["tender_reference"]
                and r["tender_reference"]
                and this["tender_reference"] == r["tender_reference"]
            ),
        ))
    # Most material first.
    order = {"same_procurement": 0, "same_client": 1, "concurrent": 2}
    out.sort(key=lambda c: (order[c.severity], c.legal_name))
    return out


def standing(
    conn: sqlite3.Connection,
    pursuit_id: str,
    *,
    as_of: str | None = None,
) -> list[PartyStanding]:
    """Every named party on a pursuit, with its declaration state and collisions."""
    import json

    out: list[PartyStanding] = []
    for row in db.parties(conn, pursuit_id):
        cols = collisions_for(conn, pursuit_id, row["entity_id"], row["role"],
                              as_of=as_of)
        out.append(PartyStanding(
            entity_id=row["entity_id"],
            legal_name=row["legal_name"],
            role=row["role"],
            aliases=json.loads(row["aliases"] or "[]"),
            jurisdiction=row["jurisdiction"],
            declaration_ref=row["declaration_ref"],
            declaration_signed_at=row["declaration_signed_at"],
            collisions=[asdict(c) | {"severity": c.severity} for c in cols],
        ))
    return out


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def sign_declaration(
    conn: sqlite3.Connection,
    pursuit_id: str,
    entity: str,
    role: str,
    declaration_ref: str,
    signatory: str,
    *,
    signed_at: str | None = None,
    checklist_ver: str = "?",
) -> None:
    """Record that a counterparty declaration has been signed and filed.

    `signatory` is the person who signed for the counterparty. It is recorded in
    the ledger under their own name rather than under a system actor, because
    the declaration is a human assertion and the register has to show whose.
    """
    ref = (declaration_ref or "").strip()
    who = (signatory or "").strip()
    if not ref:
        raise ValueError("a declaration needs a reference to the executed document")
    if not who:
        raise ValueError("a declaration needs the name of the person who signed it")
    if who.lower().startswith(db.SYSTEM_ACTOR_PREFIX):
        raise ValueError(
            f"{who!r} is a system actor. A counterparty declaration is signed by a "
            "person at the counterparty, and the system cannot stand in for one."
        )

    signed_at = signed_at or db._now()
    cur = conn.execute(
        "UPDATE pursuit_party SET declaration_ref = ?, declaration_signed_at = ? "
        "WHERE pursuit_id = ? AND entity_id = ? AND role = ?",
        (ref, signed_at, pursuit_id, entity, role),
    )
    if cur.rowcount == 0:
        raise ValueError(
            f"no party {entity!r} with role {role!r} on pursuit {pursuit_id!r}; "
            "name the party on the pursuit before filing its declaration"
        )
    db.append(
        conn, "declaration_signed", who,
        {"pursuit_id": pursuit_id, "entity": entity, "role": role,
         "declaration_ref": ref, "signed_at": signed_at},
        pursuit_id=pursuit_id, checklist_ver=checklist_ver,
    )


def blocking(parties: list[PartyStanding]) -> list[PartyStanding]:
    """Parties whose declaration must exist before the submission may progress.

    3.10 makes the declaration blocking. This returns what is in the way, so a
    caller can refuse to advance rather than record a warning nobody reads.
    """
    return [p for p in parties if p.declarable and p.declaration_state != "signed"]


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def evidence(parties: list[PartyStanding]) -> dict:
    """Shape registry output as an evidence-bundle entry for items 6, 7 and 9.

    Keys are the ones `config/checklist.example.yaml` names. Triggers are structural: a missing
    declaration, an unsigned one, a registry collision. None of them reads a
    score, and none of them is a conclusion about the party.
    """
    triggers: set[str] = set()
    declarations = []
    collisions = []

    for p in parties:
        if p.declarable:
            state = p.declaration_state
            if state == "missing":
                triggers.add("declaration_missing")
            elif state == "unsigned":
                triggers.add("declaration_unsigned")
            declarations.append({
                "entity_id": p.entity_id, "legal_name": p.legal_name,
                "role": p.role, "state": state,
                "declaration_ref": p.declaration_ref,
                "signed_at": p.declaration_signed_at,
            })
        if p.collisions:
            triggers.add("registry_collision")
            collisions.extend(p.collisions)

    return {
        "signed_declaration": declarations,
        "registry_collision_report": collisions,
        "triggers": sorted(triggers),
    }


def summarise(parties: list[PartyStanding]) -> str:
    """One line per party, for a terminal."""
    if not parties:
        return "  no third parties named on this pursuit"
    lines = []
    for p in parties:
        bits = [f"  {p.legal_name} ({p.role})"]
        if p.declarable:
            bits.append(f"declaration {p.declaration_state}")
        if p.collisions:
            # collisions_for() sorts most-material first, so [0] is the worst.
            bits.append(f"{len(p.collisions)} live collision(s), most material: "
                        f"{p.collisions[0]['severity']}")
        lines.append(" — ".join(bits))
    return "\n".join(lines)
