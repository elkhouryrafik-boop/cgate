"""
cgate.db — the Compliance Review Register and its append-only ledger.

`schema.sql` states two principles and says they are enforced rather than
promised. This module is where the enforcement that SQL cannot express lives.

What SQL already enforces
-------------------------
Triggers reject UPDATE and DELETE on `ledger` and `decision`. A CHECK
constraint refuses a `decision` row whose `actor_name` or `signature_ref` is
empty. Those hold whatever calls the database, including a person with a
sqlite3 prompt, and that is the point of putting them there.

What this module adds
---------------------
1. **A hash chain.** Each ledger row carries the hash of the row before it.
   Deleting a row is already impossible; the chain makes it *detectable* if
   someone bypasses the triggers by editing the file, which is the threat that
   matters for a register a firm may have to show an investigator.
   `verify_chain()` recomputes the whole thing and names the first bad row.

2. **A named natural person.** The SQL CHECK cannot tell a person from a
   service account called "compliance-bot". `record_decision()` refuses a role
   outside {CO, ECC, Board}, refuses an actor in the `system:` namespace that
   every machine-authored ledger row must use, and refuses a signature
   reference that is only whitespace.

3. **One writer for system findings.** `record_review()` is the only path from
   a `ReviewResult` into the register, so a check result cannot enter the
   database without the ledger row that says which tool version produced it
   against which checklist.

Actor namespacing
-----------------
Machines write as ``system:<component>``. People write as their own name. The
two namespaces do not overlap, and `record_decision()` enforces the separation
from the human side. An auditor reading the ledger can therefore answer "did a
person do this?" by looking at one field.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass

from . import TOOL_VERSION

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

SYSTEM_ACTOR_PREFIX = "system:"
HUMAN_ROLES = ("CO", "ECC", "Board")

# Field separator for the hash chain. Without it ("ab", "c") and ("a", "bc")
# produce the same digest, and the chain stops being evidence of anything.
_SEP = b"\x1f"


class LedgerError(RuntimeError):
    """The ledger could not be appended to, or does not verify."""


class DecisionError(RuntimeError):
    """A decision was offered that no human is accountable for."""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect(path: str = "register.db") -> sqlite3.Connection:
    """Open the register, creating and initialising it if it does not exist."""
    fresh = not os.path.exists(path) or os.path.getsize(path) == 0
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)

    # isolation_level=None: this module manages its own transactions, because
    # reading the previous row_hash and writing the next one must be atomic.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if fresh:
        with open(SCHEMA_PATH, encoding="utf-8") as fh:
            conn.executescript(fh.read())
    return conn


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def _normalise_name(name: str) -> str:
    """Fold a legal name to a comparison key.

    Deliberately conservative. It strips punctuation, case and the common legal
    forms, so that "Example Engineering Ltd." and "EXAMPLE ENGINEERING
    LTD" resolve to one entity. It does *not* attempt fuzzy matching:
    a near miss must surface as a candidate for a human, not be silently merged.
    """
    s = name.lower().strip()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    # Collapse runs of single letters left behind by a dotted acronym, so
    # "S.A.R.L." -> "s a r l" -> "sarl" and the legal-form strip below can see
    # it. Without this, a firm written "Contractors S.A.R.L." and the same firm
    # written "Contractors SARL" resolve to two different registry entities.
    s = re.sub(r"\b(?:[a-z]\s+){1,5}[a-z]\b",
               lambda m: m.group(0).replace(" ", ""), s)
    s = re.sub(
        r"\b(sal|sarl|s\s*a\s*l|llc|ltd|limited|inc|incorporated|plc|gmbh|"
        r"co|company|corp|corporation|holding|holdings|group|est|"
        r"establishment|wll|fzc|fze|jsc|ag|nv|bv|spa|srl|pte)\b",
        " ",
        s,
    )
    return re.sub(r"\s+", " ", s).strip()


def entity_id(legal_name: str, jurisdiction: str | None = None) -> str:
    """A stable id for an entity, derived from its normalised name.

    Deterministic on purpose: ingesting the same counterparty twice from two
    submissions must produce one registry row, or cross-pursuit collision
    detection (checklist items 6, 7, 9) has nothing to collide.
    """
    key = _normalise_name(legal_name)
    if jurisdiction:
        key += "|" + jurisdiction.strip().upper()
    return "e-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_hash(
    prev_hash: str | None,
    at: str,
    pursuit_id: str | None,
    event: str,
    actor: str,
    payload_json: str,
    checklist_ver: str,
    tool_version: str,
) -> str:
    h = hashlib.sha256()
    for part in (
        prev_hash or "",
        at,
        pursuit_id or "",
        event,
        actor,
        payload_json,
        checklist_ver,
        tool_version,
    ):
        h.update(part.encode("utf-8"))
        h.update(_SEP)
    return h.hexdigest()


def append(
    conn: sqlite3.Connection,
    event: str,
    actor: str,
    payload: dict,
    *,
    pursuit_id: str | None = None,
    checklist_ver: str = "?",
    tool_version: str = TOOL_VERSION,
    at: str | None = None,
) -> str:
    """Append one row to the audit ledger. Returns its row hash.

    `actor` is a person's name or `system:<component>`. There is no default:
    an event nobody is attributed with is not an audit record.
    """
    if not (actor or "").strip():
        raise LedgerError("every ledger row carries an actor; none was given")
    at = at or _now()
    payload_json = _canonical(payload)

    conn.execute("BEGIN IMMEDIATE")
    try:
        prev_row = conn.execute(
            "SELECT row_hash FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev = prev_row["row_hash"] if prev_row else None
        rh = _row_hash(
            prev, at, pursuit_id, event, actor, payload_json, checklist_ver, tool_version
        )
        conn.execute(
            "INSERT INTO ledger (at, pursuit_id, event, actor, payload, "
            "checklist_ver, tool_version, prev_hash, row_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (at, pursuit_id, event, actor, payload_json, checklist_ver,
             tool_version, prev, rh),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return rh


@dataclass
class ChainReport:
    ok: bool
    rows: int
    first_bad_seq: int | None = None
    reason: str = ""

    def __str__(self) -> str:
        if self.ok:
            return f"ledger verified: {self.rows} row(s), chain intact"
        return (
            f"LEDGER BROKEN at seq {self.first_bad_seq} of {self.rows}: {self.reason}"
        )


def verify_chain(conn: sqlite3.Connection) -> ChainReport:
    """Recompute every ledger row hash and check the linkage.

    This is the check an auditor runs. It answers two questions the triggers
    cannot: has a row been altered outside SQLite, and has one been removed.
    """
    prev: str | None = None
    n = 0
    for row in conn.execute("SELECT * FROM ledger ORDER BY seq ASC"):
        n += 1
        if row["prev_hash"] != prev:
            return ChainReport(
                False, n, row["seq"],
                "prev_hash does not match the preceding row — a row was removed, "
                "reordered, or inserted out of band",
            )
        expected = _row_hash(
            prev, row["at"], row["pursuit_id"], row["event"], row["actor"],
            row["payload"], row["checklist_ver"], row["tool_version"],
        )
        if expected != row["row_hash"]:
            return ChainReport(
                False, n, row["seq"],
                "row_hash does not match the row's own contents — the row was edited",
            )
        prev = row["row_hash"]
    return ChainReport(True, n)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def upsert_entity(
    conn: sqlite3.Connection,
    legal_name: str,
    kind: str = "firm",
    *,
    jurisdiction: str | None = None,
    aliases: list[str] | None = None,
    registration_no: str | None = None,
    created_by: str = SYSTEM_ACTOR_PREFIX + "registry",
) -> str:
    """Insert an entity if new, merge aliases if known. Returns its id."""
    if kind not in ("firm", "individual", "authority"):
        raise ValueError(f"unknown entity kind: {kind}")
    eid = entity_id(legal_name, jurisdiction)
    existing = conn.execute("SELECT * FROM entity WHERE id = ?", (eid,)).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO entity (id, kind, legal_name, aliases, jurisdiction, "
            "registration_no, created_by) VALUES (?,?,?,?,?,?,?)",
            (eid, kind, legal_name, _canonical(sorted(set(aliases or []))),
             jurisdiction, registration_no, created_by),
        )
        return eid

    # Known entity. Alias sets accumulate — screening needs every surface form
    # a party has ever been named by, and a later submission may carry one the
    # first did not.
    known = set(json.loads(existing["aliases"] or "[]"))
    merged = known | set(aliases or [])
    if legal_name != existing["legal_name"]:
        merged.add(legal_name)
    if merged != known:
        conn.execute(
            "UPDATE entity SET aliases = ? WHERE id = ?",
            (_canonical(sorted(merged)), eid),
        )
    return eid


def add_relationship(
    conn: sqlite3.Connection,
    from_entity: str,
    to_entity: str,
    kind: str,
    source: str,
    *,
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO relationship (from_entity, to_entity, kind, valid_from, "
        "valid_to, source) VALUES (?,?,?,?,?,?)",
        (from_entity, to_entity, kind, valid_from, valid_to, source),
    )


def resolve_name(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    """Entities whose legal name or any alias folds to the same key as `name`.

    Name resolution cannot go through `entity_id()`, because that id is salted
    with the jurisdiction and a caller who has only a name — an attendee on a
    sign-in sheet, say — does not have one. So this compares folded names
    across the registry instead.

    Returns every candidate rather than a best guess. Two entities with the
    same folded name in different jurisdictions is a question for a person, and
    picking one would answer it silently.
    """
    key = _normalise_name(name)
    if not key:
        return []
    out = []
    for row in conn.execute("SELECT * FROM entity"):
        names = [row["legal_name"], *json.loads(row["aliases"] or "[]")]
        if any(_normalise_name(n) == key for n in names):
            out.append(row)
    return out


def get_entity(conn: sqlite3.Connection, eid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM entity WHERE id = ?", (eid,)).fetchone()


def entity_names(row: sqlite3.Row) -> list[str]:
    """Every surface form of an entity: its legal name plus recorded aliases."""
    names = [row["legal_name"]]
    names += json.loads(row["aliases"] or "[]")
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# Pursuits
# ---------------------------------------------------------------------------


def open_pursuit(
    conn: sqlite3.Connection,
    pursuit_id: str,
    client_entity: str,
    title: str,
    submission_deadline: str,
    notified_by: str,
    *,
    scope_category: str = "A",
    jurisdiction: str | None = None,
    tender_reference: str | None = None,
    estimated_value: float | None = None,
    currency: str | None = None,
    notified_at: str | None = None,
    review_track: str = "standard",
    fast_track_approved_by: str | None = None,
    fast_track_approved_at: str | None = None,
    checklist_ver: str = "?",
) -> str:
    """Record the notification required before a pursuit may be worked on.

    Fast-track carries its own CHECK in SQL: the row cannot exist without a
    recorded approver. That constraint is the software form of the rule that
    fast-track may not be used to cure a late notification — the approval has
    to exist at the moment the track is set, not afterwards.
    """
    notified_at = notified_at or _now()
    conn.execute(
        "INSERT INTO pursuit (id, client_entity, scope_category, jurisdiction, "
        "tender_reference, title, estimated_value, currency, submission_deadline, "
        "notified_at, notified_by, review_track, fast_track_approved_by, "
        "fast_track_approved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pursuit_id, client_entity, scope_category,
         (jurisdiction or "").strip().upper() or None,
         tender_reference, title, estimated_value, currency, submission_deadline,
         notified_at, notified_by, review_track, fast_track_approved_by,
         fast_track_approved_at),
    )
    append(
        conn, "pursuit_notified", notified_by,
        {"pursuit_id": pursuit_id, "title": title, "client": client_entity,
         "tender_reference": tender_reference, "deadline": submission_deadline,
         "jurisdiction": jurisdiction, "review_track": review_track},
        pursuit_id=pursuit_id, checklist_ver=checklist_ver,
    )
    return pursuit_id


def add_party(
    conn: sqlite3.Connection,
    pursuit_id: str,
    entity: str,
    role: str,
    *,
    declaration_ref: str | None = None,
    declaration_signed_at: str | None = None,
    actor: str = SYSTEM_ACTOR_PREFIX + "intake",
    checklist_ver: str = "?",
) -> None:
    """Name a third party on a pursuit.

    A party is recorded whether or not its declaration exists. Recording it
    without one is what makes the absence visible to checklist items 6, 7 and
    9; refusing the row would make the party invisible instead.
    """
    conn.execute(
        "INSERT OR REPLACE INTO pursuit_party (pursuit_id, entity_id, role, "
        "declaration_ref, declaration_signed_at) VALUES (?,?,?,?,?)",
        (pursuit_id, entity, role, declaration_ref, declaration_signed_at),
    )
    append(
        conn, "party_named", actor,
        {"pursuit_id": pursuit_id, "entity": entity, "role": role,
         "declaration_on_file": bool(declaration_ref)},
        pursuit_id=pursuit_id, checklist_ver=checklist_ver,
    )


def parties(conn: sqlite3.Connection, pursuit_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT pp.*, e.legal_name, e.aliases, e.jurisdiction "
            "FROM pursuit_party pp JOIN entity e ON e.id = pp.entity_id "
            "WHERE pp.pursuit_id = ? ORDER BY pp.role, e.legal_name",
            (pursuit_id,),
        )
    )


def live_pursuits(conn: sqlite3.Connection, as_of: str | None = None) -> list[sqlite3.Row]:
    """Pursuits whose submission deadline has not passed and which are not closed."""
    as_of = as_of or _now()[:10]
    return list(
        conn.execute(
            "SELECT * FROM pursuit p WHERE p.submission_deadline >= ? "
            "AND NOT EXISTS (SELECT 1 FROM decision d WHERE d.pursuit_id = p.id "
            "AND d.decision = 'entry_closed') ORDER BY p.submission_deadline",
            (as_of,),
        )
    )


# ---------------------------------------------------------------------------
# System findings
# ---------------------------------------------------------------------------


def record_check_result(
    conn: sqlite3.Connection,
    pursuit_id: str,
    item_id: int | str,
    status: str,
    evidence: dict,
    *,
    triggers: list[str] | None = None,
    source_error: dict | None = None,
    tool_version: str = TOOL_VERSION,
) -> int:
    """Write one system finding. `status` must be a `model.Status` value.

    Nothing here can express a clearance. The column's CHECK constraint sees to
    that, and it is the reason the vocabulary in SQL and the vocabulary in
    `model.Status` are the same five words rather than two similar lists.
    """
    cur = conn.execute(
        "INSERT INTO check_result (pursuit_id, item_id, status, evidence, "
        "triggers, source_error, evaluated_at, tool_version) VALUES (?,?,?,?,?,?,?,?)",
        (pursuit_id, str(item_id), status, _canonical(evidence),
         _canonical(triggers or []),
         _canonical(source_error) if source_error else None,
         _now(), tool_version),
    )
    return int(cur.lastrowid)


def record_review(
    conn: sqlite3.Connection,
    pursuit_id: str,
    result,
    *,
    component: str = "adjudicate",
) -> int:
    """Write a whole `ReviewResult` into the register as system findings.

    One ledger row covers the run and names the document, the tool version and
    the checklist version, so that every check result written here can be tied
    back to a reproducible review. Returns the number of item rows written.
    """
    actor = SYSTEM_ACTOR_PREFIX + component
    for item in result.items:
        record_check_result(
            conn, pursuit_id, item.item_id, item.status.value,
            {"notes": item.notes, "handling": item.handling,
             "requirements": item.requirements, "doc_id": result.doc_id,
             "never_auto_clear": item.never_auto_clear},
            triggers=item.triggers,
            tool_version=result.tool_version,
        )
    append(
        conn, "review_run", actor,
        {"pursuit_id": pursuit_id, "doc_id": result.doc_id,
         "doc_path": os.path.basename(result.doc_path),
         "items": len(result.items), "stats": result.stats},
        pursuit_id=pursuit_id, checklist_ver=result.checklist_version,
        tool_version=result.tool_version,
    )
    return len(result.items)


# ---------------------------------------------------------------------------
# Human decisions
# ---------------------------------------------------------------------------


def record_decision(
    conn: sqlite3.Connection,
    pursuit_id: str,
    decision: str,
    reasons: str,
    actor_name: str,
    actor_role: str,
    signature_ref: str,
    *,
    conditions: str | None = None,
    decided_at: str | None = None,
    checklist_ver: str = "?",
) -> int:
    """Record a determination. Refuses anything a person is not accountable for.

    The SQL CHECK already refuses an empty name or signature. These checks add
    what SQL cannot see: that the role is one of the three the design
    recognises, and that the name is not a machine in the `system:`
    namespace wearing a person's field.
    """
    role = (actor_role or "").strip()
    if role not in HUMAN_ROLES:
        raise DecisionError(
            f"actor_role must be one of {', '.join(HUMAN_ROLES)}; got {actor_role!r}. "
            "No other role may determine a pursuit."
        )
    name = (actor_name or "").strip()
    if not name:
        raise DecisionError("a decision requires a named natural person")
    if name.lower().startswith(SYSTEM_ACTOR_PREFIX):
        raise DecisionError(
            f"{name!r} is a system actor. The system does not decide; "
            "it produces the evidence a person decides on."
        )
    sig = (signature_ref or "").strip()
    if not sig:
        raise DecisionError(
            "a decision requires a signature reference pointing at the executed record"
        )
    if not (reasons or "").strip():
        raise DecisionError("a decision requires stated reasons (3.10 §6.3)")

    decided_at = decided_at or _now()
    cur = conn.execute(
        "INSERT INTO decision (pursuit_id, decision, conditions, reasons, "
        "actor_name, actor_role, signature_ref, decided_at) VALUES (?,?,?,?,?,?,?,?)",
        (pursuit_id, decision, conditions, reasons, name, role, sig, decided_at),
    )
    append(
        conn, "decision_recorded", name,
        {"pursuit_id": pursuit_id, "decision": decision, "role": role,
         "conditions": conditions, "signature_ref": sig},
        pursuit_id=pursuit_id, checklist_ver=checklist_ver,
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Pre-bid engagement (checklist item 6, mechanism 3)
# ---------------------------------------------------------------------------


def record_attendance(
    conn: sqlite3.Connection,
    pursuit_id: str,
    kind: str,
    occurred_at: str,
    attendee_name: str,
    recorded_by: str,
    *,
    attendee_entity: str | None = None,
    location: str | None = None,
    source: str | None = None,
    checklist_ver: str = "?",
) -> int:
    """Record who was observed at a site visit, pre-bid meeting or clarification.

    `attendee_entity` is set when the attendee resolves to a known registry
    entity, and left null when it does not. A name nobody could resolve is
    still recorded — an unrecognised firm at a site visit is exactly the thing
    a reviewer wants to see, and dropping it would make the record cleaner and
    less true.
    """
    if not (attendee_name or "").strip():
        raise ValueError("an attendance record needs the attendee's name")
    if not (recorded_by or "").strip():
        raise ValueError("an attendance record needs the name of who recorded it")
    cur = conn.execute(
        "INSERT INTO prebid_engagement (pursuit_id, kind, occurred_at, location, "
        "attendee_entity, attendee_name, recorded_by, source) VALUES (?,?,?,?,?,?,?,?)",
        (pursuit_id, kind, occurred_at, location, attendee_entity,
         attendee_name.strip(), recorded_by.strip(), source),
    )
    append(
        conn, "attendance_recorded", recorded_by.strip(),
        {"pursuit_id": pursuit_id, "kind": kind, "occurred_at": occurred_at,
         "attendee": attendee_name.strip(), "entity": attendee_entity,
         "resolved": bool(attendee_entity)},
        pursuit_id=pursuit_id, checklist_ver=checklist_ver,
    )
    return int(cur.lastrowid)


def attendance(conn: sqlite3.Connection, pursuit_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM prebid_engagement WHERE pursuit_id = ? ORDER BY occurred_at",
        (pursuit_id,))]


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------


def register(conn: sqlite3.Connection) -> list[dict]:
    """The Compliance Review Register required by SOP-3.10 §10."""
    return [dict(r) for r in conn.execute("SELECT * FROM compliance_review_register")]


def ledger_rows(conn: sqlite3.Connection, pursuit_id: str | None = None) -> list[dict]:
    if pursuit_id:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE pursuit_id = ? ORDER BY seq", (pursuit_id,)
        )
    else:
        rows = conn.execute("SELECT * FROM ledger ORDER BY seq")
    return [dict(r) for r in rows]
